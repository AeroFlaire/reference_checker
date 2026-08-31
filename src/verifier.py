"""
Reference verification orchestrator.

For each reference, walk through phases in increasing cost / decreasing
specificity:

  Phase 0  — Standards sniper:  WG21, IETF RFC, ISBN
  Phase 1  — DOI / arXiv exact ID lookup (OpenAlex, then Crossref, then S2)
  Phase 2  — GROBID-extracted (title, author, year) → search-based verify
  Phase 3  — GROBID processCitation re-parse → search-based verify
              (this replaces the old Ollama call)
  Phase 3.5— Crossref bibliographic-search backstop
  Phase 4  — Semantic Scholar search backstop
  Phase 4.25 — OpenAlex general-search with raw reference
  Phase 4.5— OPTIONAL Ollama fallback (only if USE_OLLAMA_FALLBACK=true)
  Phase 4.9— Web search + page metadata (final non-academic backstop:
              news articles, NIST/standards docs, software project pages)
  Phase 5  — return NOT_FOUND (or NOT_REFERENCE if heuristically suspicious)

Every external HTTP call goes through cache.py, so re-runs and corpus-wide
duplicate references are nearly free.
"""
import re
import requests
from typing import Optional

import config
import sources
import grobid_client
from matching import (
    title_similarity,
    normalize_text,
    normalize_doi,
    repair_doi_with_linebreaks,
    repair_pdf_glyphs,
)


# Conference / venue / publisher tokens that strongly imply something is a
# real reference even when GROBID can't extract a clean title. Used by the
# "is this just prose?" heuristic to keep marketing-blurb-shaped citations
# in NOT_FOUND instead of NOT_REFERENCE (where editors never see them).
_VENUE_TOKENS = re.compile(
    r"\b(?:USENIX|ACM|IEEE|NeurIPS|NIPS|ICML|ICLR|CVPR|ECCV|ICCV|AAAI|"
    r"OSDI|SOSP|SIGCOMM|SIGMOD|VLDB|EuroSys|HotOS|HotCloud|ATC|"
    r"WG21|RFC|ISO|IETF|arXiv|Proceedings|Symposium|Conference|"
    r"Journal|Trans\.|Springer|Elsevier|O'Reilly|Wiley|MIT Press|"
    r"PMLR|JMLR)\b",
    re.IGNORECASE,
)


# ----- regex patterns -------------------------------------------------------

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)\b")
ARXIV_NEW_RE = re.compile(r"arxiv\s*[:\s]\s*(\d{4}\.\d{4,5})", re.IGNORECASE)


# ----- helpers --------------------------------------------------------------

def _result(status: str, payload: dict) -> dict:
    return {"status": status, "payload": payload}


def _verified(payload: dict) -> dict:
    return _result("VERIFIED", payload)


def _safe_int(x) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(str(x)[:4])
    except (TypeError, ValueError):
        return None


# ----- the per-candidate scoring (same logic as before, new metric) ---------

def _score_candidate(parsed_title: str, parsed_year, candidate: dict) -> tuple:
    """Returns (status, score, year_gap, candidate_with_optional_note).

    status is one of "VERIFIED", "YEAR_MISMATCH", "FLAWED_REFERENCE", "REJECT".
    """
    found_title = candidate.get("display_name", "") or ""
    score = title_similarity(parsed_title, found_title)

    if score < config.MIN_CONSIDER_SCORE:
        return "REJECT", score, 999, candidate

    # Year gap
    py = _safe_int(parsed_year)
    fy = _safe_int(candidate.get("publication_year"))
    year_gap = abs(py - fy) if (py and fy) else 999

    if score >= config.VERIFY_SCORE:
        if year_gap <= 3:
            note = f"Preprint lag ({year_gap} years)" if year_gap else None
            cand = dict(candidate)
            if note:
                cand["note"] = note
            return "VERIFIED", score, year_gap, cand
        else:
            cand = dict(candidate)
            cand["note"] = f"Edition mismatch (Ref: {parsed_year}, Found: {fy})"
            return "YEAR_MISMATCH", score, year_gap, cand

    if score >= config.FLAWED_SCORE:
        return "FLAWED_REFERENCE", score, year_gap, candidate

    return "REJECT", score, year_gap, candidate


