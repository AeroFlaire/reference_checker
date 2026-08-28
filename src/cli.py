#!/usr/bin/env python3
"""
Command-line interface for batch reference checking.

Why a CLI?
  The Flask /api/upload-pdfs route holds 600 PDFs in memory and returns one
  giant JSON when (if) it finishes. That's fragile: one timeout and you start
  over. The CLI processes PDFs one at a time, writes per-file results to
  SQLite immediately, and is fully resumable — kill it any time and re-run
  with --resume to skip what's already done.

Subcommands:

  check  --pdf  <file>                Single PDF, print summary
  batch  --input <dir> --output <db>  Process all PDFs in a directory
         [--resume]                   Skip files already in the results DB
         [--workers N]                Override BATCH_PDF_WORKERS
  stats  --output <db>                Print summary across all processed files
  export --output <db> --csv <file>   Dump all reference results to CSV
         --jsonl <file>               Dump as one-JSON-per-line
         --not-found-only             Only export the failures editors care about
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import config
from cache import get_cache
from grobid_client import grobid_alive
from pipeline import process_pdf_file


# ---------------------------------------------------------------------------
# Results DB — separate from the API cache; one row per (file, reference).
# ---------------------------------------------------------------------------

_RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_status (
    filename         TEXT PRIMARY KEY,
    filepath         TEXT,
    status           TEXT NOT NULL,        -- 'done' | 'error'
    reference_count  INTEGER,
    verified         INTEGER DEFAULT 0,
    edition_mismatch INTEGER DEFAULT 0,
    flawed           INTEGER DEFAULT 0,
    not_found        INTEGER DEFAULT 0,
    not_reference    INTEGER DEFAULT 0,
    error_message    TEXT,
    processed_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reference_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    filename         TEXT NOT NULL,
    bucket           TEXT NOT NULL,        -- verified | edition_mismatch | flawed_reference | not_found | not_reference
    original_text    TEXT,
    matched_title    TEXT,
    matched_year     INTEGER,
    matched_url      TEXT,
    matched_source   TEXT,
    note             TEXT,
    FOREIGN KEY (filename) REFERENCES file_status(filename) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_results_filename ON reference_results(filename);
CREATE INDEX IF NOT EXISTS idx_results_bucket   ON reference_results(bucket);
"""


def init_results_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_RESULTS_SCHEMA)
        conn.commit()


def already_processed(db_path: str, filename: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM file_status WHERE filename = ?", (filename,)
        ).fetchone()
    return bool(row) and row[0] == "done"


def _pick_match(payload: dict) -> dict:
    """Pull whichever match field the verifier put in this payload."""
    for k in ("openalex_match", "openalex_match (edition mismatch)", "openalex_match (mismatched)"):
        if k in payload and payload[k]:
            return payload[k]
    return {}


def store_pdf_result(db_path: str, result: dict) -> None:
    fn = result["filename"]
    counts = result.get("counts", {})
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM file_status WHERE filename = ?", (fn,))
        conn.execute("DELETE FROM reference_results WHERE filename = ?", (fn,))
        conn.execute(
            """INSERT INTO file_status (filename, filepath, status, reference_count,
                 verified, edition_mismatch, flawed, not_found, not_reference,
                 error_message, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fn,
                result.get("filepath"),
                result.get("status", "done"),
                result.get("reference_count", 0),
                counts.get("verified", 0),
                counts.get("edition_mismatch", 0),
                counts.get("flawed_reference", 0),
                counts.get("not_found", 0),
                counts.get("not_reference", 0),
                result.get("error"),
                int(time.time()),
            ),
        )
        for bucket, items in (result.get("results") or {}).items():
            for payload in items:
                m = _pick_match(payload)
                year = m.get("publication_year")
                try:
                    year = int(str(year)[:4]) if year else None
                except (TypeError, ValueError):
                    year = None
                conn.execute(
                    """INSERT INTO reference_results
                       (filename, bucket, original_text, matched_title, matched_year,
                        matched_url, matched_source, note)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        fn,
                        bucket,
                        payload.get("original_reference", ""),
                        m.get("display_name"),
                        year,
                        m.get("id"),
                        (payload.get("parsed_query") or {}).get("source"),
                        m.get("note"),
                    ),
                )
        conn.commit()


