"""Metric engine: implements the COMS3011A metric definitions.

Notation follows the brief.  ``H`` is the commit set selected by the filters
(reachable from the chosen reference ``ref``, restricted by committer-date
range, authors, and — optionally — a manually selected list of commits).
Per-commit file values come straight from ingestion:

    l+_h,f  added lines      l-_h,f  removed lines
    delta_h,f = l+ - l-      lambda_h,f = l+ + l-

Everything else is aggregated at query time using SQLite indexes:

* per path in H:            l+/l-/delta/lambda, modifications n, eta = n/|H|, rho = lambda/|H|
* per directory:            sums over the whole subtree (equivalent to the
  recursive immediate-child definition); modifications count distinct
  commits in which any file below the directory changed
* repository metrics:       directory metrics of the root (path "")
* per author (effective, after aliases):  modifications, churn, ownership

Performance notes: queries compose indexed lookups only (commit hash PK,
ref_commits PK, file_changes (repo_id, commit_hash) / (repo_id, path)).
The object pass streams (commit, path, added, removed) rows ordered by
commit hash and rolls up files, directories and per-commit directory
touch counts in a single Python pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

MAX_SELECTED_COMMITS = 50000
MAX_AUTHOR_KEYS = 4000
MAX_FILTER_AUTHORS = 64
BUCKET_SECONDS = {"day": 86400, "week": 604800, "month": 2592000}
DAY = 86400


class MetricsError(RuntimeError):
    status_code = 400


class BadRequest(MetricsError):
    status_code = 400


class NotFound(MetricsError):
    status_code = 404


class RefNotReady(MetricsError):
    status_code = 409


@dataclass
class Filters:
    ref: str = "HEAD"
    ts_from: int = 0
    ts_to: int = 1 << 62
    authors: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    path: str = ""
    bucket: str = "auto"

    @classmethod
    def from_payload(cls, payload: dict | None) -> "Filters":
        p = payload or {}

        def _int(value, default):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        ref = str(p.get("ref") or "HEAD").strip() or "HEAD"
        ts_from = max(0, _int(p.get("ts_from"), 0))
        ts_to = _int(p.get("ts_to"), 1 << 62)
        authors = [str(a) for a in (p.get("authors") or []) if str(a).strip()][:MAX_FILTER_AUTHORS]
        commits = [str(c).strip() for c in (p.get("commits") or []) if str(c).strip()]
        if len(commits) > MAX_SELECTED_COMMITS:
            raise BadRequest(f"too many commits selected (max {MAX_SELECTED_COMMITS})")
        path = str(p.get("path") or "").strip().strip("/")
        bucket = str(p.get("bucket") or "auto")
        if bucket not in ("auto", *BUCKET_SECONDS):
            raise BadRequest("bucket must be one of: auto, day, week, month")
        return cls(ref=ref, ts_from=ts_from, ts_to=ts_to, authors=authors,
                   commits=commits, path=path, bucket=bucket)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _prefix_clause(path: str, column: str) -> tuple[str, list]:
    """SQL fragment matching `column` == path or anything below path/."""
    if not path:
        return "", []
    return (
        f" AND ({column} = ? OR {column} LIKE ? ESCAPE '\\')",
        [path, _like_escape(path) + "/%"],
    )


class HScope:
    """Builds the WHERE predicates selecting commit set H."""

    def __init__(self, conn, repo_id: int, filters: Filters):
        self.conn = conn
        self.repo_id = repo_id
        self.f = filters
        self.author_keys: list[str] | None = None

    def prepare(self) -> None:
        if self.f.commits:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS rat_sel (hash TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM rat_sel")
            self.conn.executemany(
                "INSERT OR IGNORE INTO rat_sel (hash) VALUES (?)",
                [(h,) for h in self.f.commits],
            )
        if self.f.authors:
            by_canonical: dict[str, set[str]] = {}
            for row in self.conn.execute(
                "SELECT author_key, canonical FROM author_aliases WHERE repo_id=?",
                (self.repo_id,),
            ):
                by_canonical.setdefault(row["canonical"], set()).add(row["author_key"])
            keys: set[str] = set()
            for label in self.f.authors:
                keys.add(label)  # the canonical identity itself
                keys |= by_canonical.get(label, set())
            if len(keys) > MAX_AUTHOR_KEYS:
                raise BadRequest("author filter expands to too many identities")
            self.author_keys = sorted(keys)

    def where(self, c: str = "c") -> tuple[str, list]:
        """WHERE fragment + params over the `commits` table (aliased `c`)."""
        parts = [f"{c}.repo_id = ?"]
        params: list = [self.repo_id]
        if self.f.commits:
            parts.append(f"EXISTS (SELECT 1 FROM rat_sel s WHERE s.hash = {c}.hash)")
        parts.append(
            f"EXISTS (SELECT 1 FROM ref_commits rc WHERE rc.repo_id = {c}.repo_id"
            f" AND rc.ref = ? AND rc.hash = {c}.hash)"
        )
        params.append(self.f.ref)
        if self.author_keys is not None:
            marks = ",".join("?" * len(self.author_keys))
            parts.append(f"{c}.author_key IN ({marks})")
            params.extend(self.author_keys)
        parts.append(f"{c}.ts >= ?")
        params.append(self.f.ts_from)
        parts.append(f"{c}.ts < ?")
        params.append(self.f.ts_to)
        return " AND ".join(parts), params


def ref_is_cached(conn, repo_id: int, ref: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM refs WHERE repo_id=? AND ref=?", (repo_id, ref)
    ).fetchone()
    return row is not None


def _scope_or_raise(conn, repo_id: int, payload: dict) -> HScope:
    filters = Filters.from_payload(payload)
    if not ref_is_cached(conn, repo_id, filters.ref):
        raise RefNotReady(f"ref '{filters.ref}' is not prepared yet")
    scope = HScope(conn, repo_id, filters)
    scope.prepare()
    return scope


# ---------------------------------------------------------------------------
# Aggregation helpers


def _resolve_bucket(filters: Filters, ts_min: int | None, ts_max: int | None) -> int:
    if filters.bucket != "auto":
        return BUCKET_SECONDS[filters.bucket]
    if ts_min is None or ts_max is None:
        return DAY
    span = max(0, ts_max - ts_min)
    if span <= 62 * DAY:
        return DAY
    if span <= 730 * DAY:
        return BUCKET_SECONDS["week"]
    return BUCKET_SECONDS["month"]


def _metric_row(path: str, entry, h_count: int) -> dict:
    added, removed, modifications = entry
    churn = added + removed
    return {
        "path": path,
        "added": added,
        "removed": removed,
        "growth": added - removed,
        "churn": churn,
        "modifications": modifications,
        "modification_frequency": (modifications / h_count) if h_count else 0.0,
        "churn_rate": (churn / h_count) if h_count else 0.0,
    }


def _fetch_change_rows(conn, scope: HScope, path: str):
    """All (commit_hash, path, added, removed) rows of H within `path` scope,
    ordered by commit so per-commit rollups can be computed in one pass."""
    prefix_sql, prefix_params = _prefix_clause(path, "fc.path")
    where_sql, where_params = scope.where("c")
    sql = f"""
        SELECT fc.commit_hash, fc.path, fc.added, fc.removed
        FROM file_changes fc
        JOIN commits c ON c.repo_id = fc.repo_id AND c.hash = fc.commit_hash
        WHERE fc.repo_id = ? {prefix_sql} AND {where_sql}
        ORDER BY fc.commit_hash
    """
    return conn.execute(sql, [scope.repo_id, *prefix_params, *where_params])


def _rollup(rows) -> tuple[dict, dict]:
    """Roll file change rows up to per-file and per-directory aggregates
    ([added, removed, modifications]); directories include the whole subtree
    and '' is the repository root.  Modifications count distinct commits."""
    files: dict[str, list] = {}
    dirs: dict[str, list] = {}
    cur_commit = None
    touched: set[str] = set()
    for commit_hash, path, added, removed in rows:
        if commit_hash != cur_commit:
            for d in touched:
                dirs[d][2] += 1
            touched.clear()
            cur_commit = commit_hash
        agg = files.get(path)
        if agg is None:
            agg = files[path] = [0, 0, 0]
        agg[0] += added
        agg[1] += removed
        agg[2] += 1
        cut = path.rfind("/")
        while cut != -1:
            key = path[:cut]
            entry = dirs.get(key)
            if entry is None:
                entry = dirs[key] = [0, 0, 0]
            entry[0] += added
            entry[1] += removed
            touched.add(key)
            cut = path.rfind("/", 0, cut)
        entry = dirs.get("")
        if entry is None:
            entry = dirs[""] = [0, 0, 0]
        entry[0] += added
        entry[1] += removed
        touched.add("")
    for d in touched:
        dirs[d][2] += 1
    return files, dirs


def _by_churn(mapping: dict[str, list], limit: int, skip_root: bool = False):
    items = [(p, e) for p, e in mapping.items() if not (skip_root and p == "")]
    items.sort(key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0]))
    return items[:limit]


def _summary(conn, scope: HScope) -> dict:
    where_sql, where_params = scope.where("c")
    row = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(c.added), 0) AS added,
                   COALESCE(SUM(c.removed), 0) AS removed,
                   MIN(c.ts) AS ts_min, MAX(c.ts) AS ts_max
            FROM commits c WHERE {where_sql}""",
        where_params,
    ).fetchone()
    summary = {
        "commit_count": row["n"],
        "added": row["added"],
        "removed": row["removed"],
        "ts_min": row["ts_min"],
        "ts_max": row["ts_max"],
    }
    if row["n"] == 0:
        summary.update({"authors": 0, "files_touched": 0})
        return summary
    authors = conn.execute(
        f"""SELECT COUNT(DISTINCT COALESCE(al.canonical, c.author_key)) AS n
            FROM commits c
            LEFT JOIN author_aliases al ON al.repo_id = c.repo_id AND al.author_key = c.author_key
            WHERE {where_sql}""",
        where_params,
    ).fetchone()["n"]
    touched = conn.execute(
        f"""SELECT COUNT(DISTINCT fc.path) AS files
            FROM file_changes fc
            JOIN commits c ON c.repo_id = fc.repo_id AND c.hash = fc.commit_hash
            WHERE fc.repo_id = ? AND {where_sql}""",
        [scope.repo_id, *where_params],
    ).fetchone()
    summary["authors"] = authors
    summary["files_touched"] = touched["files"]
    return summary


