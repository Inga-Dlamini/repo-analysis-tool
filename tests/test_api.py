"""End-to-end API tests through the Flask test client.

These exercise the real threaded ingestion path (zip upload -> background
job -> ready), the 409 "ref not prepared" flow with async ensure, author
merging endpoints and error handling.
"""

from __future__ import annotations

import io
import time
import zipfile

import pytest

from backend import db, metrics
from conftest import BOB, ROBERT, make_repo_zip, ts


def wait_ready(client, repo_id, timeout=90):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        repos = client.get("/api/repos").get_json()["repos"]
        row = next((r for r in repos if r["id"] == repo_id), None)
        if row is None:
            raise AssertionError("repository disappeared while polling")
        last = row
        if row["status"] in ("ready", "error"):
            return row
        time.sleep(0.15)
    raise AssertionError(f"repo {repo_id} not ready within {timeout}s: {last}")


def wait_ref_cached(repo_id, ref, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = db.connect()
        try:
            if metrics.ref_is_cached(conn, repo_id, ref):
                return
        finally:
            conn.close()
        time.sleep(0.1)
    raise AssertionError(f"ref '{ref}' was not prepared within {timeout}s")


@pytest.fixture()
def uploaded_repo(client, synth_repo):
    zip_path = make_repo_zip(synth_repo["path"], synth_repo["path"].parent / "repo.zip")
    res = client.post(
        "/api/repos/upload",
        data={"file": (io.BytesIO(zip_path.read_bytes()), "repo.zip"), "name": "uploaded"},
        content_type="multipart/form-data",
    )
    assert res.status_code == 202, res.get_json()
    repo_id = res.get_json()["repo"]["id"]
    row = wait_ready(client, repo_id)
    assert row["status"] == "ready", row
    yield repo_id
    client.delete(f"/api/repos/{repo_id}")


def post(client, path, payload):
    return client.post(path, json=payload)


# ---------------------------------------------------------------------------


def test_health_index_and_repo_list(client):
    assert client.get("/api/health").get_json() == {"ok": True}

    index = client.get("/")
    assert index.status_code == 200
    assert b"RAT" in index.data

    body = client.get("/api/repos").get_json()
    assert "repos" in body


def test_upload_ingest_and_metrics_flow(client, uploaded_repo, synth_repo):
    h = synth_repo["hashes"]

    meta = client.get(f"/api/repos/{uploaded_repo}/meta").get_json()
    assert meta["repo"]["status"] == "ready"
    assert meta["repo"]["commit_parsed"] == 9
    assert meta["last_commit_ts"] == ts(10)
    ref_names = {r["name"] for r in meta["refs"]}
    assert {"HEAD", "main", "feature", "v1"} <= ref_names
    head_ref = next(r for r in meta["refs"] if r["name"] == "HEAD")
    assert head_ref["ready"] is True

    d = post(client, f"/api/repos/{uploaded_repo}/dashboard", {}).get_json()
    assert d["summary"]["commit_count"] == 9
    assert d["summary"]["added"] == 26
    assert d["repo_metrics"]["churn"] == 32
    assert d["tree"]["name"] == "repo"
    assert {c["name"] for c in d["tree"]["children"]} == {"src", "docs", "feature", "src2", "README.md"}

    # commit picker: newest first, paginated, ignores manual selections
    page = post(client, f"/api/repos/{uploaded_repo}/commits", {"per_page": 3, "page": 1}).get_json()
    assert page["total"] == 9
    assert [c["hash"] for c in page["commits"]] == [h["c9"], h["c8"], h["b7"]]
    assert page["commits"][1]["churn"] == 0        # the empty commit

    search = post(client, f"/api/repos/{uploaded_repo}/commits", {"q": "feature"}).get_json()
    assert search["total"] == 1                    # subject "b7: feature notes"

    detail = client.get(f"/api/repos/{uploaded_repo}/commit/{h['c2']}").get_json()
    assert (detail["added"], detail["removed"]) == (6, 1)
    assert len(detail["files"]) == 2

    tree = post(client, f"/api/repos/{uploaded_repo}/tree", {"path": "src"}).get_json()
    assert {c["name"] for c in tree["children"]} == {"deep", "a.txt", "b.txt"}

    obj = post(
        client, f"/api/repos/{uploaded_repo}/object", {"path": "src/b.txt"}
    ).get_json()
    assert obj["file"]["churn"] == 8
    assert obj["history"][0]["hash"] == h["c6"]

    found = client.get(f"/api/repos/{uploaded_repo}/objects/search?q=deep").get_json()
    assert found["results"] == ["src/deep/c.txt", "src/deep/d.txt"]

    # path filter: scope reflects src, repo metrics stay H-wide
    scoped = post(client, f"/api/repos/{uploaded_repo}/dashboard", {"path": "src"}).get_json()
    assert scoped["scope"]["dir"]["churn"] == 20
    assert scoped["repo_metrics"]["churn"] == 32


def test_ref_ensure_flow(client, uploaded_repo):
    # tag v1 exists but its reachability set has not been materialised yet
    res = post(client, f"/api/repos/{uploaded_repo}/dashboard", {"ref": "v1"})
    assert res.status_code == 409

    res = post(client, f"/api/repos/{uploaded_repo}/refs/ensure", {"ref": "v1"})
    assert res.status_code in (200, 202)
    wait_ref_cached(uploaded_repo, "v1")
    wait_ready(client, uploaded_repo)

    d = post(client, f"/api/repos/{uploaded_repo}/dashboard", {"ref": "v1"}).get_json()
    assert d["summary"]["commit_count"] == 3
    assert d["summary"]["added"] == 17

    # re-ensuring a cached ref is a no-op
    res = post(client, f"/api/repos/{uploaded_repo}/refs/ensure", {"ref": "v1"})
    assert res.status_code == 200
    assert res.get_json()["cached"] is True


def test_authors_merge_endpoints(client, uploaded_repo):
    body = client.get(f"/api/repos/{uploaded_repo}/authors").get_json()
    keys = {i["key"] for i in body["identities"]}
    assert len(keys) == 3 and ROBERT in keys

    body = post(
        client,
        f"/api/repos/{uploaded_repo}/authors/merge",
        {"canonical": BOB, "keys": [ROBERT]},
    ).get_json()
    assert body["groups"] == {BOB: [ROBERT]}

    d = post(client, f"/api/repos/{uploaded_repo}/dashboard", {}).get_json()
    authors = {a["name"]: a for a in d["authors"]}
    assert authors[BOB]["churn"] == 14
    assert d["summary"]["authors"] == 2

    body = post(
        client, f"/api/repos/{uploaded_repo}/authors/unmerge", {"canonical": BOB}
    ).get_json()
    assert body["groups"] == {}
    d = post(client, f"/api/repos/{uploaded_repo}/dashboard", {}).get_json()
    assert {a["name"] for a in d["authors"]} == {BOB, ROBERT, "Alice <alice@example.com>"}


def test_compare_endpoint(client, uploaded_repo):
    body = client.get("/api/compare").get_json()
    row = next(r for r in body["repos"] if r["id"] == uploaded_repo)
    assert row["commits"] == 9
    assert row["churn"] == 32
    assert row["growth"] == 20

    filtered = client.get(f"/api/compare?ids={uploaded_repo}").get_json()
    assert [r["id"] for r in filtered["repos"]] == [uploaded_repo]


def test_delete_repo_flow(client, synth_repo):
    zip_path = make_repo_zip(synth_repo["path"], synth_repo["path"].parent / "del.zip")
    res = client.post(
        "/api/repos/upload",
        data={"file": (io.BytesIO(zip_path.read_bytes()), "del.zip")},
        content_type="multipart/form-data",
    )
    repo_id = res.get_json()["repo"]["id"]
    wait_ready(client, repo_id)

    assert client.delete(f"/api/repos/{repo_id}").status_code == 200
    assert client.get(f"/api/repos/{repo_id}/meta").status_code == 404
    assert client.get(f"/api/repos/{repo_id}/commit/{'0' * 40}").status_code == 404


def test_upload_archive_without_git_directory(client, tmp_path):
    zip_path = tmp_path / "plain.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("notes.txt", "just files, no repository")
    res = client.post(
        "/api/repos/upload",
        data={"file": (io.BytesIO(zip_path.read_bytes()), "plain.zip")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 202
    repo_id = res.get_json()["repo"]["id"]
    row = wait_ready(client, repo_id)
    assert row["status"] == "error"
    assert ".git" in (row["error"] or "")
    client.delete(f"/api/repos/{repo_id}")


def test_upload_rejects_non_zip(client):
    res = client.post(
        "/api/repos/upload",
        data={"file": (io.BytesIO(b"hello"), "notes.txt")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 400
    assert "zip" in res.get_json()["error"]

    res = client.post("/api/repos/upload", data={}, content_type="multipart/form-data")
    assert res.status_code == 400


def test_error_responses(client, uploaded_repo):
    # unknown repository
    assert post(client, "/api/repos/999999/dashboard", {}).status_code == 404
    assert client.get("/api/repos/999999/meta").status_code == 404

    # invalid bucket
    res = post(client, f"/api/repos/{uploaded_repo}/dashboard", {"bucket": "hour"})
    assert res.status_code == 400

    # invalid ref fails fast with git's message
    res = post(client, f"/api/repos/{uploaded_repo}/refs/ensure", {"ref": "no-such-ref"})
    assert res.status_code == 400

    # clone rejects unsupported URLs
    res = post(client, "/api/repos/clone", {"url": "ftp://example.com/repo.git"})
    assert res.status_code == 400

    # unknown commit hash
    res = client.get(f"/api/repos/{uploaded_repo}/commit/{'f' * 40}")
    assert res.status_code == 404
