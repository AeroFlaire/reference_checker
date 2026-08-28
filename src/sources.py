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
  - DuckDuckGo HTML + page metadata (final web backstop — see search_web /
                     fetch_url_metadata at the bottom of the file)
"""
import html as _html_lib
import re
import time
import urllib.parse
import requests
from typing import Optional

from lxml import etree as _lxml_etree
from lxml import html as _lxml_html

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
    """Find an arXiv preprint in OpenAlex.

    OpenAlex does NOT expose an `ids.arxiv` filter (the documented ones are
    openalex/doi/mag/pmid/pmcid only). Instead, arXiv-issued DOIs use the
    `10.48550/arXiv.<id>` prefix that arXiv has minted on every paper since
    2022 and retroactively on many earlier ones — so we try that DOI form
    first. As a backstop, fall through to a free-text search of the bare ID
    (OpenAlex indexes arXiv IDs in work metadata, so this often hits).
    """
    if not arxiv_id:
        return None
    cache_query = {"endpoint": "openalex.arxiv", "arxiv": arxiv_id}

    def _fetch():
        url = "https://api.openalex.org/works"
        # Strategy 1: arXiv DOI prefix (works for the vast majority post-2022
        # and most retroactive grants).
        for filt in (
            f"doi:https://doi.org/10.48550/arXiv.{arxiv_id}",
            f"doi:10.48550/arXiv.{arxiv_id}",
        ):
            try:
                resp = requests.get(
                    url,
                    params={"filter": filt, "mailto": config.OPENALEX_EMAIL},
                    headers=_HEADERS,
                    timeout=10,
                )
                if resp.status_code == 200:
                    results = resp.json().get("results", [])
                    if results:
                        return results[0]
            except requests.exceptions.RequestException:
                pass

        # Strategy 2: free-text search for the bare ID. OpenAlex indexes
        # arXiv IDs in work metadata, so a search like "1507.08685" usually
        # surfaces the paper as the top hit.
        try:
            resp = requests.get(
                url,
                params={"search": arxiv_id, "mailto": config.OPENALEX_EMAIL, "per-page": 5},
                headers=_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                # Only accept if the top hit clearly references this arXiv
                # ID — guard against unrelated "search hit on a number"
                # false positives.
                for r in results[:3]:
                    ids = r.get("ids") or {}
                    blob = " ".join(str(v) for v in ids.values())
                    if arxiv_id in blob:
                        return r
        except requests.exceptions.RequestException:
            pass

        return None

    return get_cache().get_or_set("openalex_arxiv", cache_query, _fetch)


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------

def search_crossref(query: str, rows: int = 5) -> list:
    """Bibliographic-search Crossref. Returns a list of items (was: just the
    top hit) so the verifier can score multiple candidates against the raw
    reference — Crossref's ranking is decent but not perfect, and the
    correct answer is occasionally at rank 2 or 3."""
    if not query or len(query) < 10:
        return []

    cache_query = {"endpoint": "crossref.search", "q": query[:300], "rows": rows}

    def _fetch():
        try:
            resp = requests.get(
                "https://api.crossref.org/works",
                params={
                    "query.bibliographic": query,
                    "rows": rows,
                    "mailto": config.OPENALEX_EMAIL or "anonymous@example.com",
                },
                headers=_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("message", {}).get("items", []) or []
        except requests.exceptions.RequestException:
            pass
        return []

    return get_cache().get_or_set("crossref_search", cache_query, _fetch) or []


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


# ---------------------------------------------------------------------------
# Web search + page-metadata harvest (final non-academic backstop)
# ---------------------------------------------------------------------------
#
# Why this exists: a non-trivial fraction of references in real corpora live
# entirely outside academic databases — news articles ("Authors Sue OpenAI"
# — NYT, Ars Technica), NIST Special Publications, IETF drafts, software
# project pages (SPHINCS+, Open Quantum Safe, LeetCode), GitHub READMEs,
# blog posts. None of those are in OpenAlex / Crossref / S2, so they get
# bucketed as NOT_FOUND despite being trivially verifiable on the web.
#
# Two pieces below:
#   search_web()         — DuckDuckGo HTML scrape (no API key required).
#   fetch_url_metadata() — GET a candidate URL and harvest <title>, <meta
#                          og:title>, <meta citation_doi>, etc. Used by the
#                          verifier to confirm a hit is the real article
#                          rather than a coincidentally-matching snippet.

# A recent Firefox UA — DDG serves a CAPTCHA page to obvious Python/curl
# UAs. One real-browser string is enough; no need to rotate.
_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
    "Gecko/20100101 Firefox/124.0"
)

# URLs ending in these extensions are downloads, not articles. Fetching
# them is a waste and risks pulling MB-scale binaries into the parser.
_BINARY_URL_EXTS = (
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".zip", ".gz", ".tar", ".rar", ".7z",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".mp3", ".mp4", ".mov", ".avi", ".webm",
)


def search_web(query: str, num_results: int = 8) -> list:
    """Free-text web search via DuckDuckGo HTML.

    Returns a list of ``{"title", "snippet", "url"}`` dicts. Empty list
    means DDG answered with no usable results; callers cannot tell that
    apart from a network failure (we cache empties as the genuine answer).
    """
    if not query or len(query) < 10:
        return []
    cache_query = {"endpoint": "ddg.html", "q": query[:300]}

    def _fetch():
        # GET first, POST as fallback — different DDG mirrors / proxies
        # accept different methods, and silently returning empty on a
        # transient failure would poison the cache for CACHE_TTL_DAYS.
        body = None
        for method in ("get", "post"):
            try:
                if method == "get":
                    resp = requests.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": query[:300]},
                        headers={
                            "User-Agent": _WEB_UA,
                            "Accept": "text/html,application/xhtml+xml",
                            "Accept-Language": "en-US,en;q=0.5",
                        },
                        timeout=12,
                    )
                else:
                    resp = requests.post(
                        "https://html.duckduckgo.com/html/",
                        data={"q": query[:300]},
                        headers={
                            "User-Agent": _WEB_UA,
                            "Accept": "text/html,application/xhtml+xml",
                            "Accept-Language": "en-US,en;q=0.5",
                        },
                        timeout=12,
                    )
            except requests.exceptions.RequestException:
                continue
            if resp.status_code == 200 and resp.content:
                body = resp.content
                break

        if not body:
            # Don't cache transient HTTP/network failures — `None` is
            # excluded from the cache by get_or_set, so the next run
            # retries instead of returning a stale empty list.
            return None

        # DDG returns HTTP 200 even for its anti-bot anomaly page. Detect
        # the marker text and treat as a transient failure.
        if b"anomaly-modal" in body or b"DDG-anomaly" in body:
            return None

        try:
            tree = _lxml_html.fromstring(body)
        except (ValueError, _lxml_etree.LxmlError):
            return None

        results = []
        for r in tree.xpath(
            "//div[contains(@class,'result__body') "
            "or contains(@class,'web-result')]"
        ):
            t_nodes = r.xpath(".//a[contains(@class,'result__a')]//text()")
            u_nodes = r.xpath(".//a[contains(@class,'result__a')]/@href")
            s_nodes = r.xpath(
                ".//*[contains(@class,'result__snippet')]//text() | "
                ".//*[contains(@class,'result-snippet')]//text()"
            )
            title = _html_lib.unescape(" ".join(s.strip() for s in t_nodes)).strip()
            snippet = _html_lib.unescape(" ".join(s.strip() for s in s_nodes)).strip()
            url = u_nodes[0] if u_nodes else ""
            # DDG wraps outbound links through `/l/?uddg=<encoded>`. Decode
            # so the verified-ref entry stores a real article URL.
            if url.startswith("//"):
                url = "https:" + url
            if "duckduckgo.com/l/" in url or url.startswith("/l/"):
                m = re.search(r"[?&]uddg=([^&]+)", url)
                if m:
                    url = urllib.parse.unquote(m.group(1))
            if title:
                results.append({"title": title, "snippet": snippet, "url": url})
            if len(results) >= num_results:
                break
        return results

    return get_cache().get_or_set("ddg_html", cache_query, _fetch) or []


# Meta-tag name preferences for fetch_url_metadata. Ordered most-canonical
# first; we take the first non-empty value found.
_META_TITLE_NAMES  = ("citation_title", "og:title", "twitter:title",
                      "dc.title", "dcterms.title", "parsely-title")
_META_DOI_NAMES    = ("citation_doi", "dc.identifier", "prism.doi")
_META_AUTHOR_NAMES = ("citation_author", "author", "dc.creator", "article:author")
_META_DATE_NAMES   = ("citation_publication_date", "citation_date",
                      "article:published_time", "dc.date", "dcterms.issued")


def _meta_lookup(tree, names) -> str:
    """First non-empty <meta name=...> or <meta property=...> content,
    matching case-insensitively. XPath 1.0 has no `lower-case()`, so we
    use the standard translate() trick."""
    for name in names:
        n = name.lower()
        xp = (
            "//meta[("
            "translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            f"'abcdefghijklmnopqrstuvwxyz')='{n}' or "
            "translate(@property,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            f"'abcdefghijklmnopqrstuvwxyz')='{n}'"
            ")]/@content"
        )
        for v in tree.xpath(xp):
            v = (v or "").strip()
            if v:
                return v
    return ""


def fetch_url_metadata(url: str) -> Optional[dict]:
    """Fetch ``url`` and harvest structured metadata from its <head>.

    Returns ``{"title", "doi", "author", "year"}`` (any may be empty / None)
    or ``None`` if the URL is unfetchable, returns binary content, or yields
    no usable title.

    The verifier uses this as a *legitimacy check*: it confirms that the
    page DDG pointed at really does describe the cited reference (rather
    than just sharing a few keywords with it). When the page exposes a
    `citation_doi`, the verifier recurses into the academic-DB DOI lookup
    — turning a noisy web hit into a hard ID-based verification.
    """
    if not url or not isinstance(url, str):
        return None
    if not url.startswith(("http://", "https://")):
        return None
    base = url.lower().split("?", 1)[0].split("#", 1)[0]
    if any(base.endswith(ext) for ext in _BINARY_URL_EXTS):
        return None

    cache_query = {"endpoint": "url.metadata", "url": url}

    def _fetch():
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": _WEB_UA,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.5",
                    # Only the <head> matters; cap bandwidth at 256 KB.
                    "Range": "bytes=0-262143",
                },
                timeout=10,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException:
            return None
        if resp.status_code not in (200, 206):
            return None
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            return None
        body = resp.content[:524288]
        if not body:
            return None
        try:
            tree = _lxml_html.fromstring(body)
        except (ValueError, _lxml_etree.LxmlError):
            return None

        title = _meta_lookup(tree, _META_TITLE_NAMES)
        if not title:
            t_nodes = tree.xpath("//title//text()")
            if t_nodes:
                title = " ".join(s.strip() for s in t_nodes).strip()
        title = re.sub(r"\s+", " ", _html_lib.unescape(title)).strip()
        if not title:
            return None

        doi = ""
        raw_doi = _meta_lookup(tree, _META_DOI_NAMES)
        if raw_doi:
            m = re.search(r"(10\.\d{4,9}/[^\s\"'<>]+)", raw_doi)
            if m:
                doi = m.group(1).rstrip(".,;)\"'")

        author = _meta_lookup(tree, _META_AUTHOR_NAMES)

        year = None
        raw_year = _meta_lookup(tree, _META_DATE_NAMES)
        if raw_year:
            ym = re.search(r"\b(19|20)\d{2}\b", raw_year)
            if ym:
                try:
                    year = int(ym.group(0))
                except ValueError:
                    pass

        return {"title": title, "doi": doi, "author": author, "year": year}

    return get_cache().get_or_set("url_metadata", cache_query, _fetch)
