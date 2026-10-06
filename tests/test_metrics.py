"""Metric-engine correctness tests.

Every expected number in this file is derived by hand from the fixture
history documented in `conftest.py` — nothing is compared against a
recomputation of the code under test.
"""

from __future__ import annotations

import pytest

from backend import ingest, metrics
from conftest import ALICE, BOB, ROBERT, ingest_repo, ts


def dash(conn, repo_id, **kw):
    payload = {"ref": "HEAD"}
    payload.update(kw)
    return metrics.dashboard(conn, repo_id, payload)


# ---------------------------------------------------------------------------
# Repository level (H = HEAD, 9 non-merge commits)


def test_summary_and_repository_metrics(conn, repo_id):
    d = dash(conn, repo_id)
    s = d["summary"]
    assert s["commit_count"] == 9            # merge excluded, empty commit included
    assert s["added"] == 26
    assert s["removed"] == 6
    assert s["authors"] == 3                 # mailmap collapses the legacy identity
    assert s["files_touched"] == 9           # inventoried paths incl. binary / pure renames
    assert s["ts_min"] == ts(1)
    assert s["ts_max"] == ts(10)

    m = d["repo_metrics"]
    assert m["added"] == 26
    assert m["removed"] == 6
    assert m["growth"] == 20                 # delta = l+ - l-
    assert m["churn"] == 32                  # lambda = l+ + l-
    assert m["modifications"] == 7           # c1,c2,c3,c5,c6,b7,c9 have churn > 0
    assert m["modification_frequency"] == pytest.approx(7 / 9)   # eta = n/|H|
    assert m["churn_rate"] == pytest.approx(32 / 9)              # rho = lambda/|H|


def test_timeline_buckets(conn, repo_id):
    t = dash(conn, repo_id)["timeline"]
    assert t["bucket_seconds"] == 86400      # ~10 day span -> daily buckets
    assert len(t["points"]) == 9             # one per non-merge commit (all distinct days)
    assert sum(p[1] for p in t["points"]) == 9
    assert sum(p[2] for p in t["points"]) == 26
    assert sum(p[3] for p in t["points"]) == 6


def test_top_files_order_and_values(conn, repo_id):
    rows = dash(conn, repo_id)["top_files"]
    assert [r["path"] for r in rows] == [
        "src/b.txt",             # +4/-4 churn 8
        "src/a.txt",             # +5/-1 churn 6
        "src/deep/c.txt",        # +5    churn 5 (created pre-rename)
        "README.md",             # +3/-1 churn 4
        "docs/100%_coverage.md", # +3    churn 3 (path ascending breaks the tie)
        "feature/notes.md",      # +3    churn 3
        "src2/z.txt",            # +2    churn 2
        "src/deep/d.txt",        # +1    churn 1 (post-rename modifications)
        "assets/logo.bin",       # 0     inventoried, never measured
    ]
    by_path = {r["path"]: r for r in rows}
    assert by_path["assets/logo.bin"]["modifications"] == 0
    assert (by_path["src/a.txt"]["added"], by_path["src/a.txt"]["removed"]) == (5, 1)
    assert by_path["src/a.txt"]["modifications"] == 2
    assert by_path["src/a.txt"]["modification_frequency"] == pytest.approx(2 / 9)
    assert by_path["src/b.txt"]["growth"] == 0          # +4 - 4
    assert by_path["README.md"]["churn"] == 4


def test_top_dirs_recursive_sums_and_root_excluded(conn, repo_id):
    rows = dash(conn, repo_id)["top_dirs"]
    assert [r["path"] for r in rows] == ["src", "src/deep", "docs", "feature", "src2", "assets"]
    assert "" not in {r["path"] for r in rows}          # repository metrics are separate

    assets = next(r for r in rows if r["path"] == "assets")
    assert (assets["churn"], assets["modifications"]) == (0, 0)

    src = next(r for r in rows if r["path"] == "src")
    assert (src["added"], src["removed"]) == (15, 5)    # a(5/1) + b(4/4) + c(5/0) + d(1/0)
    assert src["churn"] == 20
    assert src["modifications"] == 5                    # c1,c2,c3,c5,c6 touch src
    assert src["modification_frequency"] == pytest.approx(5 / 9)
    assert src["churn_rate"] == pytest.approx(20 / 9)

    deep = next(r for r in rows if r["path"] == "src/deep")
    assert (deep["added"], deep["removed"], deep["modifications"]) == (6, 0, 2)

    # directory sums equal the sum of their subtree files
    files = {r["path"]: r for r in dash(conn, repo_id)["top_files"]}
    under_src = [v for k, v in files.items() if k.startswith("src/")]
    assert sum(v["added"] for v in under_src) == src["added"]
    assert sum(v["removed"] for v in under_src) == src["removed"]

    # root directory metrics == repository metrics
    repo_metrics = dash(conn, repo_id)["repo_metrics"]
    assert sum(v["added"] for v in files.values()) == repo_metrics["added"]
    assert sum(v["removed"] for v in files.values()) == repo_metrics["removed"]


