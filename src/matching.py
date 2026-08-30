"""
Title-similarity matching for reference verification.

Replaces the previous hand-rolled Levenshtein with a blend of RapidFuzz metrics,
plus length-aware guards that protect against the common false-positive mode of
"short query matches a longer title that just happens to contain those words"
(e.g. "Algorithms" vs "Introduction to Algorithms").

The function returns a single 0–100 score that the verifier compares against
config.VERIFY_SCORE / config.FLAWED_SCORE thresholds.
"""
import re
import unicodedata
import urllib.parse
from rapidfuzz import fuzz


# PDF extractors love to mangle separators into accented Latin glyphs:
#   em-dash → "ś" (U+015B), hyphen → "š" (U+0161), bullet → "·" (already ok)
# These show up between years ("2015ś2020"), in compound words, and as
# bibliographic separators. Map them to a real space/dash before the
# unicode-fold step destroys their structure.
_GLYPH_FIXES = str.maketrans({
    "ś": "-",  # ś
    "Ś": "-",  # Ś
    "š": "-",  # š
    "Š": "-",  # Š
    "ź": "-",  # ź
    "Ź": "-",  # Ź
    "ż": "-",  # ż
    "Ż": "-",  # Ż
    "˚": "",   # ˚ (degree-like marks PDFs leave behind)
    "­": "",   # soft hyphen
})


def repair_pdf_glyphs(text: str) -> str:
    """Reverse common PDF mis-encodings of separators into accented Latin
    glyphs, plus URL-decode any %xx sequences left in embedded filenames."""
    if not text:
        return ""
    text = text.translate(_GLYPH_FIXES)
    if "%" in text:
        # Only touch obvious URL-encoded substrings — `%20`, `%2F`, etc.
        # urllib.unquote is forgiving about non-encoded `%`s, so this is safe
        # to apply to mixed text.
        text = urllib.parse.unquote(text)
    return text


# Cheap stopwords list — only the ones that distort matching
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "to", "for",
    "with", "by", "is", "are", "at", "from", "as", "that",
}