def _timeline(conn, scope: HScope, ts_min: int | None, ts_max: int | None) -> dict:
    bucket = _resolve_bucket(scope.f, ts_min, ts_max)
    where_sql, where_params = scope.where("c")
    rows = conn.execute(
        f"""SELECT (c.ts / {bucket}) * {bucket} AS bucket, COUNT(*) AS commits,
                   COALESCE(SUM(c.added), 0) AS added, COALESCE(SUM(c.removed), 0) AS removed
            FROM commits c WHERE {where_sql}
            GROUP BY bucket ORDER BY bucket""",
        where_params,
    ).fetchall()
    return {
        "bucket_seconds": bucket,
        "points": [[r["bucket"], r["commits"], r["added"], r["removed"]] for r in rows],
    }


def _authors_repo(conn, scope: HScope, total_churn: int) -> list[dict]:
    where_sql, where_params = scope.where("c")
    rows = conn.execute(
        f"""SELECT COALESCE(al.canonical, c.author_key) AS name,
                   COUNT(*) AS commits,
                   COALESCE(SUM(c.added), 0) AS added,
                   COALESCE(SUM(c.removed), 0) AS removed,
                   MIN(c.ts) AS first_ts, MAX(c.ts) AS last_ts
            FROM commits c
            LEFT JOIN author_aliases al ON al.repo_id = c.repo_id AND al.author_key = c.author_key
            WHERE {where_sql}
            GROUP BY name""",
        where_params,
    ).fetchall()
    out = []
    for r in rows:
        churn = r["added"] + r["removed"]
        out.append({
            "name": r["name"],
            "commits": r["commits"],
            "added": r["added"],
            "removed": r["removed"],
            "growth": r["added"] - r["removed"],
            "churn": churn,
            "ownership": (churn / total_churn) if total_churn else 0.0,
            "first_ts": r["first_ts"],
            "last_ts": r["last_ts"],
        })
    out.sort(key=lambda a: (-a["churn"], a["name"]))
    return out


