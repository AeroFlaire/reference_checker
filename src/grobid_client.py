"""
Wrapper around GROBID's HTTP API. We use two endpoints:

  /api/processFulltextDocument  — extract structured references from a whole PDF.
  /api/processCitation           — parse a single raw reference string into TEI.

The processCitation endpoint replaces the previous Ollama-based parsing of
"messy" references. It's purpose-built for citation parsing, runs in the same
container we already need anyway, and produces structured TEI we already know
how to read. Removing Ollama drops the editor's install burden by ~4GB and one
long-running daemon.

Ollama can still be enabled as a fallback for the rare case where GROBID's
per-citation parser also fails — set USE_OLLAMA_FALLBACK=true.
"""
import os
import re
import time
import requests
from typing import Optional
from lxml import etree

import fitz  # PyMuPDF

import config
from matching import repair_pdf_glyphs

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


# ---------------------------------------------------------------------------
# Page-pruning helper (unchanged behaviour, just lives here now)
# ---------------------------------------------------------------------------

def create_reference_digest_pdf(original_path: str) -> str:
    """Scan every page; if the document is large, keep only the references.

    Strategy:
      1. Short documents (≤ 30 pages) — return as-is. Pruning is risky here
         because each page carries lots of citation-y text and the body
         pages can outscore the actual references page; we'd rather give
         GROBID the whole thing than drop the references section.
      2. Look anywhere on each page for a 'References' / 'Bibliography'
         heading. If we find one, keep that page and all pages after it
         (references almost always run to the end of the document).
      3. Fall back to a per-page score heuristic only if no heading is
         found at all.

    Returns the original path on any failure or if pruning wouldn't change
    anything.
    """
    try:
        doc = fitz.open(original_path)
        total_pages = len(doc)

        # 1. Short docs — never prune.
        if total_pages <= 30:
            doc.close()
            return original_path

        # 2. Find the references heading. Anywhere on the page, on its own
        # line. We'll keep the first match's page and everything after it.
        header_regex = re.compile(
            r"(?m)^\s*(?:references?|bibliography|literature\s+cited|works\s+cited)\s*$",
            re.IGNORECASE,
        )
        references_start = None
        for page_num in range(total_pages):
            text = doc[page_num].get_text("text")
            if header_regex.search(text):
                references_start = page_num
                break

        if references_start is not None:
            pages_to_keep = list(range(references_start, total_pages))
        else:
            # 3. Heuristic fallback for docs without a clean heading.
            pages_to_keep = []
            for page_num in range(total_pages):
                text = doc[page_num].get_text("text")
                score = 0
                score += len(re.findall(r"(?:\[\d+\]|^\s*\d+\.)", text, re.MULTILINE)) * 2
                score += len(re.findall(r"\b(?:19|20)\d{2}[a-z]?\b", text))
                # Match abbreviations both with and without trailing period —
                # ACM-style "doi:10..." has no period after "doi".
                score += len(re.findall(r"\b(?:vol|pp|eds|proc|trans)\.?\s", text, re.IGNORECASE))
                # Strong signal: any DOI-like token on the page
                score += len(re.findall(r"\b(?:doi[:\s.]+)?10\.\d{4,9}/", text, re.IGNORECASE)) * 3
                if score > 15:
                    pages_to_keep.append(page_num)

        if not pages_to_keep or len(pages_to_keep) == total_pages:
            doc.close()
            return original_path

        new_doc = fitz.open()
        for p in pages_to_keep:
            new_doc.insert_pdf(doc, from_page=p, to_page=p)
        digest_path = original_path.replace(".pdf", "_digest.pdf")
        new_doc.save(digest_path)
        new_doc.close()
        doc.close()
        print(f"  -> Reference digest: kept {len(pages_to_keep)}/{total_pages} pages "
              f"(starting at page {pages_to_keep[0]+1})")
        return digest_path

    except Exception as e:
        print(f"  -> Digest creation error: {e}")
        return original_path


# ---------------------------------------------------------------------------
# GROBID full-document processing
# ---------------------------------------------------------------------------