def _search_and_verify(parsed_title: str, parsed_author: str, parsed_year) -> tuple:
    """Run OpenAlex search waterfall + score the top candidates.

    Returns (status, best_match, best_flawed).
    """
    if not parsed_title and not parsed_author:
        return "NOT_FOUND", None, None

    clean_title = re.sub(r"[^a-zA-Z0-9\s]", "", normalize_text(parsed_title or ""))
    clean_author_str = parsed_author or ""
    if isinstance(clean_author_str, list):
        clean_author_str = clean_author_str[0] if clean_author_str else ""
    clean_author = re.sub(r"[^a-zA-Z0-9\s]", "", str(clean_author_str))

    results = []
    if clean_title and clean_author:
        results = sources.search_openalex(title=clean_title, author=clean_author)
    if not results and parsed_title:
        results = sources.search_openalex(general_search=parsed_title)
    if not results and clean_author:
        results = sources.search_openalex(author=clean_author)
    if not results and clean_title:
        results = sources.search_openalex(title=clean_title)

    status, best_match, best_flawed = "NOT_FOUND", None, None
    for cand in results[:20]:
        cstatus, cscore, _, scored = _score_candidate(parsed_title, parsed_year, cand)
        if cstatus == "VERIFIED":
            return "VERIFIED", scored, None
        if cstatus == "YEAR_MISMATCH" and status == "NOT_FOUND":
            status = "YEAR_MISMATCH"
            best_match = scored
        elif cstatus == "FLAWED_REFERENCE" and status == "NOT_FOUND":
            status = "FLAWED_REFERENCE"
            best_flawed = scored

    return status, best_match, best_flawed


def _crossref_search_and_verify(parsed_title: str, parsed_author, parsed_year,
                                ref_string: str, alt_title: str = "",
                                alt_author="") -> tuple:
    """Crossref equivalent of _search_and_verify. Returns the same
    (status, best_match, best_flawed) contract.

    This is the PRIMARY bibliographic search. OpenAlex's ~100 request/day
    credit budget makes it unusable as a primary backend at corpus scale,
    and S2 is throttled to 1 req/s, so Crossref's polite pool is the only
    backend that scales to thousands of PDFs per day.

    Two queries, same rationale as the old Phase 3.5: (1) the GROBID-parsed
    title+author, and (2) the raw reference string. References often come
    out of PDFs with the author list before the title, or a journal name
    where the title should be, so GROBID puts the wrong words in `title`
    and query (1) misses. `query.bibliographic` is built for query (2).

    Acceptance is deliberately two-tier:
      * verbatim title-in-reference OR similarity-vs-raw-reference >=
        VERIFY_SCORE  -> VERIFIED. This is the proven run4 logic and is
        scored against the RAW reference, because the parsed title may
        itself be wrong.
      * otherwise the candidate is run through _score_candidate against the
        parsed title, so Crossref can also produce YEAR_MISMATCH and
        FLAWED_REFERENCE. Without this, demoting OpenAlex would silently
        empty the `edition_mismatch` and `flawed_reference` buckets — the
        two an editor actually reads.
    """
    queries = []
    if parsed_title:
        author_str = parsed_author or ""
        if isinstance(author_str, list):
            author_str = author_str[0] if author_str else ""
        queries.append(f"{parsed_title} {author_str}".strip())
    if alt_title:
        # The other GROBID parse. Neither fulltext nor processCitation
        # dominates, so try both against Crossref (unmetered).
        alt_a = alt_author or ""
        if isinstance(alt_a, list):
            alt_a = alt_a[0] if alt_a else ""
        queries.append(f"{alt_title} {alt_a}".strip())
    if len(ref_string) >= 30:
        queries.append(ref_string[:300])
    if not queries:
        return "NOT_FOUND", None, None

    status, best_match, best_flawed = "NOT_FOUND", None, None
    seen_q = set()
    for q in queries:
        if not q or q in seen_q:
            continue
        seen_q.add(q)
        for cr in sources.search_crossref(q):
            cand = _crossref_to_match(cr, "Verified via Crossref")
            cand_title = cand.get("display_name") or ""
            if len(cand_title) < 8:
                continue

            # Tier 1 — score against the raw reference (proven run4 logic).
            # ref_string is a raw reference, not a title -> guard off
            raw_score = title_similarity(cand_title, ref_string, contained_guard=False)
            if cand_title.lower() in ref_string.lower() or raw_score >= config.VERIFY_SCORE:
                return "VERIFIED", cand, None

            # Tier 2 — score against the parsed title so Crossref can also
            # emit the triage verdicts.
            title_options = [t for t in (parsed_title, alt_title) if t]
            if not title_options:
                continue
            cstatus, scored = "REJECT", cand
            for t in title_options:
                st_, _, _, sc_ = _score_candidate(t, parsed_year, cand)
                if st_ == "VERIFIED":
                    cstatus, scored = st_, sc_
                    break
                if cstatus == "REJECT" and st_ != "REJECT":
                    cstatus, scored = st_, sc_
            if cstatus == "VERIFIED":
                return "VERIFIED", scored, None
            if cstatus == "YEAR_MISMATCH" and status == "NOT_FOUND":
                status, best_match = "YEAR_MISMATCH", scored
            elif cstatus == "FLAWED_REFERENCE" and status == "NOT_FOUND":
                status, best_flawed = "FLAWED_REFERENCE", scored

    return status, best_match, best_flawed


