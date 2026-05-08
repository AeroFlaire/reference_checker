"""
Thin Flask wrapper around the pipeline. Routes preserve the contract that
index.html already expects, so the existing UI works unchanged.
"""
import os
import concurrent.futures
from flask import Flask, jsonify, request, send_file
from werkzeug.utils import secure_filename

import config
from cache import get_cache
from grobid_client import grobid_alive, wait_for_grobid
from pipeline import process_references_list, process_pdf_file, merge_result_sets

ALLOWED_EXTENSIONS = {"pdf"}

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = config.UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return send_file("index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "grobid_reachable": grobid_alive(),
        "cache_stats": get_cache().stats(),
        "ollama_fallback_enabled": config.USE_OLLAMA_FALLBACK,
    })


@app.route("/api/check-references", methods=["POST"])
def api_check_references():
    """Legacy: raw JSON input with a 'references' list."""
    try:
        data = request.get_json() or {}
        results = process_references_list(data.get("references", []))
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload-pdf", methods=["POST"])
def upload_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if not file or file.filename == "":
        return jsonify({"error": "No selected file"}), 400
    if not _allowed_file(file.filename):
        return jsonify({"error": "Invalid file type"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(config.UPLOAD_FOLDER, filename)
    file.save(filepath)
    try:
        result = process_pdf_file(filepath, filename)
        return jsonify(result["results"])
    except Exception as e:
        print(f"Error processing PDF: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass


@app.route("/api/upload-pdfs", methods=["POST"])
def upload_pdfs_batch():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    saved = []
    for idx, f in enumerate(files):
        if not f or not f.filename:
            continue
        if not _allowed_file(f.filename):
            return jsonify({"error": f"Invalid file type: {f.filename}"}), 400
        name = secure_filename(f.filename)
        path = os.path.join(config.UPLOAD_FOLDER, f"{idx}_{name}")
        f.save(path)
        saved.append((path, name))
    if not saved:
        return jsonify({"error": "No valid files"}), 400

    per_file = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.BATCH_PDF_WORKERS) as pool:
        futs = {pool.submit(process_pdf_file, p, n): (p, n) for p, n in saved}
        for fut in concurrent.futures.as_completed(futs):
            path, name = futs[fut]
            try:
                per_file.append(fut.result())
            except Exception as e:
                per_file.append({
                    "filename": name, "status": "error", "error": str(e),
                    "results": {k: [] for k in
                                ["verified", "edition_mismatch", "flawed_reference",
                                 "not_found", "not_reference"]},
                })
            finally:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    aggregate = merge_result_sets([e.get("results", {}) for e in per_file if e.get("status") == "done"])
    summary = {
        "total_files": len(per_file),
        "completed": sum(1 for x in per_file if x.get("status") == "done"),
        "failed":    sum(1 for x in per_file if x.get("status") == "error"),
    }
    return jsonify({
        "mode": "batch",
        "summary": summary,
        "files": per_file,
        "aggregate": aggregate,
        # back-compat keys for the existing frontend
        **aggregate,
    })


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config.warn_if_misconfigured()
    print(f"Waiting for GROBID at {config.GROBID_HOST} ...")
    if wait_for_grobid(120):
        print("✓ GROBID is up")
    else:
        print("⚠  GROBID did not respond in 120s — endpoints will return errors.")
    print(f"\nReference checker is ready at http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