def test_rename_binary_and_deletion_storage(conn, repo_id, synth_repo):
    h = synth_repo["hashes"]

    def rows(commit):
        return {
            r["path"]: (r["added"], r["removed"])
            for r in conn.execute(
                "SELECT path, added, removed FROM file_changes WHERE repo_id=? AND commit_hash=?",
                (repo_id, commit),
            )
        }

    # c4: the pure rename and the binary file are inventoried with zero counts
    assert rows(h["c4"]) == {"src/deep/d.txt": (0, 0), "assets/logo.bin": (0, 0)}
    # the old path keeps its own pre-rename history but records nothing at the rename
    old = conn.execute(
        "SELECT COUNT(*) AS n FROM file_changes"
        " WHERE repo_id=? AND path='src/deep/c.txt' AND commit_hash=?",
        (repo_id, h["c4"]),
    ).fetchone()["n"]
    assert old == 0
    assert rows(h["c5"]) == {"src/deep/d.txt": (1, 0)}
    # the binary file has no line statistics at all
    binary = conn.execute(
        "SELECT added, removed FROM file_changes WHERE repo_id=? AND path LIKE 'assets/%'",
        (repo_id,),
    ).fetchall()
    assert [(r["added"], r["removed"]) for r in binary] == [(0, 0)]
    # deletion is recorded as removed lines on its path
    assert rows(h["c6"]) == {"src/b.txt": (0, 4)}


def test_merge_excluded_and_empty_commit_retained(conn, repo_id, synth_repo):
    h = synth_repo["hashes"]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM commits WHERE repo_id=?", (repo_id,)
    ).fetchone()["n"]
    assert total == 9
    merge = conn.execute(
        "SELECT 1 FROM commits WHERE repo_id=? AND hash=?", (repo_id, h["merge"])
    ).fetchone()
    assert merge is None
    empty = conn.execute(
        "SELECT added, removed FROM commits WHERE repo_id=? AND hash=?", (repo_id, h["c8"])
    ).fetchone()
    assert (empty["added"], empty["removed"]) == (0, 0)


# ---------------------------------------------------------------------------
# Filters


def test_time_range_is_half_open(conn, repo_id):
    # [ts(2), ts(4)) -> c2 and c3 only
    d = dash(conn, repo_id, ts_from=ts(2), ts_to=ts(4))
    assert d["summary"]["commit_count"] == 2
    assert d["summary"]["added"] == 12
    assert d["summary"]["removed"] == 2
    assert d["repo_metrics"]["churn"] == 14
    assert d["repo_metrics"]["modification_frequency"] == pytest.approx(1.0)

    # lower bound is inclusive: ts(4) starts at c4
    d = dash(conn, repo_id, ts_from=ts(4))
    assert d["summary"]["commit_count"] == 6          # c4,c5,c6,b7,c8,c9
    # upper bound is exclusive
    d = dash(conn, repo_id, ts_to=ts(2))
    assert d["summary"]["commit_count"] == 1          # c1
    assert d["summary"]["added"] == 5


def test_author_filter(conn, repo_id):
    d = dash(conn, repo_id, authors=[BOB])
    assert d["summary"]["commit_count"] == 3          # c2, c4 (rename+binary only), c6
    assert (d["summary"]["added"], d["summary"]["removed"]) == (6, 5)
    assert d["repo_metrics"]["growth"] == 1

    d = dash(conn, repo_id, authors=[ALICE])
    assert d["summary"]["commit_count"] == 5          # c1, c3, c5, c8, c9 (mailmapped)
    assert [a["name"] for a in d["authors"]] == [ALICE]

    # Robert's branch commit is addressed through its own identity
    d = dash(conn, repo_id, authors=[ROBERT])
    assert d["summary"]["commit_count"] == 1
    assert d["summary"]["added"] == 3