def _authors_object(conn, scope: HScope, path: str, total_churn: int) -> list[dict]:
    where_sql, where_params = scope.where("c")
    prefix_sql, prefix_params = _prefix_clause(path, "fc.path")
    rows = conn.execute(
        f"""SELECT COALESCE(al.canonical, c.author_key) AS name,
                   COUNT(DISTINCT fc.commit_hash) AS modifications,
                   COALESCE(SUM(fc.added), 0) AS added,
                   COALESCE(SUM(fc.removed), 0) AS removed
            FROM file_changes fc
            JOIN commits c ON c.repo_id = fc.repo_id AND c.hash = fc.commit_hash
            LEFT JOIN author_aliases al ON al.repo_id = c.repo_id AND al.author_key = c.author_key
            WHERE fc.repo_id = ? {prefix_sql} AND {where_sql}
            GROUP BY name""",
        [scope.repo_id, *prefix_params, *where_params],
    ).fetchall()
    out = []
    for r in rows:
        churn = r["added"] + r["removed"]
        out.append({
            "name": r["name"],
            "modifications": r["modifications"],
            "added": r["added"],
            "removed": r["removed"],
            "churn": churn,
            "ownership": (churn / total_churn) if total_churn else 0.0,
        })
    out.sort(key=lambda a: (-a["churn"], a["name"]))
    return out