# ----- optional Ollama fallback (only used if explicitly enabled) -----------

def _ollama_parse(reference_string: str) -> Optional[dict]:
    """Last-resort LLM parse for stubborn references. Off by default."""
    if not config.USE_OLLAMA_FALLBACK:
        return None

    prompt = (
        "You are an expert citation parser. Extract: 1. Title 2. First Author Only. 3. Year. "
        "Respond JSON: {title, author, year(int)}. Reference: " + reference_string
    )
    try:
        resp = requests.post(
            f"{config.OLLAMA_HOST}/api/generate",
            json={"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"},
            timeout=30,
        )
        resp.raise_for_status()
        import json
        return json.loads(resp.json().get("response", "{}"))
    except (requests.exceptions.RequestException, ValueError):
        return None


# ----- web-search backstop --------------------------------------------------

def _make_web_match(w: dict, title: str, year, note: str) -> dict:
    """Pack a web search result into the standard match shape (the same
    shape OpenAlex / Crossref / S2 hits use, so downstream code doesn't
    have to special-case web matches)."""
    return {
        "display_name": title or (w.get("title") or ""),
        "publication_year": year,
        "id": w.get("url", "") or "",
        "note": note,
    }


def _web_verify(parsed_title: str, parsed_author, parsed_year, ref_string: str) -> tuple:
    """Final backstop: search the web and confirm the cited work exists.

    Returns one of:
        ("VERIFIED",         match_dict)  — strong, paper confirmed
        ("FLAWED_REFERENCE", match_dict)  — partial match, flag for review
        (None, None)                      — no plausible match, fall through

    Legitimacy is multi-step:
      1. Score every candidate's snippet/title against the parsed title (or
         raw reference if no parsed title is available). High similarity OR
         verbatim title-in-reference → VERIFIED immediately.
      2. For mid-confidence candidates (score >= MIN_CONSIDER_SCORE), GET
         the page and read its <head> metadata. A page that exposes a
         `citation_doi` lets us recurse into the academic-DB DOI lookup —
         the strongest possible signal, since DOI registration is hard to
         fake. Re-scoring against the page's declared <meta og:title>
         catches cases where DDG truncated or rewrote the snippet.
      3. Anything that scores in [FLAWED_SCORE, VERIFY_SCORE) after the
         page fetch becomes FLAWED_REFERENCE — the editor sees it with
         the discrepancy noted instead of it disappearing into NOT_FOUND.
    """
    if not ref_string or len(ref_string) < 20:
        return None, None

    # Build the search query — extracted title preferred, raw ref fallback.
    if parsed_title and len(parsed_title) > 10:
        q = parsed_title
        a = parsed_author
        if isinstance(a, list):
            a = a[0] if a else ""
        if a:
            q = f"{q} {a}"
    else:
        q = ref_string[:250]

    web_results = sources.search_web(q)
    if not web_results:
        return None, None

    # Score against the parsed title when it's clean; against the raw
    # reference when it isn't. The raw-text path catches cases where
    # GROBID extracted the wrong span as the title.
    _cmp_is_title = bool(parsed_title and len(parsed_title) > 8)
    compare_to = parsed_title if _cmp_is_title else ref_string

    best_flawed = None  # (score, match_dict) — kept only if no VERIFIED hit

    for w in web_results:
        rt = (w.get("title") or "").strip()
        if not rt or len(rt) < 8:
            continue

        # Tier 1 — cheap scoring against the DDG result title.
        score = title_similarity(compare_to, rt, contained_guard=_cmp_is_title)
        # Verbatim presence in the reference is corroborating evidence
        # the snippet didn't accidentally win the similarity match.
        if rt.lower() in ref_string.lower() and len(rt) > 12:
            score = max(score, 92)

        if score >= config.VERIFY_SCORE:
            return "VERIFIED", _make_web_match(
                w, rt, None, "Verified via web search (DuckDuckGo)"
            )

        # Below the consider threshold — not worth the page-fetch cost.
        if score < config.MIN_CONSIDER_SCORE:
            continue

        url = w.get("url", "")
        if not url:
            continue

        # Tier 2 — fetch the page and re-evaluate using its <head>.
        md = sources.fetch_url_metadata(url)
        if not md:
            continue

        # If the page exposes a DOI, that's the strongest signal we can
        # ask for. Recurse into the academic-DB DOI path — turns a noisy
        # web hit into a hard ID-based verification.
        if md.get("doi"):
            ndoi = normalize_doi(md["doi"])
            if ndoi:
                hit = sources.lookup_openalex_doi(ndoi)
                if not hit:
                    cr = sources.lookup_crossref_doi(ndoi)
                    if cr:
                        hit = _crossref_to_match(cr, "Verified via DOI from web page")
                if hit:
                    return "VERIFIED", hit

        # Re-score using the page's canonical title (more reliable than
        # the DDG snippet, which can be truncated or rewritten).
        page_title = md.get("title") or rt
        page_year = md.get("year")
        s2 = title_similarity(compare_to, page_title, contained_guard=_cmp_is_title)
        if page_title.lower() in ref_string.lower() and len(page_title) > 12:
            s2 = max(s2, 92)

        if s2 >= config.VERIFY_SCORE:
            return "VERIFIED", _make_web_match(
                w, page_title, page_year,
                "Verified via web search + page metadata",
            )

        if s2 >= config.FLAWED_SCORE:
            cand_match = _make_web_match(
                w, page_title, page_year,
                "Web match — title differs from cited reference",
            )
            if best_flawed is None or s2 > best_flawed[0]:
                best_flawed = (s2, cand_match)

    if best_flawed:
        return "FLAWED_REFERENCE", best_flawed[1]
    return None, None


