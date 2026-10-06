#!/usr/bin/env python3
"""RAT performance benchmark.

Builds a synthetic repository of N commits (default 100 000, git.git scale)
entirely through ``git fast-import``, ingests it through the real backend
pipeline, and times the dashboard queries the UI issues.

    python scripts/benchmark.py                      # 100k synthetic commits
    python scripts/benchmark.py --commits 200000
    python scripts/benchmark.py --url https://github.com/DaveGamble/cJSON.git
    python scripts/benchmark.py --path /srv/mirrors/git.git
    python scripts/benchmark.py --keep               # keep repo + data dir

The benchmark never touches the default data directory: everything (SQLite
database, repo checkout, synthetic source) lives under a temporary directory
unless --data-dir is given.  Use --keep to print its location so a server can
later point RAT_DATA_DIR at it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BLOB_MARKS = {          # blob content, declared once at the top of the stream
    **{m: "\n".join(f"blob {m} line {j}" for j in range(m)) for m in range(1, 17)},
    18: "\n".join(f"big line {j}" for j in range(500)),
    19: "\n".join(f"big line {j}" for j in range(600)),
}
BINARY_MARKS = {17: b"\x00\x01\x02\x03" * 1024, 20: b"\xff\x00" * 4096}
MAIN_MARK_BASE = 1_000_000    # marks share one namespace with the blobs above
SIDE_MARK_BASE = 500_000_000
SIDE_INTERVAL = 2000          # every N-th main commit merges a side branch
AUTHORS = [(f"Dev {k}", f"dev{k}@example.com") for k in range(8)]
BASE_TS = 1_600_000_000
STEP_TS = 3600


# ---------------------------------------------------------------------------
# Synthetic repository


def _blob_declarations() -> bytes:
    out = []
    for mark, text in BLOB_MARKS.items():
        data = text.encode("utf-8")
        out.append(f"blob\nmark :{mark}\ndata {len(data)}\n".encode() + data + b"\n")
    for mark, data in BINARY_MARKS.items():
        out.append(f"blob\nmark :{mark}\ndata {len(data)}\n".encode() + data + b"\n")
    return b"".join(out)


def _commit_block(index: int, is_merge: bool) -> bytes:
    """One main-chain commit touching two source files (+ extras)."""
    name, email = AUTHORS[index % len(AUTHORS)]
    ts = BASE_TS + index * STEP_TS
    msg = f"commit {index}\n"
    body = [
        f"commit refs/heads/main\nmark :{MAIN_MARK_BASE + index}\n",
        f"author {name} <{email}> {ts} +0000\n",
        f"committer {name} <{email}> {ts} +0000\n",
        f"data {len(msg.encode())}\n{msg}",
    ]
    if is_merge:
        body.append(f"merge :{SIDE_MARK_BASE + index}\n")
    body.append(f"M 100644 :{(index % 16) + 1} src/f{index % 8}.txt\n")
    body.append(f"M 100644 :{((index // 8) % 16) + 1} mod/f{index % 64}.txt\n")
    if index % 100 == 0:                       # large diffs
        body.append(f"M 100644 :{18 + ((index // 100) % 2)} hot/big.txt\n")
    if index % 1000 == 0:                      # binary files (must be skipped)
        body.append(f"M 100644 :{17 if (index // 1000) % 2 else 20} bin/data.bin\n")
    if index % 500 == 0 and index > 0:         # rename-looking delete + add
        body.append(f"D src/f{(index - 1) % 8}.txt\n")
        body.append(
            f"M 100644 :{(index % 16) + 1} renamed/f{(index - 1) % 8}.{index // 500}.txt\n"
        )
    return "".join(body).encode()


def _side_commit(index: int) -> bytes:
    """A one-file commit on branch side<i>, branched off commit index - 1."""
    name, email = AUTHORS[(index + 3) % len(AUTHORS)]
    ts = BASE_TS + index * STEP_TS - 60
    msg = f"side {index}\n"
    return (
        f"commit refs/heads/side{index}\nmark :{SIDE_MARK_BASE + index}\n"
        f"author {name} <{email}> {ts} +0000\n"
        f"committer {name} <{email}> {ts} +0000\n"
        f"data {len(msg.encode())}\n{msg}"
        f"from :{MAIN_MARK_BASE + index - 1}\n"
        f"M 100644 :{(index % 16) + 1} side/note{index}.txt\n"
    ).encode()


def build_synthetic_repo(dest: str, commits: int) -> float:
    """Write `commits` main-chain commits via fast-import.  Returns seconds."""
    os.makedirs(dest, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "--quiet"],
        cwd=dest, check=True, capture_output=True,
    )
    env = os.environ.copy()
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        ["git", "fast-import", "--quiet", "--done"],
        cwd=dest, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, env=env,
    )
    buf: list[bytes] = [_blob_declarations()]
    size = 0
    try:
        for i in range(1, commits + 1):
            if i % SIDE_INTERVAL == 0:      # side commit first: the merge refers to it
                buf.append(_side_commit(i))
                size += len(buf[-1])
            buf.append(_commit_block(i, is_merge=(i % SIDE_INTERVAL == 0)))
            size += len(buf[-1])
            if size > 4 * 1024 * 1024:
                proc.stdin.write(b"".join(buf))
                buf, size = [], 0
        buf.append(b"done\n")
        proc.stdin.write(b"".join(buf))
        proc.stdin.close()
    except BrokenPipeError:
        err = proc.stderr.read().decode("utf-8", "replace")
        raise SystemExit(f"git fast-import failed:\n{err}")
    err = proc.stderr.read().decode("utf-8", "replace")
    if proc.wait() != 0:
        raise SystemExit(f"git fast-import failed:\n{err}")
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=dest, check=True, capture_output=True, env=env,
    )
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Timing helpers


def timed(label: str, fn, repeat: int = 1) -> tuple[str, float, object]:
    best = float("inf")
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - t0)
    return label, best, result


def peak_rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def dir_size_mb(path: str) -> float:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total / (1024 * 1024)


# ---------------------------------------------------------------------------
# Benchmark core


def run_benchmark(args) -> int:
    os.environ["RAT_DATA_DIR"] = args.data_dir
    from backend import db, ingest, metrics          # noqa: E402  (env first)

    db.init_db()
    commits = args.commits

    print(f"RAT benchmark — data dir: {args.data_dir}")
    conn = db.connect()
    if args.url:
        repo_id = ingest.create_url_repo(args.url)
        _, attach_s, _ = timed("clone + ingest", lambda: ingest._ingest_url(repo_id, args.url))
        print(f"  clone + ingest: {attach_s:,.1f}s")
    elif args.path:
        src = str(Path(args.path).resolve())
        repo_id = ingest._insert_repo("path", src, Path(src).name or "local")
        dest = ingest.get_repo(conn, repo_id)["path"]
        subprocess.run(["git", "clone", "--quiet", "--local", "--", src, "."],
                       cwd=dest, check=True, capture_output=True)
        _, ingest_s, _ = timed("ingest", lambda: ingest._finish_ingest(repo_id))
        print(f"  ingest (parse + HEAD ref): {ingest_s:,.1f}s")
    else:
        repo_id = ingest._insert_repo(
            "path", os.path.join(args.data_dir, "synthetic-src"), f"synthetic-{commits}"
        )
        repo_dir = ingest.get_repo(conn, repo_id)["path"]
        print(f"building synthetic repo ({commits:,} main commits, "
              f"merge every {SIDE_INTERVAL:,}) …", flush=True)
        build_s = build_synthetic_repo(repo_dir, commits)
        print(f"  fast-import: {build_s:,.1f}s "
              f"({commits / build_s:,.0f} commits/s)")
        _, ingest_s, _ = timed("ingest", lambda: ingest._finish_ingest(repo_id))
        print(f"  ingest (parse + HEAD ref): {ingest_s:,.1f}s "
              f"({commits / max(ingest_s, 1e-9):,.0f} commits/s)")

    repo = ingest.get_repo(conn, repo_id)
    if repo is None or repo["status"] != "ready":
        raise SystemExit(f"ingestion did not reach ready: {repo and dict(repo)}")

    git_count = int(subprocess.run(
        ["git", "-C", repo["path"], "rev-list", "--count", "--no-merges", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())

    rows = {t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            for t in ("commits", "file_changes", "ref_commits")}
    print(f"  parsed: {repo['commit_parsed']:,} commits "
          f"(git says {git_count:,} non-merge) | "
          f"{rows['file_changes']:,} file changes | "
          f"{repo['author_count']} authors | parser v{repo['parser_version']}")
    if repo["commit_parsed"] != git_count:
        print("  !! parsed count does not match git rev-list")

    sample = [r["hash"] for r in conn.execute(
        "SELECT hash FROM commits WHERE repo_id=? ORDER BY RANDOM() LIMIT 1000",
        (repo_id,),
    )]
    ts_rows = conn.execute(
        "SELECT MIN(ts) a, MAX(ts) b FROM commits WHERE repo_id=?", (repo_id,)
    ).fetchone()
    span = ts_rows["b"] - ts_rows["a"]
    deep_path = conn.execute(
        "SELECT path FROM file_changes WHERE repo_id=? GROUP BY path "
        "ORDER BY COUNT(*) DESC LIMIT 1", (repo_id,)
    ).fetchone()["path"]
    # the tip itself may be a merge commit (never ingested) — use the newest
    # parsed commit for the detail query
    head = conn.execute(
        "SELECT hash FROM commits WHERE repo_id=? ORDER BY seq DESC LIMIT 1", (repo_id,)
    ).fetchone()["hash"]
    total_commits = repo["commit_parsed"]

    print("\nquery timings (best of 2):")
    checks = [
        timed("dashboard, full rollup + treemap",
              lambda: metrics.dashboard(conn, repo_id, {"ref": "HEAD"}), 2),
        timed("dashboard, week buckets",
              lambda: metrics.dashboard(conn, repo_id, {"ref": "HEAD", "bucket": "week"}), 2),
        timed("dashboard, author filter",
              lambda: metrics.dashboard(conn, repo_id, {"ref": "HEAD", "authors": [f"{AUTHORS[0][0]} <{AUTHORS[0][1]}>"]}), 2),
        timed("dashboard, time range (last 20%)",
              lambda: metrics.dashboard(conn, repo_id,
                                        {"ref": "HEAD", "ts_from": ts_rows["b"] - span // 5}), 2),
        timed("dashboard, manual selection (1000 commits)",
              lambda: metrics.dashboard(conn, repo_id, {"ref": "HEAD", "commits": sample}), 2),
        timed(f"dashboard, path scope ({deep_path[:40]})",
              lambda: metrics.dashboard(conn, repo_id, {"ref": "HEAD", "path": deep_path}), 2),
        timed("tree children (root)",
              lambda: metrics.tree_children(conn, repo_id, {"ref": "HEAD"})),
        timed("object detail",
              lambda: metrics.object_detail(conn, repo_id, {"ref": "HEAD", "path": deep_path})),
        timed("search objects",
              lambda: metrics.search_objects(conn, repo_id, "src/f")),
        timed("commit page (first)",
              lambda: metrics.list_commits(conn, repo_id, {"per_page": 50, "page": 1})),
        timed("commit page (middle)",
              lambda: metrics.list_commits(conn, repo_id,
                                           {"per_page": 50, "page": max(1, total_commits // 100)})),
        timed("commit detail",
              lambda: metrics.commit_detail(conn, repo_id, head)),
        timed("authors overview",
              lambda: metrics.authors_overview(conn, repo_id)),
        timed("compare repos",
              lambda: metrics.compare_repos(conn, [repo_id])),
    ]
    worst = 0.0
    for label, seconds, _ in checks:
        worst = max(worst, seconds)
        print(f"  {label:<42} {seconds * 1000:9.1f} ms")
    print(f"  {'(worst)':<42} {worst * 1000:9.1f} ms")

    sizes = (f"db {db_size_mb():,.1f} MB", f"checkout {dir_size_mb(repo['path']):,.1f} MB")
    rss = peak_rss_mb()
    if rss is not None:
        sizes += (f"peak RSS {rss:,.0f} MB",)
    print("\n" + " | ".join(sizes))

    if args.keep:
        print(f"\nkept: repo id {repo_id} in {args.data_dir} "
              f"(serve with RAT_DATA_DIR={args.data_dir})")
    else:
        conn.close()
        ingest.delete_repo(repo_id)
        shutil.rmtree(args.data_dir, ignore_errors=True)
    return 0


def db_size_mb() -> float:
    from backend import db

    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(db.db_path() + suffix)
        except OSError:
            pass
    return total / (1024 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser(description="RAT ingestion/query benchmark")
    parser.add_argument("--commits", type=int, default=100_000,
                        help="synthetic commit count (default 100000)")
    parser.add_argument("--url", help="clone + ingest a remote repository instead")
    parser.add_argument("--path", help="clone (locally) + ingest a repository on disk")
    parser.add_argument("--data-dir", help="data directory (default: fresh temp dir)")
    parser.add_argument("--keep", action="store_true",
                        help="keep database + checkout after the run")
    args = parser.parse_args()

    if args.url and args.path:
        parser.error("--url and --path are mutually exclusive")
    if not args.data_dir:
        args.data_dir = tempfile.mkdtemp(prefix="rat-bench-")
    os.makedirs(args.data_dir, exist_ok=True)

    try:
        return run_benchmark(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
