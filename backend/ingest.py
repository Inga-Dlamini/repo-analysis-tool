"""Repository ingestion: zip upload, remote clone, reference building.

All long running work (cloning, parsing) happens on daemon worker threads and
reports progress through the ``repos`` row, which the UI polls.  Parsing is a
single streaming pass per repository: every non-merge commit reachable from
HEAD is stored, plus one row per changed path.

Reference handling: metric queries are scoped to the set of non-merge commits
reachable from a chosen reference (HEAD by default).  ``ensure_ref`` builds
that reachability set (``git rev-list --no-merges <ref>``), parses any commits
that are not yet stored (batched with ``--no-walk``), and caches the set in
``ref_commits``.  Re-selecting a cached ref is therefore instant.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import db
from .gitparse import (
    PARSER_VERSION,
    GitError,
    head_commit,
    iter_log,
    list_refs,
    rev_list,
    rev_list_count,
    rev_parse,
    run_git,
)

BATCH_COMMITS = 2000
WALK_BATCH = 1000
CLONE_TIMEOUT = 3600
MAX_UNZIP_BYTES = int(os.environ.get("RAT_MAX_UNZIP_BYTES", str(16 * 1024**3)))

_URL_PREFIXES = ("http://", "https://", "git://", "ssh://", "git@")


class IngestError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Job plumbing


class JobRunner:
    """One worker per repository at a time; job state lives in the database."""

    def __init__(self) -> None:
        self._threads: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()

    def busy(self, repo_id: int) -> bool:
        with self._lock:
            t = self._threads.get(repo_id)
            return bool(t and t.is_alive())

    def submit(self, repo_id: int, fn, *args) -> bool:
        with self._lock:
            t = self._threads.get(repo_id)
            if t and t.is_alive():
                return False
            thread = threading.Thread(
                target=self._wrap, args=(repo_id, fn, args), daemon=True
            )
            self._threads[repo_id] = thread
            thread.start()
            return True

    def _wrap(self, repo_id: int, fn, args: tuple) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - surfaced through repo status
            _fail(repo_id, str(exc))


JOBS = JobRunner()


def _set_status(conn, repo_id: int, status: str, detail: str | None = None, *, error=None) -> None:
    conn.execute(
        "UPDATE repos SET status=?, status_detail=?, error=?, updated_at=? WHERE id=?",
        (status, detail, error, db.now(), repo_id),
    )
    conn.commit()


def _fail(repo_id: int, message: str) -> None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT status FROM repos WHERE id=?", (repo_id,)).fetchone()
        if row is None:
            return
        # A failure while the repo was already usable degrades to a soft error.
        soft = row["status"] in ("ready", "refreshing", "error") and _has_commits(conn, repo_id)
        _set_status(
            conn,
            repo_id,
            "ready" if soft else "error",
            "last job failed" if soft else None,
            error=message[:2000],
        )
    finally:
        conn.close()


def _has_commits(conn, repo_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM commits WHERE repo_id=? LIMIT 1", (repo_id,)
    ).fetchone()
    return row is not None


def get_repo(conn, repo_id: int):
    return conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()


# ---------------------------------------------------------------------------
# Repository creation


def create_url_repo(url: str, name: str | None = None) -> int:
    url = (url or "").strip()
    if not url or not url.lower().startswith(_URL_PREFIXES):
        raise IngestError(
            "unsupported URL: use an http(s), git, ssh URL or git@host:path form"
        )
    display = (name or "").strip() or _default_name_from_url(url)
    repo_id = _insert_repo("url", url, display)
    JOBS.submit(repo_id, _ingest_url, repo_id, url)
    return repo_id


def create_zip_repo(zip_path: str, name: str | None = None) -> int:
    display = (name or "").strip() or Path(zip_path).stem
    repo_id = _insert_repo("zip", Path(zip_path).name, display)
    JOBS.submit(repo_id, _ingest_zip, repo_id, zip_path)
    return repo_id


def _default_name_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or "repository"


def _insert_repo(source_type: str, source: str, name: str) -> int:
    conn = db.connect()
    try:
        cur = conn.execute(
            """INSERT INTO repos (name, source_type, source, path, status, status_detail,
                                  created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, source_type, source, "", "pending", "queued", db.now(), db.now()),
        )
        conn.commit()
        repo_id = int(cur.lastrowid)
    finally:
        conn.close()
    repo_dir = os.path.join(db.repos_dir(), str(repo_id))
    os.makedirs(repo_dir, exist_ok=True)
    conn = db.connect()
    try:
        conn.execute("UPDATE repos SET path=? WHERE id=?", (repo_dir, repo_id))
        conn.commit()
    finally:
        conn.close()
    return repo_id


