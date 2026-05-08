"""
Centralised configuration. Everything is read from environment variables so
the same code runs locally, in Docker, and in CI without edits.

A user-friendly start.sh / start.bat creates a .env file on first launch and
docker compose loads it automatically. For development you can also `export`
the variables manually.
"""
import os


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


# --- Required for polite-pool access to OpenAlex / Crossref ---
OPENALEX_EMAIL = os.environ.get("OPENALEX_EMAIL", "").strip()

# --- Optional, but boosts S2 rate limits if present ---
S2_API_KEY = os.environ.get("S2_API_KEY", "").strip()

# --- Grobid host (defaults to localhost for non-Docker dev) ---
GROBID_HOST = os.environ.get("GROBID_HOST", "http://localhost:8070").rstrip("/")
GROBID_FULLTEXT_URL = f"{GROBID_HOST}/api/processFulltextDocument"
GROBID_CITATION_URL = f"{GROBID_HOST}/api/processCitation"

# --- Optional Ollama fallback (off by default; editors don't need it) ---
USE_OLLAMA_FALLBACK = _bool("USE_OLLAMA_FALLBACK", default=False)
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")

# --- Concurrency ---
MAX_REF_WORKERS = int(os.environ.get("MAX_REF_WORKERS", "6"))
BATCH_PDF_WORKERS = int(os.environ.get("BATCH_PDF_WORKERS", "2"))

# --- Storage ---
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
CACHE_DB = os.environ.get("CACHE_DB", "cache/api_cache.db")
CACHE_TTL_DAYS = int(os.environ.get("CACHE_TTL_DAYS", "30"))
RESULTS_DB = os.environ.get("RESULTS_DB", "results/results.db")

# --- Matching thresholds (tuneable; see matching.py for definitions) ---
VERIFY_SCORE = int(os.environ.get("VERIFY_SCORE", "90"))
FLAWED_SCORE = int(os.environ.get("FLAWED_SCORE", "78"))
MIN_CONSIDER_SCORE = int(os.environ.get("MIN_CONSIDER_SCORE", "60"))


def warn_if_misconfigured():
    """Print a friendly warning at startup so editors know what to fix."""
    msgs = []
    if not OPENALEX_EMAIL:
        msgs.append(
            "  ⚠  OPENALEX_EMAIL is not set. OpenAlex will work but at lower rate "
            "limits (slower batches). Set it in your .env file."
        )
    if msgs:
        print("\n".join(msgs))