def test_manual_commit_selection(conn, repo_id, synth_repo):
    h = synth_repo["hashes"]
    d = dash(conn, repo_id, commits=[h["c1"], h["c5"]])
    assert d["summary"]["commit_count"] == 2
    assert d["summary"]["added"] == 6
    assert d["summary"]["removed"] == 0
    assert d["repo_metrics"]["churn"] == 6
    assert {r["path"] for r in d["top_files"]} == {"README.md", "src/a.txt", "src/deep/d.txt"}

    # manual selection AND author filter -> empty intersection
    d = dash(conn, repo_id, commits=[h["c1"], h["c5"]], authors=[BOB])
    assert d["summary"]["commit_count"] == 0


def test_empty_commit_set_is_safe(conn, repo_id, synth_repo):
    d = dash(conn, repo_id, commits=["f" * 40])
    assert d["summary"]["commit_count"] == 0
    assert d["repo_metrics"]["churn"] == 0
    assert d["repo_metrics"]["modification_frequency"] == 0.0
    assert d["repo_metrics"]["churn_rate"] == 0.0
    assert d["timeline"]["points"] == []
    assert d["tree"] is None
    assert d["authors"] == []


def test_author_rows_and_ownership(conn, repo_id):
    d = dash(conn, repo_id)
    authors = {a["name"]: a for a in d["authors"]}
    assert set(authors) == {ALICE, BOB, ROBERT}

    assert authors[ALICE]["commits"] == 4              # c8 is empty: no author credit
    assert (authors[ALICE]["added"], authors[ALICE]["removed"]) == (17, 1)
    assert authors[ALICE]["churn"] == 18
    assert authors[BOB]["commits"] == 2                # c4 is inventory-only
    assert authors[BOB]["churn"] == 11
    assert authors[ROBERT]["churn"] == 3

    # ownership omega = author churn / H churn
    assert authors[ALICE]["ownership"] == pytest.approx(18 / 32)
    assert authors[BOB]["ownership"] == pytest.approx(11 / 32)
    assert authors[ROBERT]["ownership"] == pytest.approx(3 / 32)
    assert sum(a["ownership"] for a in d["authors"]) == pytest.approx(1.0)
    # sorted by churn desc
    assert [a["name"] for a in d["authors"]] == [ALICE, BOB, ROBERT]


# ---------------------------------------------------------------------------
# Path scope


def test_path_filter_scopes_objects_but_not_repo_metrics(conn, repo_id, synth_repo):
    h = synth_repo["hashes"]
    d = dash(conn, repo_id, path="src")

    # everything listed is inside src/, src2/ is not matched by the prefix
    assert {r["path"] for r in d["top_files"]} == {
        "src/a.txt", "src/b.txt", "src/deep/c.txt", "src/deep/d.txt",
    }
    assert {r["path"] for r in d["top_dirs"]} == {"src/deep"}
    assert "src2/z.txt" not in {r["path"] for r in d["top_files"]}

    scope = d["scope"]
    assert scope["file"] is None                       # "src" is a directory
    assert scope["dir"]["path"] == "src"
    assert scope["dir"]["churn"] == 20
    assert scope["dir"]["modifications"] == 5
    assert scope["dir"]["modification_frequency"] == pytest.approx(5 / 9)

    # author metrics inside the scope
    scoped = {a["name"]: a for a in scope["authors"]}
    assert scoped[BOB]["modifications"] == 2           # c2 and c6 (not 3 change rows)
    assert (scoped[BOB]["added"], scoped[BOB]["removed"]) == (6, 5)
    assert scoped[BOB]["ownership"] == pytest.approx(11 / 20)
    assert scoped[ALICE]["modifications"] == 3         # c1, c3, c5
    assert (scoped[ALICE]["added"], scoped[ALICE]["removed"]) == (9, 0)
    assert scoped[ALICE]["ownership"] == pytest.approx(9 / 20)

    # history rows for the scope, newest first: c6, c5, c4 (rename row), c3,
    # c2 (x2 files), c1 — inventory-only rows still carry history
    hist = scope["history"]
    assert [r["hash"] for r in hist] == [
        h["c6"], h["c5"], h["c4"], h["c3"], h["c2"], h["c2"], h["c1"],
    ]

    # repository metrics stay H-wide by design (root directory == whole repo)
    assert d["repo_metrics"]["added"] == 26
    assert d["repo_metrics"]["churn"] == 32


