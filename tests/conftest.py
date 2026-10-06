"""Shared fixtures for the RAT test suite.

Every test builds its own deterministic synthetic git repository (fixed
identities, dates, renames, binaries, a merge and an empty commit) and feeds
it through the real ingestion / metric code paths.  All runtime state lives
in one session-scoped temporary data directory so the SQLite schema is
created exactly once.

The canonical fixture history (oldest first, all times UTC, `T0` = 1600000000
and `ts(i) = T0 + i * 100000`):

    i=1   c1  Alice          README.md +2, src/a.txt +3
    i=2   c2  Bob            src/a.txt +2/-1, src/b.txt +4
    i=3   c3  Alice Smith    src/deep/c.txt +5, README.md +1/-1   (mailmapped)
    i=4   c4  Bob            rename c.txt -> d.txt (pure), binary assets/logo.bin
    i=5   c5  Alice          src/deep/d.txt +1
    i=6   c6  Bob            delete src/b.txt (-4)
    i=7   b7  Robert         feature/notes.md +3     (branch "feature")
    i=8   --  MERGE          excluded from every metric (--no-merges)
    i=9   c8  Alice          empty commit (0 +/-)
    i=10  c9  Alice          src2/z.txt +2, docs/100%_coverage.md +3
    tag v1 -> c3

Non-merge commits reachable from HEAD, |H| = 9:
    added 26, removed 6, growth 20, churn 32, repo modifications 7.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Must be set before backend.db is imported anywhere in the test session:
# data_dir() is consulted on every connection.
_TEST_DATA = tempfile.mkdtemp(prefix="rat-test-data-")
os.environ["RAT_DATA_DIR"] = _TEST_DATA

from backend import db, ingest  # noqa: E402  (import after env setup)

T0 = 1_600_000_000
STEP = 100_000

ALICE = "Alice <alice@example.com>"
BOB = "Bob <bob@example.com>"
ROBERT = "Robert <robert@example.com>"
LEGACY_ALICE = "Alice Smith <asmith@old.example>"  # mailmapped to ALICE


def ts(i: int) -> int:
    return T0 + i * STEP


def _iso(i: int) -> str:
    return _dt.datetime.fromtimestamp(ts(i), _dt.timezone.utc).isoformat()


class GitHarness:
    """Run git with deterministic identities, dates and config."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def run(
        self,
        *args: str,
        i: int = 0,
        name: str = "Alice",
        email: str = "alice@example.com",
    ) -> str:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
                "GIT_AUTHOR_DATE": _iso(i),
                "GIT_COMMITTER_DATE": _iso(i),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args], env=env, capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD")


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_synthetic_repo(root: Path) -> dict:
    """Create the canonical fixture repository (see module docstring)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    g = GitHarness(root)
    g.run("init", "-q", "-b", "main")
    g.run("config", "commit.gpgsign", "false")
    g.run("config", "user.name", "Harness")
    g.run("config", "user.email", "harness@example.com")

    hashes: dict[str, str] = {}

    _write(root, "README.md", "line1\nline2\n")
    _write(root, "src/a.txt", "a1\na2\na3\n")
    g.run("add", "-A")
    g.run("commit", "-q", "-m", "c1: initial import", i=1)
    hashes["c1"] = g.head()

    _write(root, "src/a.txt", "a1\naX\naY\na3\n")
    _write(root, "src/b.txt", "b1\nb2\nb3\nb4\n")
    g.run("add", "-A")
    g.run("commit", "-q", "-m", "c2: tweak a, add b", i=2, name="Bob", email="bob@example.com")
    hashes["c2"] = g.head()

    _write(root, "src/deep/c.txt", "c1\nc2\nc3\nc4\nc5\n")
    _write(root, "README.md", "line1\nlineX\n")
    g.run("add", "-A")
    g.run(
        "commit", "-q", "-m", "c3: deep file and readme",
        i=3, name="Alice Smith", email="asmith@old.example",
    )
    hashes["c3"] = g.head()

    g.run("mv", "src/deep/c.txt", "src/deep/d.txt")
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "logo.bin").write_bytes(bytes([0, 1, 2, 3, 0, 255, 254, 128]))
    g.run("add", "-A")
    g.run("commit", "-q", "-m", "c4: rename + binary", i=4, name="Bob", email="bob@example.com")
    hashes["c4"] = g.head()

    _write(root, "src/deep/d.txt", "c1\nc2\nc3\nc4\nc5\nc6\n")
    g.run("add", "-A")
    g.run("commit", "-q", "-m", "c5: extend deep file", i=5)
    hashes["c5"] = g.head()

    g.run("rm", "-q", "src/b.txt")
    g.run("commit", "-q", "-m", "c6: drop b", i=6, name="Bob", email="bob@example.com")
    hashes["c6"] = g.head()

    g.run("checkout", "-q", "-b", "feature")
    _write(root, "feature/notes.md", "# notes\n\ndetail\n")
    g.run("add", "-A")
    g.run("commit", "-q", "-m", "b7: feature notes", i=7, name="Robert", email="robert@example.com")
    hashes["b7"] = g.head()

    g.run("checkout", "-q", "main")
    g.run("merge", "-q", "--no-ff", "-m", "merge feature", "feature", i=8)
    hashes["merge"] = g.head()

    g.run("commit", "-q", "--allow-empty", "-m", "c8: empty commit", i=9)
    hashes["c8"] = g.head()

    _write(root, "src2/z.txt", "z1\nz2\n")
    _write(root, "docs/100%_coverage.md", "d1\nd2\nd3\n")
    g.run("add", "-A")
    g.run("commit", "-q", "-m", "c9: unrelated paths", i=10)
    hashes["c9"] = g.head()

    g.run("tag", "v1", hashes["c3"])
    # Written last and deliberately left untracked: mailmap resolves
    # LEGACY_ALICE -> ALICE for `git log --use-mailmap` without polluting the
    # file metrics with a .mailmap entry.
    (root / ".mailmap").write_text(
        f"Alice <alice@example.com> {LEGACY_ALICE}\n", encoding="utf-8"
    )
    hashes["head"] = g.head()
    return {"path": root, "hashes": hashes}


def ingest_repo(src: Path, name: str = "synthetic") -> int:
    """Create a repo row and run the real synchronous ingestion path on a
    copy of `src` (used instead of the threaded zip/clone entry points)."""
    db.init_db()
    repo_id = ingest._insert_repo("zip", str(src), name)
    dest = Path(db.repos_dir()) / str(repo_id)
    for entry in os.scandir(src):
        target = dest / entry.name
        if entry.is_dir():
            shutil.copytree(entry.path, target)
        else:
            shutil.copy2(entry.path, target)
    ingest._finish_ingest(repo_id)
    return repo_id


def make_repo_zip(src: Path, dest: Path, prefix: str = "synthetic/") -> Path:
    """Zip a repository (including .git) under a single top level folder."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            arc = prefix + p.relative_to(src).as_posix()
            if p.is_dir():
                zf.writestr(arc + "/", b"")
            else:
                zf.write(p, arc)
    return Path(dest)


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture(scope="session", autouse=True)
def _session_data_cleanup():
    yield
    shutil.rmtree(_TEST_DATA, ignore_errors=True)


@pytest.fixture()
def synth_repo(tmp_path) -> dict:
    return build_synthetic_repo(tmp_path / "synthetic")


@pytest.fixture()
def repo_id(synth_repo) -> int:
    rid = ingest_repo(synth_repo["path"], name="synthetic")
    yield rid
    ingest.delete_repo(rid)


@pytest.fixture()
def conn():
    db.init_db()
    c = db.connect()
    yield c
    c.close()


@pytest.fixture()
def client():
    from backend.app import app

    return app.test_client()
