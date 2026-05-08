"""
Pipeline: orchestrate parallel verification of a list of references and the
aggregation of multi-PDF batch results. The Flask app and the CLI both call
into here so they share identical behaviour.
"""
import time
import concurrent.futures
from typing import Iterable

import config
import grobid_client
from verifier import check_single_reference


_RESULT_BUCKETS = ["verified", "edition_mismatch", "flawed_reference", "not_found", "not_reference"]
_STATUS_TO_BUCKET = {
    "VERIFIED": "verified",
    "YEAR_MISMATCH": "edition_mismatch",
    "FLAWED_REFERENCE": "flawed_reference",
    "NOT_FOUND": "not_found",
    "NOT_REFERENCE": "not_reference",
}


def _empty_buckets() -> dict:
    return {k: [] for k in _RESULT_BUCKETS}


def process_references_list(reference_strings: Iterable, on_progress=None) -> dict:
    """Run check_single_reference in parallel over an iterable of references."""
    refs = list(reference_strings)
    out = _empty_buckets()
    if not refs:
        return out

    start = time.time()
    print(f"  -> Verifying {len(refs)} references ({config.MAX_REF_WORKERS} workers)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_REF_WORKERS) as pool:
        futures = {pool.submit(check_single_reference, r): r for r in refs}
        completed = 0
        for fut in concurrent.futures.as_completed(futures):
            completed += 1
            try:
                result = fut.result()
            except Exception as e:
                print(f"  -> worker exception: {e}")
                continue
            if result is None:
                continue
            bucket = _STATUS_TO_BUCKET.get(result["status"], "not_found")
            out[bucket].append(result["payload"])

            if on_progress:
                on_progress(completed, len(refs))
            elif completed % 10 == 0:
                print(f"     [{completed}/{len(refs)}] checked")

    duration = time.time() - start
    print(
        f"  -> Done. {len(refs)} refs in {duration:.1f}s "
        f"({duration/max(len(refs),1):.2f}s/ref avg)"
    )
    return out


def process_pdf_file(filepath: str, filename: str = None) -> dict:
    """Extract refs from a PDF and run them through the pipeline."""
    filename = filename or filepath.rsplit("/", 1)[-1]
    print(f"==> {filename}")

    references = grobid_client.extract_references(filepath)
    print(f"  -> Found {len(references)} references")

    results = process_references_list(references)
    counts = {k: len(v) for k, v in results.items()}
    return {
        "filename": filename,
        "status": "done",
        "reference_count": len(references),
        "counts": counts,
        "results": results,
    }


def merge_result_sets(result_sets: list) -> dict:
    merged = _empty_buckets()
    for rs in result_sets:
        if not rs:
            continue
        for k in merged:
            merged[k].extend(rs.get(k, []))
    return merged