# ---------------------------------------------------------------------------
# Ingestion tasks


def _ingest_url(repo_id: int, url: str) -> None:
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            return
        dest = repo["path"]
        _set_status(conn, repo_id, "cloning", f"git clone {url}")
    finally:
        conn.close()

    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(
        ["git", "clone", "--quiet", "--", url, "."],
        cwd=dest,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=CLONE_TIMEOUT,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[:2000].strip()
        raise IngestError(f"clone failed: {err or 'unknown error'}")
    _finish_ingest(repo_id)


def _ingest_zip(repo_id: int, zip_path: str) -> None:
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            return
        dest = repo["path"]
        _set_status(conn, repo_id, "loading", "extracting archive")
    finally:
        conn.close()

    try:
        _safe_extract(zip_path, dest)
        git_root = _find_git_root(dest)
        if git_root is None:
            raise IngestError(
                "no .git directory found in the archive — upload a zip of the "
                "repository including its .git directory"
            )
        if git_root != dest:
            # flatten the single top level folder so the repo lives at dest
            _flatten_single_root(dest, git_root)
        _finish_ingest(repo_id)
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass


def _safe_extract(zip_path: str, dest: str) -> None:
    dest_path = Path(dest).resolve()
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > 500_000:
            raise IngestError("archive has too many entries")
        for info in infos:
            name = info.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts:
                raise IngestError(f"unsafe path in archive: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue  # skip symlinks: not needed for analysis
            target = (dest_path / pure).resolve()
            if not str(target).startswith(str(dest_path)):
                raise IngestError(f"unsafe path in archive: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += info.file_size
            if total > MAX_UNZIP_BYTES:
                raise IngestError("archive is too large when uncompressed")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def _find_git_root(dest: str) -> str | None:
    """Locate the repository root containing .git (max depth 2)."""
    candidates = [dest]
    try:
        for entry in os.scandir(dest):
            if entry.is_dir() and not entry.name.startswith("."):
                candidates.append(entry.path)
                try:
                    for sub in os.scandir(entry.path):
                        if sub.is_dir() and not sub.name.startswith("."):
                            candidates.append(sub.path)
                except OSError:
                    pass
    except OSError:
        pass
    for cand in candidates:
        gitdir = os.path.join(cand, ".git")
        if os.path.isdir(gitdir):
            return cand
        if os.path.isfile(gitdir):
            raise IngestError(
                "the archive contains a .git file (worktree/submodule pointer) "
                "instead of a .git directory; re-zip the actual repository"
            )
    return None


def _flatten_single_root(dest: str, git_root: str) -> None:
    tmp = dest + ".tmp-flatten"
    os.rename(git_root, tmp)
    for entry in os.scandir(dest):
        try:
            shutil.rmtree(entry.path) if entry.is_dir() else os.remove(entry.path)
        except OSError:
            pass
    for entry in os.scandir(tmp):
        os.rename(entry.path, os.path.join(dest, entry.name))
    shutil.rmtree(tmp, ignore_errors=True)


def _finish_ingest(repo_id: int) -> None:
    """Validate the checkout, parse HEAD history, build the HEAD ref set."""
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            return
        path = repo["path"]
        try:
            run_git(path, "rev-parse", "--git-dir", timeout=120)
        except GitError as exc:
            raise IngestError(f"not a usable git repository: {exc}") from exc
        head = head_commit(path)
        if not head:
            raise IngestError("repository has no commits (no HEAD)")
        _set_status(conn, repo_id, "parsing", "counting commits")
        total = rev_list_count(path, "--no-merges", "HEAD")
        conn.execute(
            "UPDATE repos SET head=?, commit_total=?, commit_parsed=0 WHERE id=?",
            (head, total, repo_id),
        )
        conn.commit()
    finally:
        conn.close()

    _ensure_ref_inner(repo_id, "HEAD", label="HEAD")

    conn = db.connect()
    try:
        _refresh_counts(conn, repo_id)
        _set_status(conn, repo_id, "ready", None, error=None)
    finally:
        conn.close()


def _refresh_counts(conn, repo_id: int) -> None:
    parsed = conn.execute(
        "SELECT COUNT(*) c FROM commits WHERE repo_id=?", (repo_id,)
    ).fetchone()["c"]
    files = conn.execute(
        "SELECT COUNT(DISTINCT path) c FROM file_changes WHERE repo_id=?", (repo_id,)
    ).fetchone()["c"]
    authors = conn.execute(
        "SELECT COUNT(DISTINCT author_key) c FROM commits WHERE repo_id=?", (repo_id,)
    ).fetchone()["c"]
    conn.execute(
        """UPDATE repos SET commit_parsed=?, file_count=?, author_count=?,
                            parser_version=?, updated_at=? WHERE id=?""",
        (parsed, files, authors, PARSER_VERSION, db.now(), repo_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Parsing


def _parse_commits(
    conn,
    repo_id: int,
    path: str,
    *,
    revisions: list[str] | None = None,
    exclude: list[str] | None = None,
    walk_hashes: list[str] | None = None,
    expected: int | None = None,
    seq_start: int = 0,
    detail: str = "parsing",
) -> int:
    """Stream commits into the database.  Returns the number parsed.

    Commits are stored oldest-first via ``seq`` (the stream is newest-first,
    so the sequence is assigned by countdown once the total is known).
    """
    total = expected
    if total is None:
        try:
            if walk_hashes:
                total = len(walk_hashes)
            else:
                total = rev_list_count(path, "--no-merges", *(revisions or ["HEAD"]))
        except GitError:
            total = None

    batch: list = []
    parsed = 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        commit_rows = []
        file_rows = []
        base = parsed - len(batch)  # global (0-based) index of batch[0]
        for i, c in enumerate(batch):
            idx = base + i
            # stream is newest-first: newest commit gets the highest seq
            seq = seq_start + (total - 1 - idx) if total else seq_start + idx
            commit_rows.append(
                (
                    repo_id,
                    c.hash,
                    c.parent,
                    c.ts,
                    c.author_name,
                    c.author_email,
                    c.author_key,
                    c.subject,
                    sum(f.added for f in c.files),
                    sum(f.removed for f in c.files),
                    seq,
                )
            )
            for f in c.files:
                file_rows.append((repo_id, c.hash, f.path, f.added, f.removed))
        conn.executemany(
            """INSERT OR REPLACE INTO commits
               (repo_id, hash, parent, ts, author_name, author_email, author_key,
                subject, added, removed, seq)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            commit_rows,
        )
        if file_rows:
            conn.executemany(
                """INSERT INTO file_changes (repo_id, commit_hash, path, added, removed)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(repo_id, commit_hash, path) DO UPDATE SET
                       added = added + excluded.added,
                       removed = removed + excluded.removed""",
                file_rows,
            )
        conn.commit()
        batch = []

    for commit in iter_log(path, revisions=revisions, exclude=exclude, walk_hashes=walk_hashes):
        batch.append(commit)
        parsed += 1
        if len(batch) >= BATCH_COMMITS:
            flush()
            if conn.execute(
                "SELECT 1 FROM repos WHERE id=?", (repo_id,)
            ).fetchone() is None:
                raise IngestError("repository was removed during parsing")
            conn.execute(
                "UPDATE repos SET commit_parsed=commit_parsed+?, status_detail=?, updated_at=? WHERE id=?",
                (parsed, f"{detail}: {parsed}" + (f"/{total}" if total else ""), db.now(), repo_id),
            )
            conn.commit()
    flush()
    return parsed


def _max_seq(conn, repo_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) s FROM commits WHERE repo_id=?", (repo_id,)
    ).fetchone()
    return int(row["s"])


# ---------------------------------------------------------------------------
# References


def ensure_ref(repo_id: int, ref: str) -> None:
    """Blocking version used from request handlers when the caller explicitly
    asked for the ref to be materialised."""
    _ensure_ref_inner(repo_id, ref)


def ensure_ref_async(repo_id: int, ref: str) -> bool:
    return JOBS.submit(repo_id, _ensure_ref_job, repo_id, ref)


def _ensure_ref_job(repo_id: int, ref: str) -> None:
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            return
        _set_status(conn, repo_id, "refreshing", f"preparing ref {ref}")
    finally:
        conn.close()
    _ensure_ref_inner(repo_id, ref)
    conn = db.connect()
    try:
        if get_repo(conn, repo_id) is not None:
            _set_status(conn, repo_id, "ready", None, error=None)
    finally:
        conn.close()


def _ensure_ref_inner(repo_id: int, ref: str, label: str | None = None) -> None:
    """Guarantee: every non-merge commit reachable from `ref` is parsed and
    the reachability set is cached in ref_commits."""
    display = label or ref
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            raise IngestError("repository not found")
        path = repo["path"]
        full_hash = rev_parse(path, ref)  # raises GitError for unknown refs
        cached = conn.execute(
            "SELECT hash FROM refs WHERE repo_id=? AND ref=?", (repo_id, ref)
        ).fetchone()
        if cached is not None and cached["hash"] == full_hash:
            return

        hashes = rev_list(path, "--no-merges", full_hash)
        hashset = set(hashes)
        parsed_hashes = {
            row["hash"]
            for row in conn.execute("SELECT hash FROM commits WHERE repo_id=?", (repo_id,))
        }
        missing = [h for h in hashes if h not in parsed_hashes]
        if missing:
            seq = _max_seq(conn, repo_id) + 1
            detail = f"parsing ref {display}"
            done = 0
            # rev_list returns newest-first; parse oldest-first in chunks so
            # `seq` stays ascending in commit order across chunk boundaries.
            chunks = [missing[i : i + WALK_BATCH] for i in range(0, len(missing), WALK_BATCH)]
            for chunk in reversed(chunks):
                done += _parse_commits(
                    conn,
                    repo_id,
                    path,
                    walk_hashes=chunk,
                    expected=len(chunk),
                    seq_start=seq,
                    detail=detail,
                )
                seq += len(chunk)
                conn.execute(
                    """UPDATE repos SET
                         commit_parsed=(SELECT COUNT(*) FROM commits WHERE repo_id=?),
                         status_detail=?, updated_at=? WHERE id=?""",
                    (repo_id, f"{detail}: {done}/{len(missing)}", db.now(), repo_id),
                )
                conn.commit()
        conn.executemany(
            "INSERT OR IGNORE INTO ref_commits (repo_id, ref, hash) VALUES (?,?,?)",
            ((repo_id, ref, h) for h in hashes),
        )
        conn.execute(
            """INSERT INTO refs (repo_id, ref, hash, commit_count, built_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(repo_id, ref) DO UPDATE SET
                   hash=excluded.hash, commit_count=excluded.commit_count,
                   built_at=excluded.built_at""",
            (repo_id, ref, full_hash, len(hashset), db.now()),
        )
        conn.commit()
    finally:
        conn.close()


def ref_is_ready(conn, repo_id: int, ref: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM refs WHERE repo_id=? AND ref=?", (repo_id, ref)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Maintenance actions


def refresh_repo(repo_id: int) -> None:
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            raise IngestError("repository not found")
        if repo["source_type"] != "url":
            raise IngestError("refresh is only available for cloned repositories; use rebuild instead")
        path = repo["path"]
        _set_status(conn, repo_id, "refreshing", "fetching updates")
    finally:
        conn.close()

    subprocess.run(
        ["git", "-C", path, "fetch", "--quiet", "--all", "--prune"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=CLONE_TIMEOUT,
    )
    # best effort: fast-forward the local branch to its upstream
    subprocess.run(
        ["git", "-C", path, "merge", "--ff-only", "--quiet", "@{u}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    _finish_ingest(repo_id)


def rebuild_repo(repo_id: int) -> None:
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            raise IngestError("repository not found")
        conn.execute("DELETE FROM file_changes WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM ref_commits WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM refs WHERE repo_id=?", (repo_id,))
        conn.execute("UPDATE repos SET commit_parsed=0 WHERE id=?", (repo_id,))
        conn.commit()
        _set_status(conn, repo_id, "parsing", "rebuilding")
    finally:
        conn.close()
    _finish_ingest(repo_id)


def delete_repo(repo_id: int) -> None:
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            return
        path = repo["path"]
        conn.execute("DELETE FROM file_changes WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM ref_commits WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM refs WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM author_aliases WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM repos WHERE id=?", (repo_id,))
        conn.commit()
    finally:
        conn.close()
    if path:
        shutil.rmtree(path, ignore_errors=True)


def repo_refs(repo_id: int) -> list[dict]:
    conn = db.connect()
    try:
        repo = get_repo(conn, repo_id)
        if repo is None:
            raise IngestError("repository not found")
        path = repo["path"]
        built = {
            row["ref"]: row
            for row in conn.execute(
                "SELECT ref, hash, commit_count FROM refs WHERE repo_id=?", (repo_id,)
            )
        }
    finally:
        conn.close()
    refs = list_refs(path)
    seen: set[str] = set()
    for r in refs:
        seen.add(r["name"])
        row = built.get(r["name"])
        r["ready"] = bool(row and row["hash"] == r["hash"])
        r["built"] = row["hash"] if row else None
        r["commits"] = row["commit_count"] if row else None
    # refs prepared earlier that are not branch/tag names (e.g. custom commit
    # hashes entered in the UI): keep offering them so dashboard URLs built
    # from such a ref remain resolvable after a reload.
    for name, row in built.items():
        if name in seen:
            continue
        refs.append({
            "name": name,
            "hash": row["hash"],
            "ready": True,
            "built": row["hash"],
            "commits": row["commit_count"],
        })
    return refs