def grobid_process_fulltext(pdf_path: str, retries: int = 3, timeout: tuple = (5, 60)) -> Optional[bytes]:
    """POST a PDF to /processFulltextDocument; return TEI XML bytes."""
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)

    print(f"  -> GROBID fulltext: {os.path.getsize(pdf_path)/1024:.1f} KB")

    for attempt in range(retries):
        try:
            with open(pdf_path, "rb") as f:
                resp = requests.post(
                    config.GROBID_FULLTEXT_URL,
                    files={"input": f},
                    timeout=timeout,
                )
            if resp.status_code == 200:
                return resp.content
            print(f"  -> GROBID error {resp.status_code}: {resp.text[:120]}")
            return None
        except requests.exceptions.Timeout:
            print(f"  -> GROBID timeout (attempt {attempt+1}/{retries})")
        except requests.exceptions.ConnectionError:
            print(f"  -> Cannot reach GROBID at {config.GROBID_FULLTEXT_URL}")
            return None
        except Exception as e:
            print(f"  -> GROBID error: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# GROBID single-citation parsing  (replaces Ollama)
# ---------------------------------------------------------------------------

# Cheap in-memory cache keyed on the citation string; many references repeat
# across a 600-PDF corpus, and processCitation is the slowest call per ref.
_citation_memo: dict = {}


def grobid_process_citation(reference_string: str, timeout: int = 15) -> Optional[dict]:
    """Parse a single citation string via /processCitation.

    Returns a dict with title / author / year / doi keys (any may be empty),
    or None if GROBID could not parse the string at all.
    """
    if not reference_string or len(reference_string) < 10:
        return None

    if reference_string in _citation_memo:
        return _citation_memo[reference_string]

    try:
        resp = requests.post(
            config.GROBID_CITATION_URL,
            data={"citations": reference_string, "consolidateCitations": "0"},
            timeout=timeout,
        )
        if resp.status_code != 200 or not resp.content:
            _citation_memo[reference_string] = None
            return None

        try:
            # GROBID returns a TEI <biblStruct> at the document root
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError:
            _citation_memo[reference_string] = None
            return None

        result = _extract_biblstruct(root)
        _citation_memo[reference_string] = result
        return result

    except requests.exceptions.RequestException as e:
        print(f"  -> processCitation error: {e}")
        return None


def _first_text(node, xpath_expr: str) -> str:
    """Helper: first matching text node, stripped, or empty string."""
    out = node.xpath(xpath_expr, namespaces=TEI_NS)
    if out and isinstance(out[0], str):
        return out[0].strip()
    return ""


def _extract_biblstruct(node) -> dict:
    """Pull the structured fields we care about out of a TEI biblStruct.

    Tolerates the slightly different shapes that processCitation and
    processFulltextDocument produce.
    """
    title = _first_text(node, ".//tei:analytic/tei:title/text()")
    if not title:
        title = _first_text(node, ".//tei:monogr/tei:title/text()")

    author = _first_text(node, ".//tei:author/tei:persName/tei:surname/text()")

    year = ""
    when = node.xpath(".//tei:date[@type='published']/@when", namespaces=TEI_NS)
    if when:
        year = when[0].split("-")[0]
    if not year:
        # Fallback: any 4-digit year-looking child of <date>
        any_date = _first_text(node, ".//tei:date/text()")
        m = re.search(r"\b(19|20)\d{2}\b", any_date)
        if m:
            year = m.group(0)

    doi = _first_text(node, ".//tei:idno[@type='DOI']/text()")
    arxiv = _first_text(node, ".//tei:idno[@type='arXiv']/text()")

    return {
        "title": title,
        "author": author,
        "year": year or None,
        "doi": doi or None,
        "arxiv": arxiv or None,
    }


# ---------------------------------------------------------------------------
# Top-level: extract references from a PDF
# ---------------------------------------------------------------------------

def extract_references(pdf_path: str) -> list:
    """Run GROBID on (the reference pages of) a PDF and return a list of dicts:

        {"raw_text", "grobid_title", "grobid_author", "grobid_year", "is_suspicious"}
    """
    target = create_reference_digest_pdf(pdf_path)
    xml_content = grobid_process_fulltext(target)

    # Clean up the digest if we made one
    if target != pdf_path and os.path.exists(target):
        try:
            os.remove(target)
        except OSError:
            pass

    if not xml_content:
        return []

    try:
        root = etree.fromstring(xml_content)
    except etree.XMLSyntaxError as e:
        print(f"GROBID XML parse error: {e}")
        return []

    extracted = []
    for bibl in root.xpath("//tei:listBibl/tei:biblStruct", namespaces=TEI_NS):
        # 1. Raw string
        raw_node = bibl.xpath("./tei:note[@type='raw_reference']/text()", namespaces=TEI_NS)
        raw_text = raw_node[0].strip() if raw_node else ""
        if not raw_text:
            raw_text = " ".join(t.strip() for t in bibl.xpath(".//text()", namespaces=TEI_NS))

        # PDFs often mangle separators (em-dash → "ś", soft-hyphen, etc.)
        # and embed URL-encoded filenames. Repair before downstream regex
        # matching so DOI / WG21 / year regexes can actually fire.
        raw_text = repair_pdf_glyphs(raw_text)

        # 2. Append structured DOI if not already present
        doi_nodes = bibl.xpath(".//tei:idno[@type='DOI']/text()", namespaces=TEI_NS)
        if doi_nodes and doi_nodes[0] not in raw_text:
            raw_text += f" DOI:{doi_nodes[0]}"

        # 3. Structured metadata for the Phase-2 fast lane
        fields = _extract_biblstruct(bibl)
        # Same glyph repair on the structured fields — GROBID inherits the
        # PDF's character soup.
        if fields.get("title"):
            fields["title"] = repair_pdf_glyphs(fields["title"])
        if fields.get("author"):
            fields["author"] = repair_pdf_glyphs(fields["author"])

        # 4. Suspicion heuristics. The dominant failure mode is GROBID
        # picking up body-text paragraphs from the PDF and treating them as
        # references (e.g. "Hana is a header-only library for C++
        # metaprogramming..."). The detector below catches those by
        # counting prose sentences — multiple full sentences with normal
        # English structure are almost never citations even when GROBID
        # managed to fish a token out as a "title".
        #
        # We deliberately do NOT use the presence of a year, venue, or
        # citation-like word as a "this must be a reference" signal: prose
        # blurbs frequently mention years and product names too.
        is_suspicious = False

        # 4a. Empty extraction with nothing to verify against
        if not fields["title"] and not fields["author"]:
            is_suspicious = True

        # 4b. Multi-sentence prose detector. Counts transitions like
        # "lowercase. Uppercase" — a strong indicator of body prose.
        # Real references rarely have more than one such transition (e.g.
        # journal names like "Trans. on ..." produce one).
        sentence_breaks = len(re.findall(r"[a-z]\.\s+[A-Z]", raw_text))
        if sentence_breaks >= 2 and len(raw_text) > 200:
            is_suspicious = True

        # 4c. Opens with a "[Subject] [optional parenthetical] is/are/..."
        # construction — a hallmark of marketing-blurb intros that get
        # accidentally extracted as references. Tolerates parentheticals
        # between the subject and the verb (e.g. "SYCL (pronounced
        # 'sickle') is a royalty-free...").
        if re.match(
            r"^\s*[A-Z][\w.\-]*(?:\s+(?:\([^)]{0,40}\)|[A-Za-z\.\-]+)){0,3}\s+"
            r"(?:is|are|was|were|provides?|enables?|allows?|builds?|"
            r"controls?|boasts?|leverages?|aims?|consists?|works?|"
            r"helps?|supports?)\s+",
            raw_text,
        ) and len(raw_text) > 200:
            is_suspicious = True

        # 4d. Long blob with no DOI / arXiv / WG21 paper id — almost
        # certainly body prose. Real references this long usually have at
        # least one of those identifiers. The 600-char cutoff was tuned
        # against representative cases: legitimate long references (full
        # author lists, vol/pp metadata) almost always carry a DOI; long
        # blocks without one are body text.
        has_strong_id = bool(re.search(
            r"\b(?:doi[:\s]|10\.\d{4,9}/|arxiv[:\s]|"
            r"\bRFC[\s-]?\d|\b[NP]\d{4}(?:R\d+)?\b)",
            raw_text,
            re.IGNORECASE,
        ))
        if len(raw_text) > 600 and not has_strong_id:
            is_suspicious = True

        extracted.append({
            "raw_text": raw_text,
            "grobid_title": fields["title"],
            "grobid_author": fields["author"],
            "grobid_year": fields["year"],
            "grobid_doi": fields["doi"],
            "grobid_arxiv": fields["arxiv"],
            "is_suspicious": is_suspicious,
        })

    return extracted


# ---------------------------------------------------------------------------
# Health check (used by start scripts and the CLI)
# ---------------------------------------------------------------------------

def grobid_alive(timeout: int = 3) -> bool:
    try:
        resp = requests.get(f"{config.GROBID_HOST}/api/isalive", timeout=timeout)
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False


def wait_for_grobid(max_wait_seconds: int = 120) -> bool:
    """Block until GROBID answers — used at startup so the app waits for the
    container to finish booting (Java + ML models take ~30-60s)."""
    start = time.time()
    while time.time() - start < max_wait_seconds:
        if grobid_alive():
            return True
        time.sleep(2)
    return False
