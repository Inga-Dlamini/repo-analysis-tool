"""Unit tests for the streaming `git log --numstat -z` parser."""

from __future__ import annotations

from backend.gitparse import (
    PARSER_VERSION,
    LogParser,
    ParsedCommit,
    author_key,
    iter_log,
    rev_list,
    rev_list_count,
)

H1 = "1" * 40
H2 = "2" * 40
H3 = "3" * 40


def header(
    h: str,
    wall: int = 1_600_000_000,
    name: str = "Alice",
    email: str = "alice@example.com",
    subject: str = "subject",
    parents: str = "",
) -> bytes:
    return f"{h}\x1f{parents}\x1f{wall}\x1f{name}\x1f{email}\x1f{subject}".encode()


def stream(*tokens: bytes) -> bytes:
    return b"".join(t + b"\x00" for t in tokens)


def parse(data: bytes) -> list[ParsedCommit]:
    parser = LogParser()
    out = parser.feed(data)
    out += parser.close()
    return out


def test_parser_version_is_pinned():
    assert PARSER_VERSION == 1


def test_basic_commit_with_two_files():
    data = stream(
        header(H1, subject="first"),
        b"\n2\t1\tsrc/a.txt",
        b"5\t0\tREADME.md",
        header(H2, wall=1_600_000_100, name="Bob", email="bob@example.com", subject="second"),
    )
    commits = parse(data)
    assert [c.hash for c in commits] == [H1, H2]
    c1, c2 = commits
    assert (c1.ts, c1.author_name, c1.author_email, c1.subject) == (
        1_600_000_000, "Alice", "alice@example.com", "first",
    )
    assert [(f.path, f.added, f.removed) for f in c1.files] == [
        ("src/a.txt", 2, 1),
        ("README.md", 5, 0),
    ]
    assert c2.files == []


def test_commit_without_diff_entries_is_kept():
    commits = parse(stream(header(H1), header(H2, subject="second")))
    assert [c.hash for c in commits] == [H1, H2]
    assert all(c.files == [] for c in commits)


def test_parent_uses_first_parent():
    commits = parse(stream(header(H1, parents=f"{H2} {H3}")))
    assert commits[0].parent == H2


def test_empty_subject():
    commits = parse(stream(header(H1, subject="")))
    assert commits[0].subject == ""


def test_rename_with_changes_is_attributed_to_new_path():
    data = stream(
        header(H1),
        b"\n2\t1\t",              # rename header: empty path field
        b"old/name.txt",
        b"new/name.txt",
        header(H2),
    )
    commits = parse(data)
    assert len(commits) == 2  # the rename was consumed before H2 started
    (f,) = commits[0].files
    assert (f.old_path, f.path, f.added, f.removed) == ("old/name.txt", "new/name.txt", 2, 1)


def test_pure_rename_is_consumed_but_not_measured():
    data = stream(
        header(H1),
        b"\n0\t0\t",
        b"old.txt",
        b"new.txt",
        b"3\t0\tkept.txt",
        header(H2),
    )
    commits = parse(data)
    assert [(f.path, f.added, f.removed) for f in commits[0].files] == [("kept.txt", 3, 0)]
    assert [c.hash for c in commits] == [H1, H2]


def test_binary_entries_are_skipped():
    data = stream(
        header(H1),
        b"\n-\t-\tassets/logo.png",
        b"2\t0\tcode.py",
        header(H2),
    )
    commits = parse(data)
    assert [(f.path, f.added, f.removed) for f in commits[0].files] == [("code.py", 2, 0)]


def test_binary_rename_is_skipped_without_breaking_the_stream():
    data = stream(
        header(H1),
        b"\n-\t-\t",
        b"old.bin",
        b"new.bin",
        b"1\t1\ttext.txt",
        header(H2),
    )
    commits = parse(data)
    assert [(f.path, f.added, f.removed) for f in commits[0].files] == [("text.txt", 1, 1)]
    assert [c.hash for c in commits] == [H1, H2]


def test_zero_change_record_is_skipped():
    data = stream(header(H1), b"\n0\t0\tmode-only.sh", b"4\t2\treal.txt", header(H2))
    commits = parse(data)
    assert [(f.path, f.added, f.removed) for f in commits[0].files] == [("real.txt", 4, 2)]


def test_unknown_records_are_tolerated():
    data = stream(header(H1), b"\nnot-a-numstat-record", b"1\t0\tok.txt", header(H2))
    commits = parse(data)
    assert [(f.path, f.added, f.removed) for f in commits[0].files] == [("ok.txt", 1, 0)]


def test_chunked_feeding_matches_single_pass():
    data = stream(
        header(H1, subject="one"),
        b"\n2\t1\tsrc/a.txt",
        b"0\t4\tgone.txt",
        header(H2, subject="two"),
        b"\n1\t0\t",
        b"was/x.txt",
        b"now/x.txt",
        header(H3, subject="three"),
    )
    whole = parse(data)
    for size in (1, 3, 7, 64):
        parser = LogParser()
        out: list[ParsedCommit] = []
        for i in range(0, len(data), size):
            out += parser.feed(data[i : i + size])
        out += parser.close()
        assert [(c.hash, [(f.path, f.added, f.removed) for f in c.files]) for c in out] == \
               [(c.hash, [(f.path, f.added, f.removed) for f in c.files]) for c in whole]


def test_unicode_paths_and_subjects():
    data = stream(
        header(H1, subject="café ☕"),
        b"\n1\t1\t" + "docs/résumé.md".encode("utf-8"),
    )
    # paths/subjects arrive as raw UTF-8 bytes after the NUL-separated header
    commits = parse(data)
    assert commits[0].subject == "café ☕"
    assert commits[0].files[0].path == "docs/résumé.md"


def test_author_key_format():
    assert author_key("Jane Doe", "jane@example.com") == "Jane Doe <jane@example.com>"
    assert ParsedCommit(
        hash=H1, parent=None, ts=0, author_name="A", author_email="e@x", subject=""
    ).author_key == "A <e@x>"


# ---------------------------------------------------------------------------
# Integration against the synthetic repository (real `git log` output)


def test_iter_log_on_synthetic_repo(synth_repo):
    h = synth_repo["hashes"]
    commits = list(iter_log(str(synth_repo["path"]), revisions=["HEAD"]))
    assert len(commits) == 9
    by_hash = {c.hash: c for c in commits}
    assert h["merge"] not in by_hash  # merges excluded

    files = lambda hs: [(f.path, f.added, f.removed) for f in by_hash[h[hs]].files]
    assert sorted(files("c1")) == [("README.md", 2, 0), ("src/a.txt", 3, 0)]
    # c4: pure rename (0/0) + binary -> nothing measurable
    assert by_hash[h["c4"]].files == []
    # c6: deletion recorded as removed lines on its path
    assert files("c6") == [("src/b.txt", 0, 4)]
    # c5: changes after a rename are attributed to the new path
    assert files("c5") == [("src/deep/d.txt", 1, 0)]
    # c3 was authored with the legacy identity; mailmap resolves it
    assert by_hash[h["c3"]].author_key == "Alice <alice@example.com>"
    assert sorted(f.path for f in by_hash[h["c3"]].files) == ["README.md", "src/deep/c.txt"]


def test_rev_list_helpers(synth_repo):
    path = str(synth_repo["path"])
    assert rev_list_count(path, "--no-merges", "HEAD") == 9
    assert rev_list_count(path, "HEAD") == 10
    assert rev_list_count(path, "--no-merges", "v1") == 3
    merges = rev_list(path, "--merges", "HEAD")
    assert merges == [synth_repo["hashes"]["merge"]]
