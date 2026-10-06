"""Flask application: JSON API + static frontend for the RAT dashboard.

Run with ``./run.sh`` or ``python3 -m backend.app``.
Configuration (environment variables):

    RAT_HOST              bind address            (default 127.0.0.1)
    PORT / RAT_PORT       port                    (default 8000)
    RAT_DATA_DIR          runtime data directory  (default ./data)
    RAT_MAX_UPLOAD_MB     zip upload limit        (default 1024)
    RAT_MAX_UNZIP_BYTES   uncompressed zip limit  (default 16 GiB)
"""

from __future__ import annotations

import logging
import os
import traceback

from flask import Flask, jsonify, request, send_from_directory

from . import db, ingest, metrics
from .gitparse import GitError

BASE_DIR = db.BASE_DIR
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

log = logging.getLogger("rat")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("RAT_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
app.json.sort_keys = False


# ---------------------------------------------------------------------------
# Error handling


@app.before_request
def _ensure_db():
    """Idempotent schema/data-dir init so the app works under any WSGI server."""
    db.init_db()


@app.errorhandler(metrics.MetricsError)
def _metrics_error(exc):
    return jsonify({"error": str(exc)}), exc.status_code


@app.errorhandler(ingest.IngestError)
@app.errorhandler(GitError)
def _ingest_error(exc):
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(413)
def _too_large(_exc):
    limit_mb = os.environ.get("RAT_MAX_UPLOAD_MB", "1024")
    return jsonify({"error": f"upload exceeds the {limit_mb} MB limit"}), 413


@app.errorhandler(Exception)
def _unexpected(exc):
    log.error("unhandled error: %s\n%s", exc, traceback.format_exc())
    return jsonify({"error": f"internal error: {exc}"}), 500


def _payload() -> dict:
    return request.get_json(silent=True) or {}


def _repo_or_404(conn, repo_id: int):
    row = ingest.get_repo(conn, repo_id)
    if row is None:
        raise metrics.NotFound("repository not found")
    return row


def _repo_json(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "source_type": row["source_type"],
        "source": row["source"],
        "status": row["status"],
        "status_detail": row["status_detail"],
        "error": row["error"],
        "head": row["head"],
        "commit_total": row["commit_total"],
        "commit_parsed": row["commit_parsed"],
        "file_count": row["file_count"],
        "author_count": row["author_count"],
        "parser_version": row["parser_version"],
        "parser_outdated": bool(row["parser_version"]) and row["parser_version"] != _parser_version(),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _parser_version() -> int:
    from .gitparse import PARSER_VERSION

    return PARSER_VERSION


# ---------------------------------------------------------------------------
# Frontend


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


# ---------------------------------------------------------------------------
# Repository management


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/repos")
def list_repos():
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM repos ORDER BY created_at DESC").fetchall()
        return jsonify({"repos": [_repo_json(r) for r in rows]})
    finally:
        conn.close()


@app.post("/api/repos/clone")
def clone_repo():
    payload = _payload()
    repo_id = ingest.create_url_repo(payload.get("url", ""), payload.get("name"))
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        return jsonify({"repo": _repo_json(row)}), 202
    finally:
        conn.close()


@app.post("/api/repos/upload")
def upload_repo():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise metrics.BadRequest("no file uploaded (expected multipart field 'file')")
    if not upload.filename.lower().endswith(".zip"):
        raise metrics.BadRequest("please upload a .zip archive of the repository")
    safe_name = f"upload-{db.now()}-{os.getpid()}.zip"
    zip_path = os.path.join(db.uploads_dir(), safe_name)
    upload.save(zip_path)
    repo_id = ingest.create_zip_repo(zip_path, request.form.get("name"))
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        return jsonify({"repo": _repo_json(row)}), 202
    finally:
        conn.close()


@app.delete("/api/repos/<int:repo_id>")
def delete_repo(repo_id: int):
    if ingest.JOBS.busy(repo_id):
        raise metrics.BadRequest("a job is currently running for this repository; try again shortly")
    ingest.delete_repo(repo_id)
    return jsonify({"ok": True})


@app.post("/api/repos/<int:repo_id>/refresh")
def refresh_repo(repo_id: int):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
    finally:
        conn.close()
    if not ingest.JOBS.submit(repo_id, ingest.refresh_repo, repo_id):
        raise metrics.BadRequest("a job is already running for this repository")
    return jsonify({"ok": True, "started": True}), 202


@app.post("/api/repos/<int:repo_id>/rebuild")
def rebuild_repo(repo_id: int):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
    finally:
        conn.close()
    if not ingest.JOBS.submit(repo_id, ingest.rebuild_repo, repo_id):
        raise metrics.BadRequest("a job is already running for this repository")
    return jsonify({"ok": True, "started": True}), 202


@app.get("/api/repos/<int:repo_id>/meta")
def repo_meta(repo_id: int):
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        refs = ingest.repo_refs(repo_id)
        last_ts = conn.execute(
            "SELECT MAX(ts) AS t FROM commits WHERE repo_id=?", (repo_id,)
        ).fetchone()["t"]
        return jsonify({
            "repo": _repo_json(row),
            "refs": refs,
            "last_commit_ts": last_ts,
            "default_ref": "HEAD",
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# References


@app.post("/api/repos/<int:repo_id>/refs/ensure")
def ensure_ref(repo_id: int):
    ref = str(_payload().get("ref") or "").strip() or "HEAD"
    conn = db.connect()
    try:
        row = _repo_or_404(conn, repo_id)
        if row["status"] in ("pending", "cloning", "loading", "parsing", "error"):
            raise metrics.BadRequest("repository is not ready yet")
        path = row["path"]
        # validate the ref synchronously so bad input fails fast
        from .gitparse import rev_parse

        rev_parse(path, ref)
        if metrics.ref_is_cached(conn, repo_id, ref):
            return jsonify({"ok": True, "cached": True})
    finally:
        conn.close()
    if not ingest.ensure_ref_async(repo_id, ref):
        raise metrics.BadRequest("a job is already running for this repository")
    return jsonify({"ok": True, "started": True}), 202


# ---------------------------------------------------------------------------
# Metrics


@app.post("/api/repos/<int:repo_id>/dashboard")
def dashboard(repo_id: int):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        return jsonify(metrics.dashboard(conn, repo_id, _payload()))
    finally:
        conn.close()


@app.post("/api/repos/<int:repo_id>/object")
def object_detail(repo_id: int):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        return jsonify(metrics.object_detail(conn, repo_id, _payload()))
    finally:
        conn.close()


@app.post("/api/repos/<int:repo_id>/tree")
def tree(repo_id: int):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        return jsonify(metrics.tree_children(conn, repo_id, _payload()))
    finally:
        conn.close()


@app.post("/api/repos/<int:repo_id>/commits")
def commits(repo_id: int):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        return jsonify(metrics.list_commits(conn, repo_id, _payload()))
    finally:
        conn.close()


@app.get("/api/repos/<int:repo_id>/commit/<commit_hash>")
def commit_detail(repo_id: int, commit_hash: str):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        return jsonify(metrics.commit_detail(conn, repo_id, commit_hash))
    finally:
        conn.close()


@app.get("/api/repos/<int:repo_id>/objects/search")
def search_objects(repo_id: int):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        return jsonify(metrics.search_objects(conn, repo_id, request.args.get("q", "")))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Authors


@app.get("/api/repos/<int:repo_id>/authors")
def authors(repo_id: int):
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        return jsonify(metrics.authors_overview(conn, repo_id))
    finally:
        conn.close()


@app.post("/api/repos/<int:repo_id>/authors/merge")
def merge_authors(repo_id: int):
    payload = _payload()
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        metrics.merge_authors(conn, repo_id, payload.get("canonical", ""), payload.get("keys") or [])
        return jsonify(metrics.authors_overview(conn, repo_id))
    finally:
        conn.close()


@app.post("/api/repos/<int:repo_id>/authors/unmerge")
def unmerge_authors(repo_id: int):
    payload = _payload()
    conn = db.connect()
    try:
        _repo_or_404(conn, repo_id)
        metrics.unmerge_authors(conn, repo_id, str(payload.get("canonical") or ""))
        return jsonify(metrics.authors_overview(conn, repo_id))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-repo


@app.get("/api/compare")
def compare():
    raw = request.args.get("ids", "")
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    conn = db.connect()
    try:
        if not ids:
            ids = [r["id"] for r in conn.execute("SELECT id FROM repos ORDER BY created_at").fetchall()]
        return jsonify({"repos": metrics.compare_repos(conn, ids)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    db.init_db()
    host = os.environ.get("RAT_HOST") or os.environ.get("HOST") or "127.0.0.1"
    port = int(os.environ.get("RAT_PORT") or os.environ.get("PORT") or "8000")
    log.info("RAT data directory: %s", db.data_dir())
    log.info("dashboard: http://%s:%d", "localhost" if host in ("0.0.0.0", "127.0.0.1") else host, port)
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