def normalize_text(text: str) -> str:
    """ASCII-fold, lowercase, collapse non-alphanumerics to single space."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Ceiling applied when one title is fully contained in a much longer one.
# Must sit in [FLAWED_SCORE, VERIFY_SCORE) so such candidates are routed to
# FLAWED_REFERENCE for human review instead of being auto-verified.
_CONTAINED_TITLE_CAP = 85


def _strip_subtitle(text: str) -> str:
    """Cut at first colon / semicolon / paren — common subtitle separators.

    Matches "Type Theory and Formal Proof: An Introduction" → "Type Theory and Formal Proof"
    Also catches trailing metadata like "Title: Author, Venue, Year".
    """
    if not text:
        return ""
    return re.split(r"[:;(\[]", text, maxsplit=1)[0].strip()


def title_similarity(parsed: str, candidate: str, contained_guard: bool = True) -> int:
    """Return 0–100 similarity between the parsed reference title and a
    candidate retrieved from a database.

    Designed to:
      - tolerate hyphenation, case, punctuation, ASCII vs unicode (✓)
      - tolerate subtitles, edition tags, trailing author/venue metadata (✓)
      - reject prefix-only collisions (e.g. "Algorithms" → "Intro to Algorithms")
      - reject titles with only 1-2 content tokens unless covered exactly

    The verifier should use:
      score >= config.VERIFY_SCORE   (default 90)  → VERIFIED
      score >= config.FLAWED_SCORE   (default 78)  → FLAWED_REFERENCE
      score >= config.MIN_CONSIDER   (default 60)  → keep iterating
      score <  config.MIN_CONSIDER                 → reject
    """
    if not parsed or not candidate:
        return 0

    p_full = normalize_text(parsed)
    c_full = normalize_text(candidate)
    if not p_full or not c_full:
        return 0

    # Stripped variants — handle subtitles on either side.
    p_short = normalize_text(_strip_subtitle(parsed))
    c_short = normalize_text(_strip_subtitle(candidate))

    pairs = [(p_full, c_full), (p_short, c_short), (p_short, c_full), (p_full, c_short)]
    sort_scores = [fuzz.token_sort_ratio(a, b) for a, b in pairs if a and b]
    partial_scores = [fuzz.partial_ratio(a, b) for a, b in pairs if a and b]
    base = max(sort_scores) if sort_scores else 0
    partial_max = max(partial_scores) if partial_scores else 0

    # Content-token coverage check: how many content tokens of the smaller title
    # are present in the larger? This is what stops "Algorithms" from matching
    # "Introduction to Algorithms".
    p_content = {w for w in p_full.split() if w not in _STOPWORDS}
    c_content = {w for w in c_full.split() if w not in _STOPWORDS}
    if not p_content or not c_content:
        return int(base)

    smaller = p_content if len(p_content) <= len(c_content) else c_content
    larger = c_content if smaller is p_content else p_content
    coverage = len(smaller & larger) / len(smaller)
    n_small = len(smaller)

    # A candidate that fully covers the shorter title but carries a lot of
    # extra content tokens is usually a DIFFERENT work that contains or
    # discusses it — and partial_ratio scores those 100 regardless. Measured
    # on run6: "CSTA Standards for CS Teachers" matched "Using CSTA Standards
    # for CS Teachers to Design CS Teacher Pathways", and "Denoising
    # diffusion probabilistic models" matched "...for Probabilistic Energy
    # Forecasting". Use a proportional test, not an absolute token count:
    # set semantics dedupe repeated words, so a genuinely different title can
    # add as few as two new tokens.
    #
    # Genuine subtitles stay safe because _strip_subtitle() already feeds
    # token_sort_ratio a subtitle-free variant, which sets `base` high on its
    # own before we get here.
    # CRITICAL: only meaningful when BOTH sides are titles. Several callers
    # score a DB title against the RAW REFERENCE STRING, which legitimately
    # carries far more tokens (authors, venue, year, pages) — there the size
    # gap is normal, not evidence of a different work. Those callers pass
    # contained_guard=False. Getting this wrong cost run7: "Attention Is All
    # You Need" scored against its own reference dropped 100 -> 85, and
    # verified fell 3690 -> 3484 with not_found up 442 -> 595.
    much_longer = contained_guard and len(larger) > len(smaller) * 1.4

    # Tiered logic by size of the smaller content-token set:
    if coverage >= 0.95 and n_small >= 4:
        # Substantial title fully present → trust partial_ratio
        # (handles "Attention Is All You Need" → "Attention Is All You Need: Vaswani...")
        # unless the candidate is much longer, in which case cap below
        # VERIFY_SCORE so it lands in FLAWED_REFERENCE for an editor to judge.
        base = max(base, min(partial_max, _CONTAINED_TITLE_CAP) if much_longer else partial_max)
    elif coverage >= 0.95 and n_small == 3:
        # Borderline: trust partial up to 92, no further — and only when the
        # candidate is not much longer, since 92 is still above VERIFY_SCORE.
        base = max(base, min(partial_max, _CONTAINED_TITLE_CAP if much_longer else 92))
    elif n_small <= 2:
        # Too short to disambiguate → cap conservatively
        base = min(base, 78 if coverage >= 1.0 else 70)
    elif coverage < 0.75:
        # Many content tokens missing → likely a different paper
        base = min(base, 70)

    return int(base)


def title_matches(parsed: str, candidate: str, threshold: int) -> bool:
    """Convenience wrapper for boolean checks."""
    return title_similarity(parsed, candidate) >= threshold


def normalize_doi(doi: str) -> str:
    """Strip URL prefixes, trailing punctuation, lowercase the registrant prefix."""
    if not doi:
        return ""
    doi = doi.strip()
    # Pull out just the 10.xxxx/yyyy part if a full URL was passed
    m = re.search(r"(10\.\d{4,9}/[^\s]+)", doi)
    if m:
        doi = m.group(1)
    # Strip trailing punctuation that PDFs love to glue on
    doi = doi.rstrip(".,;)\"'")
    return doi


def repair_doi_with_linebreaks(text: str) -> list:
    """Find DOIs even when the PDF inserted line breaks or soft-hyphens.

    Returns a list of cleaned DOIs found in `text`. The default regex used by
    the verifier still runs first; this is a fallback for the failure case.
    """
    if not text:
        return []
    # Remove soft hyphens and join obvious mid-DOI line breaks
    cleaned = text.replace("\u00ad", "").replace("\xad", "")
    cleaned = re.sub(r"(10\.\d{4,9}/[^\s]*)\s*\n\s*([^\s]*)", r"\1\2", cleaned)
    matches = re.findall(r"\b(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)", cleaned)
    return [normalize_doi(m) for m in matches]
