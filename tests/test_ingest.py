"""Ingestion tests: rebuilds, chunked walks, archive safety, URL validation."""

from __future__ import annotations

import stat
import zipfile

import pytest

from backend import ingest, metrics
from backend.gitparse import PARSER_VERSION
from conftest import ingest_repo


def _dashboard(conn, repo_id, **kw):
    payload = {"ref": "HEAD"}
    payload.update(kw)
    return metrics.dashboard(conn, repo_id, payload)


def test_ingest_records_repo_metadata(conn, repo_id, synth_repo):
    row = ingest.get_repo(conn, repo_id)
    assert row["status"] == "ready"
    assert row["head"] == synth_repo["hashes"]["head"]
    assert row["commit_parsed"] == 9
    assert row["commit_total"] == 9
    assert row["file_count"] == 8
    assert row["author_count"] == 3
    assert row["parser_version"] == PARSER_VERSION


def test_rebuild_reproduces_metrics(conn, repo_id, synth_repo, monkeypatch):
    # small batch size exercises the multi-batch flush path
    monkeypatch.setattr(ingest, "BATCH_COMMITS", 3)
    before = _dashboard(conn, repo_id)

    ingest.rebuild_repo(repo_id)
    assert ingest.get_repo(conn, repo_id)["status"] == "ready"

    after = _dashboard(conn, repo_id)
    assert after["summary"] == before["summary"]
    assert after["repo_metrics"] == before["repo_metrics"]
    assert after["top_files"] == before["top_files"]
    assert after["authors"] == before["authors"]


def test_ensure_ref_chunked_walk_restores_full_history(conn, repo_id, synth_repo, monkeypatch):
    # wipe the parsed data, then re-ensure HEAD through many small walk chunks
    monkeypatch.setattr(ingest, "WALK_BATCH", 2)
    conn.execute("DELETE FROM file_changes WHERE repo_id=?", (repo_id,))
    conn.execute("DELETE FROM commits WHERE repo_id=?", (repo_id,))
    conn.execute("DELETE FROM ref_commits WHERE repo_id=?", (repo_id,))
    conn.execute("DELETE FROM refs WHERE repo_id=?", (repo_id,))
    conn.commit()

    ingest.ensure_ref(repo_id, "HEAD")

    d = _dashboard(conn, repo_id)
    assert d["summary"]["commit_count"] == 9
    assert d["summary"]["added"] == 26
    assert d["summary"]["removed"] == 6

    # `seq` is unique and strictly ascending in commit-time order even though
    # the history was parsed in chunks
    rows = conn.execute(
        "SELECT ts, seq FROM commits WHERE repo_id=? ORDER BY ts, hash", (repo_id,)
    ).fetchall()
    seqs = [r["seq"] for r in rows]
    assert len(set(seqs)) == 9
    assert seqs == sorted(seqs)


def test_ensure_ref_is_idempotent(conn, repo_id):
    ingest.ensure_ref(repo_id, "HEAD")
    ingest.ensure_ref(repo_id, "HEAD")
    assert _dashboard(conn, repo_id)["summary"]["commit_count"] == 9


def test_repo_refs_include_ensured_custom_refs(conn, repo_id, synth_repo):
    hashes = synth_repo["hashes"]
    ingest.ensure_ref(repo_id, hashes["c5"])
    refs = {r["name"]: r for r in ingest.repo_refs(repo_id)}
    entry = refs[hashes["c5"]]              # offered even though it is no ref name
    assert entry["ready"] is True
    assert entry["commits"] == 5            # c1..c5 reachable from the hash
    assert refs["HEAD"]["ready"] is True    # git refs are unaffected


def test_ensure_ref_unknown_ref_raises(repo_id):
    from backend.gitparse import GitError

    with pytest.raises(GitError):
        ingest.ensure_ref(repo_id, "does-not-exist")


def test_safe_extract_rejects_traversal(tmp_path):
    bad = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../escape.txt", "boom")
    with pytest.raises(ingest.IngestError):
        ingest._safe_extract(str(bad), str(tmp_path / "out"))
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_skips_symlinks(tmp_path):
    zip_path = tmp_path / "link.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        info = zipfile.ZipInfo("link.txt")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
        zf.writestr("real.txt", "ok")
    out = tmp_path / "out"
    ingest._safe_extract(str(zip_path), str(out))
    assert (out / "real.txt").read_text() == "ok"
    assert not (out / "link.txt").exists()


def test_safe_extract_rejects_oversized_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_UNZIP_BYTES", 4)
    zip_path = tmp_path / "big.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("data.txt", "0123456789")
    with pytest.raises(ingest.IngestError):
        ingest._safe_extract(str(zip_path), str(tmp_path / "out"))


def test_find_git_root_locates_nested_repo(tmp_path):
    (tmp_path / "outer" / "inner").mkdir(parents=True)
    (tmp_path / "outer" / "inner" / ".git").mkdir()
    assert ingest._find_git_root(str(tmp_path)) == str(tmp_path / "outer" / "inner")


def test_find_git_root_rejects_gitfile(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".git").write_text("gitdir: /elsewhere")
    with pytest.raises(ingest.IngestError):
        ingest._find_git_root(str(tmp_path))


def test_create_url_repo_validates_scheme(conn):
    before = conn.execute("SELECT COUNT(*) AS n FROM repos").fetchone()["n"]
    for bad in ("", "ftp://host/repo.git", "file:///tmp/x", "/local/path", "gitlab.com/x/y"):
        with pytest.raises(ingest.IngestError):
            ingest.create_url_repo(bad)
    # nothing was inserted for the rejected URLs
    assert conn.execute("SELECT COUNT(*) AS n FROM repos").fetchone()["n"] == before


def test_delete_repo_removes_everything(conn, synth_repo):
    rid = ingest_repo(synth_repo["path"], name="to-delete")
    path = ingest.get_repo(conn, rid)["path"]
    ingest.ensure_ref(rid, "v1")
    ingest.delete_repo(rid)
    assert ingest.get_repo(conn, rid) is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM commits WHERE repo_id=?", (rid,)
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM refs WHERE repo_id=?", (rid,)
    ).fetchone()["n"] == 0
    import os

    assert not os.path.exists(path)