def test_path_filter_file_scope(conn, repo_id):
    d = dash(conn, repo_id, path="README.md")
    scope = d["scope"]
    assert scope["file"]["path"] == "README.md"
    assert scope["file"]["churn"] == 4
    assert scope["file"]["modifications"] == 2
    assert scope["dir"] is None
    assert {a["name"] for a in scope["authors"]} == {ALICE}
    assert scope["authors"][0]["ownership"] == pytest.approx(1.0)


def test_tree_children(conn, repo_id):
    root = metrics.tree_children(conn, repo_id, {"ref": "HEAD"})
    by_name = {c["name"]: c for c in root["children"]}
    assert set(by_name) == {"src", "docs", "feature", "src2", "README.md", "assets"}
    assert by_name["src"]["type"] == "dir"
    assert by_name["src"]["churn"] == 20
    assert by_name["README.md"]["type"] == "file"
    assert (by_name["assets"]["churn"], by_name["assets"]["modifications"]) == (0, 0)

    src = metrics.tree_children(conn, repo_id, {"ref": "HEAD", "path": "src"})
    kids = {c["name"]: c for c in src["children"]}
    assert set(kids) == {"deep", "a.txt", "b.txt"}
    assert kids["deep"]["type"] == "dir"
    assert kids["deep"]["churn"] == 6
    assert kids["b.txt"]["churn"] == 8


def test_object_detail(conn, repo_id, synth_repo):
    h = synth_repo["hashes"]
    d = metrics.object_detail(conn, repo_id, {"ref": "HEAD", "path": "README.md"})
    assert d["file"]["added"] == 3
    assert d["file"]["modifications"] == 2
    assert d["dir"] is None
    assert [r["hash"] for r in d["history"]] == [h["c3"], h["c1"]]   # newest first

    d = metrics.object_detail(conn, repo_id, {"ref": "HEAD", "path": "src/deep"})
    assert d["dir"]["churn"] == 6
    assert d["file"] is None
    assert [r["hash"] for r in d["history"]] == [h["c5"], h["c4"], h["c3"]]

    with pytest.raises(metrics.BadRequest):
        metrics.object_detail(conn, repo_id, {"ref": "HEAD"})


def test_search_objects(conn, repo_id):
    res = metrics.search_objects(conn, repo_id, "deep")
    assert res["results"] == ["src/deep/c.txt", "src/deep/d.txt"]
    # the rename itself is not recorded: c.txt has no entry after c3
    assert metrics.search_objects(conn, repo_id, "c.txt")["results"] == ["src/deep/c.txt"]
    # LIKE metacharacters in the query are treated literally
    assert metrics.search_objects(conn, repo_id, "100%_c")["results"] == ["docs/100%_coverage.md"]
    assert metrics.search_objects(conn, repo_id, "100x_c")["results"] == []
    assert metrics.search_objects(conn, repo_id, "a")["results"] == []   # < 2 chars


def test_commit_detail(conn, repo_id, synth_repo):
    h = synth_repo["hashes"]
    d = metrics.commit_detail(conn, repo_id, h["c2"])
    assert (d["added"], d["removed"], d["churn"]) == (6, 1, 7)
    assert d["author"] == BOB
    assert [(f["path"], f["added"], f["removed"]) for f in d["files"]] == [
        ("src/b.txt", 4, 0),
        ("src/a.txt", 2, 1),
    ]
    with pytest.raises(metrics.NotFound):
        metrics.commit_detail(conn, repo_id, "0" * 40)


# ---------------------------------------------------------------------------
# Author merging


