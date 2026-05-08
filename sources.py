"""
External-source lookups. Every HTTP call here goes through the persistent
SQLite cache (see cache.py), so re-running on the same corpus is fast and you
can tune verification thresholds without re-paying API costs.

Sources:
  - OpenAlex     (primary academic DB)
  - Crossref     (DOI backstop, parallel coverage)
  - Semantic Scholar (grey literature, datasets, AI/CS preprints)
  - WG21         (C++ standards committee papers)
  - IETF         (RFCs)
  - OpenLibrary  (books, by ISBN)
"""
import re
import time
import requests
from typing import Optional

import config
from cache import get_cache


_HEADERS = {
    "User-Agent": (
        "ReferenceChecker/2.0 (mailto:" + (config.OPENALEX_EMAIL or "anonymous") + ")"
    )
}


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

def search_openalex(
    title: Optional[str] = None,
    author: Optional[str] = None,
    year: Optional[int] = None,
    general_search: Optional[str] = None,
) -> list:
    base_url = "https://api.openalex.org/works"
    params = {
        "select": "id,display_name,authorships,publication_year,doi",
        "mailto": config.OPENALEX_EMAIL,
        "per-page": 25,
    }

    if general_search:
        params["search"] = general_search
    else:
        filters = []
        if title:
            filters.append(f"title.search:{title}")
        if author:
            filters.append(f"raw_author_name.search:{author}")
        if year:
            filters.append(f"publication_year:{year}")
        if not filters:
            return []
        params["filter"] = ",".join(filters)

    cache_query = {"endpoint": "openalex.search", **params}

    def _fetch():
        try:
            resp = requests.get(base_url, params=params, headers=_HEADERS, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("results", [])
            return []
        except requests.exceptions.RequestException:
            return []

    return get_cache().get_or_set("openalex_search", cache_query, _fetch) or []


def lookup_openalex_doi(doi: str) -> Optional[dict]:
    if not doi:
        return None
    cache_query = {"endpoint": "openalex.doi", "doi": doi}

    def _fetch():
        url = "https://api.openalex.org/works"
        params = {"filter": f"doi:https://doi.org/{doi}", "mailto": config.OPENALEX_EMAIL}
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=10)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                return results[0] if results else None
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("openalex_doi", cache_query, _fetch)


def lookup_openalex_arxiv(arxiv_id: str) -> Optional[dict]:
    if not arxiv_id:
        return None
    cache_query = {"endpoint": "openalex.arxiv", "arxiv": arxiv_id}

    def _fetch():
        url = "https://api.openalex.org/works"
        params = {"filter": f"ids.arxiv:{arxiv_id}", "mailto": config.OPENALEX_EMAIL}
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=10)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                return results[0] if results else None
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("openalex_arxiv", cache_query, _fetch)


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------