def store_error(db_path: str, filename: str, filepath: str, msg: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM file_status WHERE filename = ?", (filename,))
        conn.execute(
            """INSERT INTO file_status (filename, filepath, status, error_message, processed_at)
               VALUES (?, ?, 'error', ?, ?)""",
            (filename, filepath, msg, int(time.time())),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_check(args) -> int:
    if not grobid_alive():
        print(f"ERROR: GROBID is not reachable at {config.GROBID_HOST}", file=sys.stderr)
        print("  Start it with: docker compose up -d   (or run start.sh / start.bat)", file=sys.stderr)
        return 2
    if not os.path.exists(args.pdf):
        print(f"ERROR: file not found: {args.pdf}", file=sys.stderr)
        return 2
    result = process_pdf_file(args.pdf, os.path.basename(args.pdf))
    print(json.dumps(result["counts"], indent=2))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.json, "w") as fh:
            json.dump(result, fh, indent=2)
        print(f"Wrote {args.json}")
    return 0


def cmd_batch(args) -> int:
    if not grobid_alive():
        print(f"ERROR: GROBID is not reachable at {config.GROBID_HOST}", file=sys.stderr)
        print("  Start it first: docker compose up -d   (or run start.sh / start.bat)", file=sys.stderr)
        return 2

    input_dir = Path(args.input)
    if not input_dir.is_dir():
        print(f"ERROR: not a directory: {input_dir}", file=sys.stderr)
        return 2

    db_path = args.output
    init_results_db(db_path)

    if args.workers:
        config.BATCH_PDF_WORKERS = args.workers

    pdfs = sorted(p for p in input_dir.rglob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"No PDFs found in {input_dir}", file=sys.stderr)
        return 1

    todo = []
    skipped = 0
    for p in pdfs:
        if args.resume and already_processed(db_path, p.name):
            skipped += 1
            continue
        todo.append(p)

    print(f"PDFs in input dir: {len(pdfs)}")
    print(f"  already processed (skipped via --resume): {skipped}")
    print(f"  to process this run:                      {len(todo)}")
    print(f"  results DB:                               {db_path}")
    print(f"  cache DB:                                 {config.CACHE_DB}")
    print()

    started = time.time()
    for i, pdf_path in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {pdf_path.name}")
        try:
            result = process_pdf_file(str(pdf_path), pdf_path.name)
            result["filepath"] = str(pdf_path)
            store_pdf_result(db_path, result)
            c = result["counts"]
            print(f"   ✓ verified={c['verified']}  edition={c['edition_mismatch']}  "
                  f"flawed={c['flawed_reference']}  not_found={c['not_found']}  "
                  f"not_ref={c['not_reference']}")
        except KeyboardInterrupt:
            print("\nInterrupted. Re-run with --resume to continue.")
            return 130
        except Exception as e:
            print(f"   ✗ ERROR: {e}")
            store_error(db_path, pdf_path.name, str(pdf_path), str(e))
            if not args.continue_on_error:
                print("  use --continue-on-error to skip failed PDFs", file=sys.stderr)
                return 1

    elapsed = time.time() - started
    print(f"\nFinished {len(todo)} PDFs in {elapsed/60:.1f} min "
          f"({elapsed/max(len(todo),1):.1f} s/PDF avg)")
    return 0


def cmd_stats(args) -> int:
    if not os.path.exists(args.output):
        print(f"No results DB at {args.output}", file=sys.stderr)
        return 1
    with sqlite3.connect(args.output) as conn:
        files = conn.execute("SELECT COUNT(*) FROM file_status WHERE status='done'").fetchone()[0]
        errors = conn.execute("SELECT COUNT(*) FROM file_status WHERE status='error'").fetchone()[0]
        totals = dict(conn.execute(
            "SELECT bucket, COUNT(*) FROM reference_results GROUP BY bucket"
        ).fetchall())
    print(f"PDFs processed:   {files}")
    print(f"PDFs failed:      {errors}")
    print(f"References:")
    for bucket in ("verified", "edition_mismatch", "flawed_reference", "not_found", "not_reference"):
        print(f"  {bucket:18s} {totals.get(bucket, 0)}")
    cache_stats = get_cache().stats()
    print(f"\nAPI cache: {cache_stats['total']} entries")
    for src, n in cache_stats["by_source"].items():
        print(f"  {src:20s} {n}")
    return 0


# Sources whose entries are "search results" — non-deterministic, can be
# poisoned by a transient API outage that returns an empty list. Safe to
# wipe; expensive ID lookups (DOI, arXiv, ISBN) are kept since their hits
# don't go stale.
_SEARCH_CACHE_SOURCES = (
    "openalex_search", "crossref_search", "s2_search",
    "ddg_html", "url_metadata",
)


def cmd_cache(args) -> int:
    """Inspect or clear the API cache.

    With no flags: print a per-source breakdown and exit.
    --clear-search:  drop only the search-style caches (openalex_search,
                     crossref_search, s2_search, ddg_html, url_metadata).
                     ID lookups (DOIs, arXiv, ISBN, RFC) are preserved
                     because their answers don't go stale.
    --clear-all:     wipe the entire API cache.
    """
    cache = get_cache()

    if args.clear_all:
        cache.clear()
        print(f"Cleared entire API cache ({cache.db_path}).")
        return 0

    if args.clear_search:
        with sqlite3.connect(cache.db_path) as conn:
            placeholders = ",".join("?" * len(_SEARCH_CACHE_SOURCES))
            cur = conn.execute(
                f"DELETE FROM api_cache WHERE source IN ({placeholders})",
                _SEARCH_CACHE_SOURCES,
            )
            conn.commit()
            removed = cur.rowcount
        print(f"Cleared {removed} cached search entries from "
              f"{cache.db_path}\n  (sources: {', '.join(_SEARCH_CACHE_SOURCES)})")
        return 0

    # Default — stats.
    stats = cache.stats()
    print(f"API cache: {cache.db_path}")
    print(f"  total entries: {stats['total']}")
    if stats["by_source"]:
        print()
        for src, n in stats["by_source"].items():
            print(f"  {src:24s} {n}")
    print(
        "\nFlags:\n"
        "  --clear-search   wipe poisoned search caches "
        "(keeps reliable DOI/ID lookups)\n"
        "  --clear-all      wipe everything"
    )
    return 0


def cmd_export(args) -> int:
    if not os.path.exists(args.output):
        print(f"No results DB at {args.output}", file=sys.stderr)
        return 1

    where = ""
    params: list = []
    if args.not_found_only:
        where = "WHERE bucket IN ('not_found', 'not_reference', 'flawed_reference')"

    with sqlite3.connect(args.output) as conn:
        rows = conn.execute(
            f"""SELECT filename, bucket, original_text, matched_title, matched_year,
                       matched_url, matched_source, note
                FROM reference_results {where}
                ORDER BY filename, bucket""",
            params,
        ).fetchall()
    cols = ["filename", "bucket", "original_text", "matched_title",
            "matched_year", "matched_url", "matched_source", "note"]

    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}")
    if args.jsonl:
        Path(args.jsonl).parent.mkdir(parents=True, exist_ok=True)
        with open(args.jsonl, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(dict(zip(cols, r))) + "\n")
        print(f"Wrote {len(rows)} rows to {args.jsonl}")
    if not args.csv and not args.jsonl:
        print("Pass --csv FILE and/or --jsonl FILE to write output.", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="refchecker",
                                description="Batch reference checker (CLI mode)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("check", help="Check a single PDF")
    pc.add_argument("--pdf", required=True)
    pc.add_argument("--json", help="Optional path to write full JSON result")

    pb = sub.add_parser("batch", help="Process all PDFs in a directory")
    pb.add_argument("--input", required=True, help="Directory of PDFs (recursed)")
    pb.add_argument("--output", default=config.RESULTS_DB,
                    help=f"Results DB path (default: {config.RESULTS_DB})")
    pb.add_argument("--resume", action="store_true",
                    help="Skip PDFs already in the results DB")
    pb.add_argument("--workers", type=int, default=None,
                    help="Override BATCH_PDF_WORKERS")
    pb.add_argument("--continue-on-error", action="store_true",
                    help="Don't stop the run if a single PDF fails")

    ps = sub.add_parser("stats", help="Show results-DB summary")
    ps.add_argument("--output", default=config.RESULTS_DB)

    pcache = sub.add_parser(
        "cache",
        help="Inspect or clear the API cache",
    )
    pcache.add_argument(
        "--clear-search", action="store_true",
        help="Drop search-style caches (openalex_search, crossref_search, "
             "s2_search, ddg_html, url_metadata). Keeps reliable ID lookups.",
    )
    pcache.add_argument(
        "--clear-all", action="store_true",
        help="Wipe the entire API cache.",
    )

    pe = sub.add_parser("export", help="Export references from the results DB")
    pe.add_argument("--output", default=config.RESULTS_DB,
                    help="Results DB path (input)")
    pe.add_argument("--csv", help="Write CSV to this path")
    pe.add_argument("--jsonl", help="Write JSONL to this path")
    pe.add_argument("--not-found-only", action="store_true",
                    help="Only the references editors need to review")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config.warn_if_misconfigured()
    return {
        "check":  cmd_check,
        "batch":  cmd_batch,
        "stats":  cmd_stats,
        "cache":  cmd_cache,
        "export": cmd_export,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