# ----- main entrypoint ------------------------------------------------------

def check_single_reference(ref_data) -> Optional[dict]:
    """Verify one reference. ref_data is either a raw string OR the dict
    produced by grobid_client.extract_references()."""

    if isinstance(ref_data, str):
        ref_string = ref_data
        g_title, g_author, g_year, g_doi, g_arxiv = "", "", None, None, None
        g_url = None
        is_suspicious = False
    else:
        ref_string = ref_data.get("raw_text", "")
        g_title = ref_data.get("grobid_title", "") or ""
        g_author = ref_data.get("grobid_author", "") or ""
        g_year = ref_data.get("grobid_year")
        g_doi = ref_data.get("grobid_doi")
        g_arxiv = ref_data.get("grobid_arxiv")
        g_url = ref_data.get("grobid_url")
        is_suspicious = ref_data.get("is_suspicious", False)

    # Normalise: strip braces, kill known header pollution, collapse whitespace.
    # repair_pdf_glyphs unmangles separator glyphs (e.g. "2015ś2020" → "2015-2020")
    # and URL-decodes embedded filename fragments before they reach the
    # ASCII-fold step that would otherwise destroy their structure.
    ref_string = repair_pdf_glyphs(ref_string)
    ref_string = ref_string.replace("{", "").replace("}", "")
    ref_string = ref_string.replace("Publication date", "")
    ref_string = re.sub(r"\s+", " ", ref_string).strip()
    if len(ref_string) < 10:
        return _result("NOT_REFERENCE", {"original_reference": ref_string, "note": "Too short"})

    norm_ref = normalize_text(ref_string) and ref_string  # keep readable casing for display
    # for regex/matching purposes use a unicode-folded copy
    fold_ref = (
        ref_string.encode("ascii", "ignore").decode("ascii")
        if ref_string else ""
    )

    # ===========================================================
    # PHASE 0 — Standards sniper
    # ===========================================================
    for checker, source_name in [
        (sources.check_wg21_link, "WG21_LINK"),
        (sources.check_ietf_rfc, "IETF_RFC"),
        (sources.check_isbn, "ISBN"),
    ]:
        match = checker(fold_ref)
        if match:
            return _verified({
                "original_reference": ref_string,
                "parsed_query": {"source": source_name},
                "openalex_match": match,
            })

    # ===========================================================
    # PHASE 1 — DOI / arXiv exact lookup
    # ===========================================================

    # arXiv
    arxiv_id = g_arxiv
    if not arxiv_id:
        m = ARXIV_NEW_RE.search(fold_ref)
        if m:
            arxiv_id = m.group(1)
    if arxiv_id:
        # S2 indexes arXiv natively and is the strongest source for preprints;
        # OpenAlex is tried last because its daily credit budget is tiny.
        match = sources.get_semantic_scholar_paper(f"ARXIV:{arxiv_id}")
        if not match:
            match = sources.lookup_openalex_arxiv(arxiv_id)
        if match:
            return _verified({
                "original_reference": ref_string,
                "parsed_query": {"arxiv": arxiv_id},
                "openalex_match": match,
            })

    # DOI: prefer GROBID's structured one, then regex on the raw text,
    # then a line-break-tolerant repair pass.
    doi_candidates = []
    if g_doi:
        doi_candidates.append(normalize_doi(g_doi))
    m = DOI_RE.search(fold_ref)
    if m:
        doi_candidates.append(normalize_doi(m.group(1)))
    doi_candidates.extend(repair_doi_with_linebreaks(ref_string))
    seen_doi = set()
    for doi in doi_candidates:
        if not doi or doi in seen_doi:
            continue
        seen_doi.add(doi)
        # Crossref IS the DOI registration authority, so it is both the most
        # authoritative and the only unmetered option. OpenAlex last.
        cr = sources.lookup_crossref_doi(doi)
        match = _crossref_to_match(cr, "Verified via Crossref (DOI)") if cr else None
        if not match:
            match = sources.get_semantic_scholar_paper(f"DOI:{doi}")
        if not match:
            match = sources.lookup_openalex_doi(doi)
        if match:
            return _verified({
                "original_reference": ref_string,
                "parsed_query": {"doi": doi},
                "openalex_match": match,
            })

    # ===========================================================
    # PHASE 2 — GROBID processCitation re-parse  (local, unmetered)
    # ===========================================================
    # Moved ahead of every bibliographic search: it is a local GROBID call
    # that costs no API budget, and it produces the best available title /
    # author / year, which every later phase queries with.
    carry_status, carry_match, carry_flawed = "NOT_FOUND", None, None

    parsed = grobid_client.grobid_process_citation(ref_string)
    pc_title, pc_author, pc_year = "", "", None
    if parsed:
        pc_title = parsed.get("title") or ""
        pc_author = parsed.get("author") or ""
        pc_year = parsed.get("year")

        # If processCitation found a DOI/arXiv that the regex missed, resolve
        # it Crossref -> S2 -> OpenAlex, same precedence as Phase 1.
        pc_doi = parsed.get("doi")
        if pc_doi and normalize_doi(pc_doi) not in seen_doi:
            n_pc_doi = normalize_doi(pc_doi)
            cr = sources.lookup_crossref_doi(n_pc_doi)
            match = _crossref_to_match(cr, "Verified via Crossref (DOI)") if cr else None
            if not match:
                match = sources.get_semantic_scholar_paper(f"DOI:{n_pc_doi}")
            if not match:
                match = sources.lookup_openalex_doi(n_pc_doi)
            if match:
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"doi": pc_doi, "source": "GROBID_processCitation"},
                    "openalex_match": match,
                })
        pc_arxiv = parsed.get("arxiv")
        if pc_arxiv and pc_arxiv != arxiv_id:
            match = sources.get_semantic_scholar_paper(f"ARXIV:{pc_arxiv}")
            if not match:
                match = sources.lookup_openalex_arxiv(pc_arxiv)
            if match:
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"arxiv": pc_arxiv, "source": "GROBID_processCitation"},
                    "openalex_match": match,
                })

    # Prefer the FULLTEXT parse over the processCitation parse.
    #
    # This precedence used to read `pc_title or g_title`, which was harmless
    # only because processCitation was silently broken (see §13) and pc_title
    # was always "". Once it started working, the isolated-string parse began
    # overriding the fulltext parse — and it is frequently worse, because it
    # lacks document context:
    #
    #   g_title: "QEMU, a Fast and Portable Dynamic Translator"
    #   pc_title:"Fast and Portable Dynamic Translator Fabrice Bellard"
    #   g_title: "Computer Systems: A Programmer's Perspective"
    #   pc_title:"Computer Systems: A Programmer's Perspective 2022"
    #
    # It glues authors/years on and drops leading tokens. Neither parse
    # dominates though — g_title "MIT JOS"/"MIT 6" vs pc_title
    # "MIT 6 Operating System Engineering" goes the other way — so pc_* is
    # kept as the fallback and as an alternate query below, rather than
    # discarded. pc_doi / pc_arxiv are unaffected and remain a clear win.
    best_title_for_query = g_title or pc_title
    best_author_for_query = g_author or pc_author
    best_year_for_query = g_year or pc_year

    # The other parse, when it genuinely differs — used as an extra Crossref
    # query (Crossref is unmetered, so this is close to free). Not sent to
    # S2, which is throttled to 1 req/s.
    alt_title = pc_title if (pc_title and pc_title != best_title_for_query) else ""
    alt_author = pc_author if alt_title else ""

    # ===========================================================
    # PHASE 3 — Crossref bibliographic search  (PRIMARY)
    # ===========================================================
    # Crossref leads because it is the only backend without a hard daily
    # cap: OpenAlex allows ~100 credits/day and S2 is throttled to 1 req/s,
    # neither of which survives a thousand-PDF day. Crossref also emits the
    # YEAR_MISMATCH / FLAWED_REFERENCE verdicts now, so demoting OpenAlex
    # no longer empties those buckets.
    status, match, flawed = _crossref_search_and_verify(
        best_title_for_query, best_author_for_query, best_year_for_query,
        ref_string, alt_title, alt_author
    )
    if status == "VERIFIED":
        return _verified({
            "original_reference": ref_string,
            "parsed_query": {
                "title": best_title_for_query or "(raw)",
                "source": "CROSSREF",
            },
            "openalex_match": match,
        })
    if status == "YEAR_MISMATCH":
        carry_status, carry_match = status, match
    elif status == "FLAWED_REFERENCE":
        carry_status, carry_flawed = status, flawed

    # ===========================================================
    # PHASE 4 — Semantic Scholar search backstop
    # ===========================================================
    # Same two-query approach as Phase 3: try title+author first, then the
    # raw reference. S2's search is forgiving of free-form bibliographic
    # input so the raw-string query frequently hits where structured search
    # misses.
    #
    # NOTE ON SCALE: search_semantic_scholar() goes through _s2_throttle(),
    # a global 1 req/s serial gate. These two queries are therefore the
    # slowest thing in the pipeline. They only run for references Crossref
    # already failed on, which is what keeps that affordable — do not
    # promote S2 above Crossref.
    s2_queries = []
    if best_title_for_query:
        q1 = f"{best_title_for_query} {best_author_for_query}".strip()
        if len(q1) >= 15:
            s2_queries.append(q1)
    if len(ref_string) >= 30:
        s2_queries.append(ref_string[:300])

    for s2_q in s2_queries:
        s2_match = sources.search_semantic_scholar(s2_q)
        if not s2_match:
            continue
        s2_title = s2_match.get("display_name", "") or ""
        if not s2_title or len(s2_title) < 8:
            continue
        score = title_similarity(s2_title, ref_string, contained_guard=False)
        title_in_ref = s2_title.lower() in ref_string.lower()
        if title_in_ref or score >= config.VERIFY_SCORE:
            return _verified({
                "original_reference": ref_string,
                "parsed_query": {"title": best_title_for_query or "(raw)", "source": "S2"},
                "openalex_match": s2_match,
            })

    # ===========================================================
    # PHASE 5 — OpenAlex structured search  (OPPORTUNISTIC BACKUP)
    # ===========================================================
    # Was Phase 2/3, i.e. the primary backend. Demoted here because the
    # OpenAlex credit model allows only ~100 requests/day, and this function
    # issues up to four calls per invocation. When the budget is spent,
    # search_openalex() short-circuits on the _openalex_budget_exhausted
    # latch and this phase costs nothing, so it is safe to leave enabled:
    # on a small run it still adds matches, on a large run it self-disables
    # after the first 429.
    for _oa_title, _oa_author, _oa_year, _oa_src in (
        (pc_title, pc_author, pc_year, "GROBID_processCitation"),
        (g_title, g_author, g_year, "GROBID"),
    ):
        if not _oa_title or len(_oa_title) <= 5:
            continue
        status, match, flawed = _search_and_verify(_oa_title, _oa_author, _oa_year)
        if status == "VERIFIED":
            return _verified({
                "original_reference": ref_string,
                "parsed_query": {"title": _oa_title, "source": _oa_src},
                "openalex_match": match,
            })
        if status == "YEAR_MISMATCH" and carry_status != "YEAR_MISMATCH":
            carry_status, carry_match = status, match
        elif status == "FLAWED_REFERENCE" and carry_status == "NOT_FOUND":
            carry_status, carry_flawed = status, flawed

    # ===========================================================
    # PHASE 5.25 — OpenAlex general-search backstop with raw reference
    # ===========================================================
    # Always run, regardless of whether GROBID extracted a title in earlier
    # phases. The reason: when references are formatted as "[authors]
    # [arxiv-id] [title] [year]" or "[title] [authors] [venue] [year]"
    # without clear separators, GROBID frequently extracts an author name
    # OR the journal name as the "title" — so Phase 2's structured search
    # fires with the wrong query and misses. The raw-text /works?search=
    # endpoint is bibliographic-string aware and finds these.
    #
    # We score the candidate against the raw reference (not the extracted
    # title) because the parsed title is potentially wrong. We accept the
    # match only if the candidate's title appears verbatim in the raw
    # reference OR the similarity is above VERIFY_SCORE — both checks
    # protect against false positives.
    if len(ref_string) >= 30:
        oa_results = sources.search_openalex(general_search=ref_string[:300])
        for cand in oa_results[:10]:
            cand_title = cand.get("display_name", "") or ""
            if not cand_title or len(cand_title) < 8:
                continue
            score = title_similarity(cand_title, ref_string, contained_guard=False)
            title_in_ref = cand_title.lower() in ref_string.lower()
            if title_in_ref or score >= config.VERIFY_SCORE:
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"title": "(raw)", "source": "OPENALEX_GENERAL"},
                    "openalex_match": cand,
                })

    # ===========================================================
    # PHASE 5.5 — Ollama fallback (only if explicitly enabled)
    # ===========================================================
    if config.USE_OLLAMA_FALLBACK and not best_title_for_query:
        parsed_ollama = _ollama_parse(ref_string)
        if parsed_ollama and parsed_ollama.get("title"):
            o_title = parsed_ollama.get("title")
            o_author = parsed_ollama.get("author", "")
            o_year = parsed_ollama.get("year")
            status, match, flawed = _search_and_verify(o_title, o_author, o_year)
            if status == "VERIFIED":
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"title": o_title, "source": "OLLAMA"},
                    "openalex_match": match,
                })

    # ===========================================================
    # PHASE 5.8 — Cited URL, fetched directly
    # ===========================================================
    # The reference's own URL, harvested from GROBID's <ptr target="...">.
    # This runs before the web SEARCH because it needs no search engine: we
    # already know exactly which page the author cited, so we just fetch it
    # and check that it describes the cited work.
    #
    # This is the only viable path for the grey literature that dominates
    # not_found — software projects, datasets, standards, industry reports —
    # none of which any academic database indexes. For those, the cited page
    # is a MORE authoritative source than Crossref would be.
    #
    # arXiv and doi.org links never reach here: grobid_client folds them into
    # grobid_arxiv / grobid_doi so Phase 1 resolves them as exact IDs.
    if g_url:
        md = sources.fetch_url_metadata(g_url)
        if md:
            # Strongest signal: the page declares its own DOI. Resolve it
            # against the academic DBs — a registered DOI is hard to fake.
            md_doi = normalize_doi(md.get("doi") or "")
            if md_doi and md_doi not in seen_doi:
                cr = sources.lookup_crossref_doi(md_doi)
                hit = _crossref_to_match(cr, "Verified via DOI from cited URL") if cr else None
                if not hit:
                    hit = sources.get_semantic_scholar_paper(f"DOI:{md_doi}")
                if hit:
                    return _verified({
                        "original_reference": ref_string,
                        "parsed_query": {"doi": md_doi, "source": "CITED_URL_DOI"},
                        "openalex_match": hit,
                    })

            page_title = (md.get("title") or "").strip()
            if len(page_title) >= 8:
                compare_to = (best_title_for_query
                              if best_title_for_query and len(best_title_for_query) > 8
                              else ref_string)
                guard = bool(best_title_for_query and len(best_title_for_query) > 8)
                score = title_similarity(compare_to, page_title, contained_guard=guard)
                if page_title.lower() in ref_string.lower() and len(page_title) > 12:
                    score = max(score, 92)
                match = _make_web_match(
                    {"url": g_url}, page_title, md.get("year"),
                    "Verified via the URL cited in the reference",
                )
                if score >= config.VERIFY_SCORE:
                    return _verified({
                        "original_reference": ref_string,
                        "parsed_query": {
                            "title": best_title_for_query or "(raw)",
                            "source": "CITED_URL",
                        },
                        "openalex_match": match,
                    })
                if (score >= config.FLAWED_SCORE
                        and carry_status not in ("YEAR_MISMATCH", "FLAWED_REFERENCE")):
                    carry_status, carry_flawed = "FLAWED_REFERENCE", match

    # ===========================================================
    # PHASE 5.9 — Web search + page-metadata backstop
    # ===========================================================
    # Final attempt before bucketing as NOT_FOUND / NOT_REFERENCE. Catches
    # references that legitimately exist but live outside academic DBs:
    # news articles (NYT / Ars Technica / The Register coverage of OpenAI
    # lawsuits), NIST Special Publications, software project pages
    # (SPHINCS+, Open Quantum Safe, LeetCode), GitHub READMEs, blog posts.
    # The two-tier match (snippet → page metadata → academic-DB DOI) is
    # what keeps this from introducing false positives.
    web_kind, web_match = _web_verify(
        pc_title or g_title, pc_author or g_author, pc_year or g_year, ref_string
    )
    if web_kind == "VERIFIED" and web_match:
        return _verified({
            "original_reference": ref_string,
            "parsed_query": {
                "title": (pc_title or g_title) or "(raw)",
                "source": "WEB_SEARCH",
            },
            "openalex_match": web_match,
        })
    # FLAWED_REFERENCE from the web only wins if no academic phase already
    # gave us a YEAR_MISMATCH or a stronger flawed candidate to report.
    if (web_kind == "FLAWED_REFERENCE" and web_match
            and carry_status not in ("YEAR_MISMATCH", "FLAWED_REFERENCE")):
        carry_status = "FLAWED_REFERENCE"
        carry_flawed = web_match

    # ===========================================================
    # PHASE 6 — terminal: report carry status, NOT_FOUND, or NOT_REFERENCE
    # ===========================================================

    if carry_status == "YEAR_MISMATCH" and carry_match:
        return _result("YEAR_MISMATCH", {
            "original_reference": ref_string,
            "parsed_query": {"title": best_title_for_query},
            "openalex_match (edition mismatch)": carry_match,
        })

    if carry_status == "FLAWED_REFERENCE" and carry_flawed:
        return _result("FLAWED_REFERENCE", {
            "original_reference": ref_string,
            "parsed_query": {"title": best_title_for_query},
            "openalex_match (mismatched)": carry_flawed,
        })

    # Genuinely failed all checks. Was this even a reference?
    if is_suspicious:
        return _result("NOT_REFERENCE", {
            "original_reference": ref_string,
            "note": "Heuristically flagged as non-reference; no DB matches found",
        })

    return _result("NOT_FOUND", {
        "original_reference": ref_string,
        "parsed_query": {"title": best_title_for_query},
    })


def _crossref_to_match(cr_item: dict, note: str) -> dict:
    """Convert a Crossref API item to our standard match shape."""
    title = ""
    titles = cr_item.get("title") or []
    if titles:
        title = titles[0]
    year = None
    try:
        year = cr_item.get("issued", {}).get("date-parts", [[None]])[0][0]
        if not year:
            year = cr_item.get("published", {}).get("date-parts", [[None]])[0][0]
    except (TypeError, IndexError):
        pass
    return {
        "display_name": title,
        "publication_year": year,
        "id": cr_item.get("URL", ""),
        "note": note,
    }