def _history(conn, scope: HScope, path: str, limit: int = 30) -> list[dict]:
    where_sql, where_params = scope.where("c")
    prefix_sql, prefix_params = _prefix_clause(path, "fc.path")
    rows = conn.execute(
        f"""SELECT c.hash, c.ts, COALESCE(al.canonical, c.author_key) AS author,
                   c.subject, fc.added, fc.removed
            FROM file_changes fc
            JOIN commits c ON c.repo_id = fc.repo_id AND c.hash = fc.commit_hash
            LEFT JOIN author_aliases al ON al.repo_id = c.repo_id AND al.author_key = c.author_key
            WHERE fc.repo_id = ? {prefix_sql} AND {where_sql}
            ORDER BY c.ts DESC, c.hash DESC
            LIMIT ?""",
        [scope.repo_id, *prefix_params, *where_params, limit],
    ).fetchall()
    return [dict(r) for r in rows]


def _repo_metrics(conn, scope: HScope, h_count: int) -> dict:
    """Repository metrics = directory metrics on the root, which equals the
    commit-level totals (every change rolls up to the root)."""
    where_sql, where_params = scope.where("c")
    row = conn.execute(
        f"""SELECT COALESCE(SUM(c.added), 0) AS added,
                   COALESCE(SUM(c.removed), 0) AS removed,
                   COALESCE(SUM(CASE WHEN c.added + c.removed > 0 THEN 1 ELSE 0 END), 0) AS modified
            FROM commits c WHERE {where_sql}""",
        where_params,
    ).fetchone()
    return _metric_row("", [row["added"], row["removed"], row["modified"]], h_count)