def search_crossref(query: str) -> Optional[dict]:
    if not query or len(query) < 10:
        return None

    cache_query = {"endpoint": "crossref.search", "q": query[:300]}

    def _fetch():
        try:
            resp = requests.get(
                "https://api.crossref.org/works",
                params={
                    "query.bibliographic": query,
                    "rows": 1,
                    "mailto": config.OPENALEX_EMAIL or "anonymous@example.com",
                },
                headers=_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                items = resp.json().get("message", {}).get("items", [])
                return items[0] if items else None
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("crossref_search", cache_query, _fetch)


def lookup_crossref_doi(doi: str) -> Optional[dict]:
    """Direct DOI lookup against Crossref."""
    if not doi:
        return None
    cache_query = {"endpoint": "crossref.doi", "doi": doi}

    def _fetch():
        try:
            resp = requests.get(
                f"https://api.crossref.org/works/{doi}",
                headers=_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("message")
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("crossref_doi", cache_query, _fetch)


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

# S2 free tier is strict: 100 req / 5 min. We rate-limit gently to avoid 429s.
_s2_last_call = [0.0]


def _s2_throttle():
    elapsed = time.time() - _s2_last_call[0]
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _s2_last_call[0] = time.time()


def _s2_headers():
    return {**_HEADERS, **({"x-api-key": config.S2_API_KEY} if config.S2_API_KEY else {})}


def get_semantic_scholar_paper(paper_id: str) -> Optional[dict]:
    """paper_id e.g. 'DOI:10.1145/...' or 'ARXIV:1705.103'."""
    if not paper_id:
        return None
    cache_query = {"endpoint": "s2.paper", "id": paper_id}

    def _fetch():
        _s2_throttle()
        try:
            resp = requests.get(
                f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}",
                params={"fields": "title,authors,year,url,externalIds"},
                headers=_s2_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                item = resp.json()
                if not item.get("title"):
                    return None
                return {
                    "display_name": item.get("title"),
                    "publication_year": item.get("year"),
                    "id": item.get("url"),
                    "note": "Verified via Semantic Scholar (ID match)",
                }
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("s2_paper", cache_query, _fetch)


def search_semantic_scholar(query: str) -> Optional[dict]:
    if not query or len(query) < 10:
        return None
    cache_query = {"endpoint": "s2.search", "q": query[:300]}

    def _fetch():
        _s2_throttle()
        try:
            resp = requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": query, "limit": 1, "fields": "title,authors,year,url,externalIds"},
                headers=_s2_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("data"):
                    item = data["data"][0]
                    return {
                        "display_name": item.get("title"),
                        "publication_year": item.get("year"),
                        "id": item.get("url"),
                        "note": "Verified via Semantic Scholar",
                    }
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("s2_search", cache_query, _fetch)


# ---------------------------------------------------------------------------
# WG21 (C++ standards papers)
# ---------------------------------------------------------------------------

_WG21_RE = re.compile(r"\b([NP])\s*(\d{4}(?:R\d+)?)\b", re.IGNORECASE)


def check_wg21_link(ref_string: str) -> Optional[dict]:
    m = _WG21_RE.search(ref_string)
    if not m:
        return None
    paper_id = f"{m.group(1)}{m.group(2)}".upper()
    cache_query = {"endpoint": "wg21", "id": paper_id}

    def _fetch():
        url = f"https://wg21.link/{paper_id}"
        try:
            resp = requests.head(url, allow_redirects=True, timeout=10)
            if resp.status_code == 200:
                return {
                    "display_name": f"C++ Standard Paper {paper_id}",
                    "publication_year": None,
                    "id": url,
                    "note": "Verified via WG21.link",
                }
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("wg21", cache_query, _fetch)


# ---------------------------------------------------------------------------
# IETF (RFCs)
# ---------------------------------------------------------------------------

_RFC_RE = re.compile(r"\bRFC[\s\-]?(\d{1,5})\b", re.IGNORECASE)


def check_ietf_rfc(ref_string: str) -> Optional[dict]:
    m = _RFC_RE.search(ref_string)
    if not m:
        return None
    rfc_id = m.group(1)
    cache_query = {"endpoint": "ietf", "id": rfc_id}

    def _fetch():
        url = f"https://datatracker.ietf.org/doc/rfc{rfc_id}/"
        try:
            resp = requests.head(url, timeout=10)
            if resp.status_code == 200:
                return {
                    "display_name": f"IETF RFC {rfc_id}",
                    "publication_year": None,
                    "id": url,
                    "note": "Verified via IETF Datatracker",
                }
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("ietf", cache_query, _fetch)


# ---------------------------------------------------------------------------
# ISBN (books)
# ---------------------------------------------------------------------------

_ISBN_RE = re.compile(
    r"\b(?:ISBN(?:[:\s]+))?((?:978|979)[\s\-0-9]{10,17})\b",
    re.IGNORECASE,
)


def check_isbn(ref_string: str) -> Optional[dict]:
    m = _ISBN_RE.search(ref_string)
    if not m:
        return None
    raw_isbn = re.sub(r"[\s\-]", "", m.group(1))
    if len(raw_isbn) != 13:
        return None

    cache_query = {"endpoint": "openlibrary", "isbn": raw_isbn}

    def _fetch():
        url = f"https://openlibrary.org/isbn/{raw_isbn}.json"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "display_name": data.get("title", f"Book (ISBN {raw_isbn})"),
                    "publication_year": data.get("publish_date", "N/A"),
                    "id": f"https://openlibrary.org/isbn/{raw_isbn}",
                    "note": "Verified via OpenLibrary (Book)",
                }
        except requests.exceptions.RequestException:
            pass
        return None

    return get_cache().get_or_set("openlibrary", cache_query, _fetch)
