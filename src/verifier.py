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
  Phase 4.5— OPTIONAL Ollama fallback (only if USE_OLLAMA_FALLBACK=true)
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


# ----- main entrypoint ------------------------------------------------------

def check_single_reference(ref_data) -> Optional[dict]:
    """Verify one reference. ref_data is either a raw string OR the dict
    produced by grobid_client.extract_references()."""

    if isinstance(ref_data, str):
        ref_string = ref_data
        g_title, g_author, g_year, g_doi, g_arxiv = "", "", None, None, None
        is_suspicious = False
    else:
        ref_string = ref_data.get("raw_text", "")
        g_title = ref_data.get("grobid_title", "") or ""
        g_author = ref_data.get("grobid_author", "") or ""
        g_year = ref_data.get("grobid_year")
        g_doi = ref_data.get("grobid_doi")
        g_arxiv = ref_data.get("grobid_arxiv")
        is_suspicious = ref_data.get("is_suspicious", False)

    # Normalise: strip braces, kill known header pollution, collapse whitespace
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
        match = sources.lookup_openalex_arxiv(arxiv_id)
        if not match:
            match = sources.get_semantic_scholar_paper(f"ARXIV:{arxiv_id}")
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
        match = sources.lookup_openalex_doi(doi)
        if not match:
            cr = sources.lookup_crossref_doi(doi)
            if cr:
                match = _crossref_to_match(cr, "Verified via Crossref (DOI)")
        if not match:
            match = sources.get_semantic_scholar_paper(f"DOI:{doi}")
        if match:
            return _verified({
                "original_reference": ref_string,
                "parsed_query": {"doi": doi},
                "openalex_match": match,
            })

    # ===========================================================
    # PHASE 2 — GROBID structured fields → search & verify
    # ===========================================================
    if g_title and len(g_title) > 5:
        status, match, flawed = _search_and_verify(g_title, g_author, g_year)
        if status == "VERIFIED":
            return _verified({
                "original_reference": ref_string,
                "parsed_query": {"title": g_title, "source": "GROBID"},
                "openalex_match": match,
            })
        # carry these forward as fallbacks if Phase 3 also fails
        carry_status, carry_match, carry_flawed = status, match, flawed
    else:
        carry_status, carry_match, carry_flawed = "NOT_FOUND", None, None

    # ===========================================================
    # PHASE 3 — GROBID processCitation re-parse  (replaces Ollama)
    # ===========================================================
    parsed = grobid_client.grobid_process_citation(ref_string)
    pc_title, pc_author, pc_year = "", "", None
    if parsed:
        pc_title = parsed.get("title") or ""
        pc_author = parsed.get("author") or ""
        pc_year = parsed.get("year")

        # If processCitation found a DOI/arXiv that the regex missed:
        pc_doi = parsed.get("doi")
        if pc_doi and pc_doi not in seen_doi:
            match = sources.lookup_openalex_doi(normalize_doi(pc_doi))
            if match:
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"doi": pc_doi, "source": "GROBID_processCitation"},
                    "openalex_match": match,
                })
        pc_arxiv = parsed.get("arxiv")
        if pc_arxiv and pc_arxiv != arxiv_id:
            match = sources.lookup_openalex_arxiv(pc_arxiv)
            if match:
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"arxiv": pc_arxiv, "source": "GROBID_processCitation"},
                    "openalex_match": match,
                })

        if pc_title:
            status, match, flawed = _search_and_verify(pc_title, pc_author, pc_year)
            if status == "VERIFIED":
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"title": pc_title, "source": "GROBID_processCitation"},
                    "openalex_match": match,
                })
            # bubble up into carry if better than what we have
            if status == "YEAR_MISMATCH" and carry_status != "YEAR_MISMATCH":
                carry_status, carry_match = status, match
            elif status == "FLAWED_REFERENCE" and carry_status == "NOT_FOUND":
                carry_status, carry_flawed = status, flawed

    # ===========================================================
    # PHASE 3.5 — Crossref bibliographic-search backstop
    # ===========================================================
    best_title_for_query = pc_title or g_title
    best_author_for_query = pc_author or g_author
    if best_title_for_query:
        cr = sources.search_crossref(f"{best_title_for_query} {best_author_for_query}".strip())
        if cr:
            cr_title = (cr.get("title") or [""])[0] or ""
            score = title_similarity(best_title_for_query, cr_title)
            if score >= config.VERIFY_SCORE:
                cr_year = None
                try:
                    cr_year = cr.get("published", {}).get("date-parts", [[None]])[0][0]
                except (TypeError, IndexError):
                    pass
                match = {
                    "display_name": cr_title,
                    "publication_year": cr_year,
                    "id": cr.get("URL", "No URL"),
                    "note": "Verified via Crossref (Backstop)",
                }
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"title": best_title_for_query, "source": "CROSSREF"},
                    "openalex_match": match,
                })

    # ===========================================================
    # PHASE 4 — Semantic Scholar search backstop
    # ===========================================================
    if best_title_for_query:
        s2_query = f"{best_title_for_query} {best_author_for_query}".strip()
        if len(s2_query) < 15:
            s2_query = ref_string[:200]
        s2_match = sources.search_semantic_scholar(s2_query)
        if s2_match:
            s2_title = s2_match.get("display_name", "")
            score = title_similarity(best_title_for_query, s2_title)
            title_in_ref = bool(s2_title) and s2_title.lower() in ref_string.lower()
            if score >= config.VERIFY_SCORE or title_in_ref:
                return _verified({
                    "original_reference": ref_string,
                    "parsed_query": {"title": best_title_for_query, "source": "S2"},
                    "openalex_match": s2_match,
                })

    # ===========================================================
    # PHASE 4.5 — Ollama fallback (only if explicitly enabled)
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
    # PHASE 5 — terminal: report carry status, NOT_FOUND, or NOT_REFERENCE
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