def _build_treemap(dirs: dict, files: dict, max_nodes: int = 2500, max_depth: int = 4) -> dict | None:
    """Nested tree of directories (depth capped) with files as leaves;
    zero-churn branches are pruned and node count is budgeted."""
    if "" not in dirs:
        return None

    def churn(entry) -> int:
        return entry[0] + entry[1]

    def dir_node(path: str) -> dict:
        entry = dirs[path]
        return {
            "name": path.rsplit("/", 1)[-1] if path else "repo",
            "path": path,
            "added": entry[0],
            "removed": entry[1],
            "churn": churn(entry),
            "modifications": entry[2],
            "children": [],
        }

    child_dirs: dict[str, list[str]] = {}
    for path in dirs:
        if not path:
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        child_dirs.setdefault(parent, []).append(path)
    file_children: dict[str, list[str]] = {}
    for path in files:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        file_children.setdefault(parent, []).append(path)

    root = dir_node("")
    budget = max_nodes
    stack = [(root, 0)]
    while stack and budget > 0:
        node, depth = stack.pop()
        if depth >= max_depth:
            continue
        path = node["path"]
        kids: list[dict] = []
        for child in sorted(child_dirs.get(path, []), key=lambda p: -churn(dirs[p])):
            if churn(dirs[child]) > 0:
                kids.append(dir_node(child))
        for child in sorted(file_children.get(path, []), key=lambda p: -churn(files[p])):
            entry = files[child]
            if churn(entry) > 0:
                kids.append({
                    "name": child.rsplit("/", 1)[-1],
                    "path": child,
                    "added": entry[0],
                    "removed": entry[1],
                    "churn": churn(entry),
                    "modifications": entry[2],
                    "children": [],
                })
        kids = kids[:200]
        node["children"] = kids
        budget -= len(kids)
        for kid in kids:
            if kid["path"] in dirs:
                stack.append((kid, depth + 1))
    return root


# ---------------------------------------------------------------------------
# Public API


def dashboard(conn, repo_id: int, payload: dict) -> dict:
    scope = _scope_or_raise(conn, repo_id, payload)
    summary = _summary(conn, scope)
    h_count = summary["commit_count"]
    out = {
        "filters": {
            "ref": scope.f.ref,
            "ts_from": scope.f.ts_from,
            "ts_to": scope.f.ts_to,
            "authors": scope.f.authors,
            "commits_selected": len(scope.f.commits),
            "path": scope.f.path,
        },
        "summary": summary,
        "timeline": {"bucket_seconds": DAY, "points": []},
        "repo_metrics": _metric_row("", [0, 0, 0], h_count),
        "top_files": [],
        "top_dirs": [],
        "tree": None,
        "authors": [],
        "scope": None,
    }
    if h_count == 0:
        return out

    out["timeline"] = _timeline(conn, scope, summary["ts_min"], summary["ts_max"])
    out["repo_metrics"] = _repo_metrics(conn, scope, h_count)
    rows = _fetch_change_rows(conn, scope, scope.f.path)
    files, dirs = _rollup(rows)
    # repo-wide author ownership is relative to the full H churn
    total_churn = summary["added"] + summary["removed"]

    out["top_files"] = [_metric_row(p, e, h_count) for p, e in _by_churn(files, 50)]
    out["top_dirs"] = [
        _metric_row(p, e, h_count)
        for p, e in _by_churn(dirs, 40, skip_root=True)
        if p != scope.f.path  # the scope root itself is reported as scope.dir
    ]
    out["tree"] = _build_treemap(dirs, files)
    out["authors"] = _authors_repo(conn, scope, total_churn)

    if scope.f.path:
        entry = files.get(scope.f.path)
        dir_entry = dirs.get(scope.f.path)
        if dir_entry is not None:
            object_churn = dir_entry[0] + dir_entry[1]
        elif entry is not None:
            object_churn = entry[0] + entry[1]
        else:
            object_churn = total_churn
        out["scope"] = {
            "path": scope.f.path,
            "file": _metric_row(scope.f.path, entry, h_count) if entry else None,
            "dir": _metric_row(scope.f.path, dir_entry, h_count) if dir_entry else None,
            "authors": _authors_object(conn, scope, scope.f.path, object_churn),
            "history": _history(conn, scope, scope.f.path, limit=30),
        }
    return out