def test_authors_overview_merge_and_unmerge(conn, repo_id):
    overview = metrics.authors_overview(conn, repo_id)
    assert {i["key"] for i in overview["identities"]} == {ALICE, BOB, ROBERT}
    assert overview["groups"] == {}
    assert overview["suggestions"] == []
    # the mailmapped legacy identity never surfaces as a separate person
    assert not any("asmith" in i["email"] for i in overview["identities"])

    metrics.merge_authors(conn, repo_id, BOB, [ROBERT])
    try:
        overview = metrics.authors_overview(conn, repo_id)
        assert overview["groups"] == {BOB: [ROBERT]}
        assert next(i for i in overview["identities"] if i["key"] == ROBERT)["canonical"] == BOB

        d = dash(conn, repo_id)
        authors = {a["name"]: a for a in d["authors"]}
        assert set(authors) == {ALICE, BOB}
        assert authors[BOB]["commits"] == 3                # c2, c6, b7 (c4 inventoried only)
        assert authors[BOB]["churn"] == 14
        assert authors[BOB]["ownership"] == pytest.approx(14 / 32)
        assert d["summary"]["authors"] == 2

        # filtering by the canonical name selects every merged identity
        d = dash(conn, repo_id, authors=[BOB])
        assert d["summary"]["commit_count"] == 4
        assert (d["summary"]["added"], d["summary"]["removed"]) == (9, 5)
    finally:
        metrics.unmerge_authors(conn, repo_id, BOB)

    d = dash(conn, repo_id)
    assert {a["name"] for a in d["authors"]} == {ALICE, BOB, ROBERT}
    assert dash(conn, repo_id, authors=[BOB])["summary"]["commit_count"] == 3


def test_merge_validation(conn, repo_id):
    with pytest.raises(metrics.BadRequest):
        metrics.merge_authors(conn, repo_id, "", [BOB])
    with pytest.raises(metrics.BadRequest):
        metrics.merge_authors(conn, repo_id, BOB, [])
    with pytest.raises(metrics.BadRequest):
        metrics.merge_authors(conn, repo_id, BOB, ["Nobody <none@example.com>"])


# ---------------------------------------------------------------------------
# References


def test_ref_requires_ensure_then_reports_tag_scope(conn, repo_id):
    with pytest.raises(metrics.RefNotReady):
        dash(conn, repo_id, ref="v1")
    assert not metrics.ref_is_cached(conn, repo_id, "v1")

    ingest.ensure_ref(repo_id, "v1")
    assert metrics.ref_is_cached(conn, repo_id, "v1")

    d = dash(conn, repo_id, ref="v1")
    assert d["summary"]["commit_count"] == 3              # c1..c3 reachable from v1
    assert (d["summary"]["added"], d["summary"]["removed"]) == (17, 2)
    assert d["repo_metrics"]["modification_frequency"] == pytest.approx(1.0)
    assert d["repo_metrics"]["churn_rate"] == pytest.approx(19 / 3)
    assert [r["path"] for r in d["top_files"]] == [
        "src/a.txt", "src/deep/c.txt", "README.md", "src/b.txt",
    ]


# ---------------------------------------------------------------------------
# Payload validation / helpers


def test_filter_payload_validation():
    f = metrics.Filters.from_payload({"ts_from": -10, "ref": " ", "path": "/src/"})
    assert f.ts_from == 0
    assert f.ref == "HEAD"
    assert f.path == "src"

    with pytest.raises(metrics.BadRequest):
        metrics.Filters.from_payload({"bucket": "hour"})
    with pytest.raises(metrics.BadRequest):
        metrics.Filters.from_payload({"commits": ["x"] * (metrics.MAX_SELECTED_COMMITS + 1)})


def test_like_escape():
    assert metrics._like_escape("a%b_c\\d") == "a\\%b\\_c\\\\d"
    sql, params = metrics._prefix_clause("src", "fc.path")
    assert params == ["src", "src/%"]
    assert metrics._prefix_clause("", "fc.path") == ("", [])


# ---------------------------------------------------------------------------
# Cross-repo


def test_compare_repos(conn, synth_repo, repo_id):
    other = ingest_repo(synth_repo["path"], name="synthetic-copy")
    try:
        rows = metrics.compare_repos(conn, [repo_id, other])
        assert [r["id"] for r in rows] == [repo_id, other]
        for r in rows:
            assert r["commits"] == 9
            assert r["churn"] == 32
            assert r["growth"] == 20
        assert rows[0]["name"] == "synthetic"
        assert metrics.compare_repos(conn, [10**9]) == []   # unknown ids are skipped
    finally:
        ingest.delete_repo(other)
