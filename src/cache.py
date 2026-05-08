"""
Persistent SQLite-backed cache for reference-lookup API responses.

Why a cache?
  With 600 PDFs × ~30 references each ≈ 18 000 lookups, and most academic
  references are cited by many papers, a substantial fraction of those lookups
  hit the same DOIs / titles. A persistent cache makes re-runs ~3-5× faster
  and lets you tune thresholds without re-paying API costs.

Why SQLite?
  - Zero install, ships with Python.
  - Survives crashes (WAL mode), so a 600-PDF batch can be killed and resumed.
  - Works fine across threads when you open one short-lived connection per call.

Keys are SHA-256(source || canonical_json(query)) — stable across runs.
"""
import sqlite3
import json
import hashlib
import time
import threading
from pathlib import Path
from typing import Any, Optional

import config


class APICache:
    def __init__(self, db_path: Optional[str] = None, ttl_days: Optional[int] = None):
        self.db_path = db_path or config.CACHE_DB
        self.ttl_seconds = (ttl_days if ttl_days is not None else config.CACHE_TTL_DAYS) * 86400
        self._lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False is fine because we hold self._lock for writes
        # and reads through SQLite are safe across threads.
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key  TEXT PRIMARY KEY,
                    source     TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_cache_source ON api_cache(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_cache_created ON api_cache(created_at)")
            conn.commit()

    @staticmethod
    def _key(source: str, query: Any) -> str:
        canonical = json.dumps(query, sort_keys=True, default=str)
        return hashlib.sha256(f"{source}::{canonical}".encode("utf-8")).hexdigest()

    def get(self, source: str, query: Any) -> Optional[Any]:
        """Return cached response or None if missing/expired."""
        key = self._key(source, query)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload, created_at FROM api_cache WHERE cache_key = ?",
                    (key,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        payload, created_at = row
        if time.time() - created_at > self.ttl_seconds:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    def set(self, source: str, query: Any, response: Any) -> None:
        """Store response. Silent no-op on serialization or DB errors."""
        key = self._key(source, query)
        try:
            payload = json.dumps(response, default=str)
        except (TypeError, ValueError):
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO api_cache (cache_key, source, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (key, source, payload, int(time.time())),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def get_or_set(self, source: str, query: Any, fetch_fn) -> Any:
        """Return cached value, otherwise call fetch_fn() and cache its result."""
        cached = self.get(source, query)
        if cached is not None:
            return cached
        result = fetch_fn()
        # Only cache successful results; None and {"error": ...} aren't useful.
        if result is not None and not (isinstance(result, dict) and "error" in result):
            self.set(source, query, result)
        return result

    def stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) FROM api_cache GROUP BY source ORDER BY COUNT(*) DESC"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]
        return {"total": total, "by_source": dict(rows)}

    def purge_expired(self) -> int:
        cutoff = int(time.time()) - self.ttl_seconds
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM api_cache WHERE created_at < ?", (cutoff,))
            conn.commit()
            return cur.rowcount

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM api_cache")
            conn.commit()


# Process-wide default
_default_cache: Optional[APICache] = None


def get_cache() -> APICache:
    global _default_cache
    if _default_cache is None:
        _default_cache = APICache()
    return _default_cache