def object_detail(conn, repo_id: int, payload: dict) -> dict:
    scope = _scope_or_raise(conn, repo_id, payload)
    if not scope.f.path:
        raise BadRequest("path is required")
    summary = _summary(conn, scope)
    h_count = summary["commit_count"]
    rows = _fetch_change_rows(conn, scope, scope.f.path)
    files, dirs = _rollup(rows)
    dir_entry = dirs.get(scope.f.path)
    file_entry = files.get(scope.f.path)
    total_churn = (dir_entry[0] + dir_entry[1]) if dir_entry else (
        (file_entry[0] + file_entry[1]) if file_entry else 0
    )
    return {
        "path": scope.f.path,
        "h_count": h_count,
        "file": _metric_row(scope.f.path, file_entry, h_count) if file_entry else None,
        "dir": _metric_row(scope.f.path, dir_entry, h_count) if dir_entry else None,
        "authors": _authors_object(conn, scope, scope.f.path, total_churn),
        "history": _history(conn, scope, scope.f.path, limit=50),
    }


def tree_children(conn, repo_id: int, payload: dict) -> dict:
    scope = _scope_or_raise(conn, repo_id, payload)
    path = scope.f.path
    summary = _summary(conn, scope)
    h_count = summary["commit_count"]
    rows = _fetch_change_rows(conn, scope, path)

    child_dirs: dict[str, list] = {}
    child_files: dict[str, list] = {}
    cur_commit = None
    touched: set[str] = set()
    base = path + "/" if path else ""
    for commit_hash, full_path, added, removed in rows:
        if commit_hash != cur_commit:
            for d in touched:
                child_dirs[d][2] += 1
            touched.clear()
            cur_commit = commit_hash
        rel = full_path[len(base):]
        cut = rel.find("/")
        if cut == -1:
            entry = child_files.get(rel)
            if entry is None:
                entry = child_files[rel] = [0, 0, 0]
            entry[0] += added
            entry[1] += removed
            entry[2] += 1
        else:
            name = rel[:cut]
            entry = child_dirs.get(name)
            if entry is None:
                entry = child_dirs[name] = [0, 0, 0]
            entry[0] += added
            entry[1] += removed
            touched.add(name)
    for d in touched:
        child_dirs[d][2] += 1

    children = []
    for name, entry in child_dirs.items():
        row = _metric_row(base + name, entry, h_count)
        row["name"] = name
        row["type"] = "dir"
        children.append(row)
    for name, entry in child_files.items():
        row = _metric_row(base + name, entry, h_count)
        row["name"] = name
        row["type"] = "file"
        children.append(row)
    children.sort(key=lambda r: (r["type"] != "dir", -r["churn"], r["name"]))
    truncated = len(children) > 500
    return {"path": path, "children": children[:500], "truncated": truncated}


