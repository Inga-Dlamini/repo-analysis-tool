"""SQLite persistence layer for the Repo Analysis Tool.

A single SQLite database in WAL mode stores every ingested repository.
The schema is deliberately narrow: one row per commit plus one row per
(commit, changed path) pair.  Everything the metric engine needs is derived
from those two tables, which keeps ingestion a single streaming pass over
`git log`.

Layout of the runtime data directory (``data/`` by default, override with
the ``RAT_DATA_DIR`` environment variable)::

    data/
      rat.sqlite3        the database
      repos/<id>/        one checkout/clone per ingested repository
      uploads/           staging area for uploaded zip archives
"""

from __future__ import annotations

import os
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# On-disk schema version, stored via PRAGMA user_version and upgraded by
# _migrate() below.  v2: repos.source_type also accepts 'path' (local directory).
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    source_type    TEXT    NOT NULL CHECK (source_type IN ('zip', 'url', 'path')),
    source         TEXT    NOT NULL DEFAULT '',
    path           TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'pending',
    status_detail  TEXT,
    error          TEXT,
    head           TEXT,
    commit_total   INTEGER NOT NULL DEFAULT 0,
    commit_parsed  INTEGER NOT NULL DEFAULT 0,
    file_count     INTEGER NOT NULL DEFAULT 0,
    author_count   INTEGER NOT NULL DEFAULT 0,
    parser_version INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
    repo_id      INTEGER NOT NULL,
    hash         TEXT    NOT NULL,
    parent       TEXT,
    ts           INTEGER NOT NULL,
    author_name  TEXT    NOT NULL,
    author_email TEXT    NOT NULL,
    author_key   TEXT    NOT NULL,
    subject      TEXT    NOT NULL DEFAULT '',
    added        INTEGER NOT NULL DEFAULT 0,
    removed      INTEGER NOT NULL DEFAULT 0,
    seq          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo_id, hash)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_commits_ts     ON commits(repo_id, ts);
CREATE INDEX IF NOT EXISTS idx_commits_author ON commits(repo_id, author_key);

CREATE TABLE IF NOT EXISTS file_changes (
    repo_id     INTEGER NOT NULL,
    commit_hash TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    added       INTEGER NOT NULL,
    removed     INTEGER NOT NULL,
    PRIMARY KEY (repo_id, commit_hash, path)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_fc_path ON file_changes(repo_id, path);

-- Reachability cache: one row per (ref, commit) for refs that have been
-- "ensured" (all reachable commits parsed and the set materialised).
CREATE TABLE IF NOT EXISTS refs (
    repo_id      INTEGER NOT NULL,
    ref          TEXT    NOT NULL,
    hash         TEXT    NOT NULL,
    commit_count INTEGER NOT NULL DEFAULT 0,
    built_at     INTEGER NOT NULL,
    PRIMARY KEY (repo_id, ref)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ref_commits (
    repo_id INTEGER NOT NULL,
    ref     TEXT    NOT NULL,
    hash    TEXT    NOT NULL,
    PRIMARY KEY (repo_id, ref, hash)
) WITHOUT ROWID;

-- Manual author merges (applied on top of .mailmap at query time).
CREATE TABLE IF NOT EXISTS author_aliases (
    repo_id    INTEGER NOT NULL,
    author_key TEXT    NOT NULL,
    canonical  TEXT    NOT NULL,
    PRIMARY KEY (repo_id, author_key)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON author_aliases(repo_id, canonical);
"""

_init_lock = threading.Lock()
_initialized = False


def data_dir() -> str:
    return os.environ.get("RAT_DATA_DIR") or os.path.join(BASE_DIR, "data")


def repos_dir() -> str:
    return os.path.join(data_dir(), "repos")


def uploads_dir() -> str:
    return os.path.join(data_dir(), "uploads")


def db_path() -> str:
    return os.path.join(data_dir(), "rat.sqlite3")


def connect() -> sqlite3.Connection:
    """Open a new connection.  Safe for use from any thread."""
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


_REPOS_COLUMNS = (
    "id, name, source_type, source, path, status, status_detail, error, head, "
    "commit_total, commit_parsed, file_count, author_count, parser_version, "
    "created_at, updated_at"
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to SCHEMA_VERSION."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='repos'"
    ).fetchone()
    if row is not None and "'path'" not in row[0]:
        # v1 -> v2: widen the source_type CHECK constraint by rebuilding the
        # table (SQLite cannot alter constraints in place).
        conn.execute("ALTER TABLE repos RENAME TO repos_migrate")
        conn.executescript(SCHEMA)
        conn.execute(
            f"INSERT INTO repos ({_REPOS_COLUMNS}) SELECT {_REPOS_COLUMNS} FROM repos_migrate"
        )
        conn.execute("DROP TABLE repos_migrate")


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        os.makedirs(data_dir(), exist_ok=True)
        os.makedirs(repos_dir(), exist_ok=True)
        os.makedirs(uploads_dir(), exist_ok=True)
        conn = sqlite3.connect(db_path(), timeout=30)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def now() -> int:
    import time

    return int(time.time())