def list_commits(conn, repo_id: int, payload: dict) -> dict:
    filters = Filters.from_payload(payload)
    if not ref_is_cached(conn, repo_id, filters.ref):
        raise RefNotReady(f"ref '{filters.ref}' is not prepared yet")
    # the commit picker lists candidates: ignore any manual commit selection
    picker_filters = replace(filters, commits=[], path="")
    scope = HScope(conn, repo_id, picker_filters)
    scope.prepare()
    where_sql, where_params = scope.where("c")

    search = str(payload.get("q") or "").strip()
    extra = ""
    extra_params: list = []
    if search:
        like = f"%{_like_escape(search)}%"
        extra = " AND (c.subject LIKE ? ESCAPE '\\' OR c.hash LIKE ? ESCAPE '\\')"
        extra_params = [like, f"{_like_escape(search)}%"]

    page = max(1, int(payload.get("page") or 1))
    per_page = min(200, max(1, int(payload.get("per_page") or 50)))
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM commits c WHERE {where_sql}{extra}",
        [*where_params, *extra_params],
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT c.hash, c.ts, c.subject, c.added, c.removed, c.parent,
                   COALESCE(al.canonical, c.author_key) AS author,
                   (SELECT COUNT(*) FROM file_changes fc
                     WHERE fc.repo_id = c.repo_id AND fc.commit_hash = c.hash) AS files_changed
            FROM commits c
            LEFT JOIN author_aliases al ON al.repo_id = c.repo_id AND al.author_key = c.author_key
            WHERE {where_sql}{extra}
            ORDER BY c.ts DESC, c.hash DESC
            LIMIT ? OFFSET ?""",
        [*where_params, *extra_params, per_page, (page - 1) * per_page],
    ).fetchall()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "commits": [
            {
                **dict(r),
                "churn": r["added"] + r["removed"],
            }
            for r in rows
        ],
    }


def commit_detail(conn, repo_id: int, commit_hash: str) -> dict:
    row = conn.execute(
        """SELECT c.hash, c.ts, c.subject, c.parent, c.added, c.removed,
                  c.author_name, c.author_email, c.author_key,
                  COALESCE(al.canonical, c.author_key) AS author
           FROM commits c
           LEFT JOIN author_aliases al ON al.repo_id = c.repo_id AND al.author_key = c.author_key
           WHERE c.repo_id = ? AND c.hash = ?""",
        (repo_id, commit_hash),
    ).fetchone()
    if row is None:
        raise NotFound("commit not found")
    files = conn.execute(
        """SELECT path, added, removed FROM file_changes
           WHERE repo_id = ? AND commit_hash = ?
           ORDER BY (added + removed) DESC, path""",
        (repo_id, commit_hash),
    ).fetchall()
    return {
        **dict(row),
        "churn": row["added"] + row["removed"],
        "files": [dict(f) for f in files],
    }


def search_objects(conn, repo_id: int, query: str) -> dict:
    query = (query or "").strip()
    if len(query) < 2:
        return {"results": []}
    like = f"%{_like_escape(query)}%"
    rows = conn.execute(
        """SELECT DISTINCT path FROM file_changes
           WHERE repo_id = ? AND path LIKE ? ESCAPE '\\'
           ORDER BY path LIMIT 50""",
        (repo_id, like),
    ).fetchall()
    return {"results": [r["path"] for r in rows]}


def authors_overview(conn, repo_id: int) -> dict:
    rows = conn.execute(
        """SELECT c.author_key, COUNT(*) AS commits,
                  COALESCE(SUM(c.added), 0) AS added,
                  COALESCE(SUM(c.removed), 0) AS removed,
                  MIN(c.ts) AS first_ts, MAX(c.ts) AS last_ts
           FROM commits c WHERE c.repo_id = ?
           GROUP BY c.author_key ORDER BY c.author_key""",
        (repo_id,),
    ).fetchall()
    aliases = {
        r["author_key"]: r["canonical"]
        for r in conn.execute(
            "SELECT author_key, canonical FROM author_aliases WHERE repo_id=?", (repo_id,)
        )
    }
    identities = []
    for r in rows:
        key = r["author_key"]
        name, _, email_part = key.rpartition(" <")
        email = email_part.rstrip(">")
        identities.append({
            "key": key,
            "name": name,
            "email": email,
            "commits": r["commits"],
            "added": r["added"],
            "removed": r["removed"],
            "churn": r["added"] + r["removed"],
            "first_ts": r["first_ts"],
            "last_ts": r["last_ts"],
            "canonical": aliases.get(key),
        })
    groups: dict[str, list[str]] = {}
    for key, canonical in aliases.items():
        groups.setdefault(canonical, []).append(key)
    for g in groups.values():
        g.sort()

    # merge suggestions: same email (different names), same name (different emails)
    suggestions: list[dict] = []
    by_email: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for ident in identities:
        if ident["canonical"]:
            continue
        by_email.setdefault(ident["email"].lower(), []).append(ident["key"])
        by_name.setdefault(ident["name"].lower(), []).append(ident["key"])
    for email, keys in by_email.items():
        if len(keys) > 1:
            suggestions.append({"reason": f"same email <{email}>", "keys": sorted(keys)})
    for name, keys in by_name.items():
        if len(keys) > 1:
            emails = {k.rpartition(" <")[2].rstrip(">").lower() for k in keys}
            if len(emails) > 1:
                suggestions.append({"reason": f"same name '{name}'", "keys": sorted(keys)})
    return {"identities": identities, "groups": groups, "suggestions": suggestions}


def merge_authors(conn, repo_id: int, canonical: str, keys: list[str]) -> None:
    canonical = (canonical or "").strip()
    if not canonical:
        raise BadRequest("canonical name is required")
    if len(canonical) > 200:
        raise BadRequest("canonical name too long")
    keys = [str(k).strip() for k in (keys or []) if str(k).strip()]
    if not keys:
        raise BadRequest("select at least one author identity")
    known = {
        r["author_key"]
        for r in conn.execute("SELECT DISTINCT author_key FROM commits WHERE repo_id=?", (repo_id,))
    }
    for key in keys:
        if key not in known:
            raise BadRequest(f"unknown author identity: {key}")
    conn.executemany(
        """INSERT INTO author_aliases (repo_id, author_key, canonical) VALUES (?,?,?)
           ON CONFLICT(repo_id, author_key) DO UPDATE SET canonical=excluded.canonical""",
        [(repo_id, k, canonical) for k in keys],
    )
    conn.commit()


def unmerge_authors(conn, repo_id: int, canonical: str) -> None:
    conn.execute(
        "DELETE FROM author_aliases WHERE repo_id=? AND canonical=?", (repo_id, canonical)
    )
    conn.commit()


def compare_repos(conn, repo_ids: list[int]) -> list[dict]:
    out = []
    for repo_id in repo_ids:
        repo = conn.execute("SELECT * FROM repos WHERE id=?", (repo_id,)).fetchone()
        if repo is None:
            continue
        stats = conn.execute(
            """SELECT COUNT(*) AS commits,
                      COALESCE(SUM(c.added), 0) AS added,
                      COALESCE(SUM(c.removed), 0) AS removed,
                      MIN(c.ts) AS first_ts, MAX(c.ts) AS last_ts
               FROM commits c WHERE c.repo_id = ?""",
            (repo_id,),
        ).fetchone()
        authors = conn.execute(
            """SELECT COUNT(DISTINCT COALESCE(al.canonical, c.author_key)) AS n
               FROM commits c
               LEFT JOIN author_aliases al ON al.repo_id = c.repo_id AND al.author_key = c.author_key
               WHERE c.repo_id = ?""",
            (repo_id,),
        ).fetchone()["n"]
        out.append({
            "id": repo_id,
            "name": repo["name"],
            "status": repo["status"],
            "source_type": repo["source_type"],
            "head": repo["head"],
            "commits": stats["commits"],
            "authors": authors,
            "files": repo["file_count"],
            "added": stats["added"],
            "removed": stats["removed"],
            "growth": stats["added"] - stats["removed"],
            "churn": stats["added"] + stats["removed"],
            "first_ts": stats["first_ts"],
            "last_ts": stats["last_ts"],
        })
    return out
