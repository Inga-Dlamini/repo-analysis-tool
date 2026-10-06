"""Git interface: raw `git log` extraction and streaming parsing.

Ingestion is a single streaming pass over::

    git log --no-merges --use-mailmap --numstat -z -M50% \
        --format=%H%x1f%P%x1f%ct%x1f%aN%x1f%aE%x1f%s <revisions>

That command gives, for every non-merge commit reachable from the given
revisions: the commit hash, its parent, the committer date, the mailmap aware
author identity, the subject, and the per-file line statistics with rename
detection at the 50% threshold — exactly the data the metric definitions in
the brief are stated over.

Wire format notes (verified against git 2.43, stable since ~2.9):

* With ``-z`` every record is NUL terminated.  The commit header (our
  ``--format`` string, fields separated by ``0x1f``) ends with NUL; when the
  commit has diff entries it is followed by a literal ``\\n`` (the usual
  blank line between the commit header and the diffstat section).
* A normal numstat record is ``<added>\\t<deleted>\\t<path>\\0``.  Binary
  files use ``-`` for both counters (not measured per the brief).
* A rename record is ``<added>\\t<deleted>\\t\\0<old-path>\\0<new-path>\\0``
  — the path field is empty and the following two NUL separated tokens are
  the old and new path.  Changes are attributed to the new path.
* Commits with no measurable diff (empty commits, merges) simply carry no
  numstat records.
* ``%aN``/``%aE`` always resolve identities through ``.mailmap``; passing
  ``--use-mailmap`` as well is harmless and keeps the intent explicit.
* ``-c diff.renameLimit=32767`` raises the rename-candidate search cap so
  huge commits are still rename-detected like small ones (git's default cap
  of 1000 would silently skip detection on drop commits).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

# Bump when parsing logic changes in a way that invalidates stored data.
PARSER_VERSION = 1

FIELD_SEP = b"\x1f"
FORMAT = "%H%x1f%P%x1f%ct%x1f%aN%x1f%aE%x1f%s"

_HASH_RE = re.compile(rb"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_NUMSTAT_RE = re.compile(rb"^(\d+|-)\t(\d+|-)\t([\s\S]*)$")

_MAX_STDERR = 8000


class GitError(RuntimeError):
    pass


def author_key(name: str, email: str) -> str:
    """Canonical identity string for a single author."""
    return f"{name} <{email}>"


@dataclass
class ParsedFile:
    path: str
    added: int
    removed: int
    old_path: str | None = None  # set for renames

    @property
    def touched(self) -> bool:
        return (self.added + self.removed) > 0


@dataclass
class ParsedCommit:
    hash: str
    parent: str | None
    ts: int
    author_name: str
    author_email: str
    subject: str
    files: list[ParsedFile] = field(default_factory=list)

    @property
    def author_key(self) -> str:
        return author_key(self.author_name, self.author_email)


def _dec(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


class LogParser:
    """Incremental parser for the byte stream described in the module docstring.

    Feed it arbitrary chunks with :meth:`feed` and flush with :meth:`close`;
    both return the list of commits completed by that call.
    """

    def __init__(self) -> None:
        self._buf = b""
        self._commit: ParsedCommit | None = None
        self._after_header = False
        self._pending_rename: bool = False
        self._rename_paths: list[str] = []
        self._rename_file: ParsedFile | None = None
        self._rename_counts: tuple[int, int] = (0, 0)

    def feed(self, data: bytes) -> list[ParsedCommit]:
        out: list[ParsedCommit] = []
        if self._buf:
            data = self._buf + data
            self._buf = b""
        parts = data.split(b"\x00")
        self._buf = parts.pop()
        for tok in parts:
            self._token(tok, out)
        return out

    def close(self) -> list[ParsedCommit]:
        out: list[ParsedCommit] = []
        if self._buf:
            tok, self._buf = self._buf, b""
            self._token(tok, out)
        self._flush(out)
        return out

    # -- internals ---------------------------------------------------------

    def _token(self, tok: bytes, out: list[ParsedCommit]) -> None:
        if self._after_header:
            self._after_header = False
            if tok.startswith(b"\n"):
                tok = tok[1:]
        if not tok:
            return
        if self._pending_rename:
            self._rename_paths.append(_dec(tok))
            if len(self._rename_paths) == 2:
                if self._rename_file is not None and self._rename_file.touched:
                    self._rename_file.old_path, self._rename_file.path = self._rename_paths
                    assert self._commit is not None
                    self._commit.files.append(self._rename_file)
                self._pending_rename = False
                self._rename_paths = []
                self._rename_file = None
            return
        first_field = tok.split(FIELD_SEP, 1)[0]
        if FIELD_SEP in tok and _HASH_RE.match(first_field):
            self._flush(out)
            self._start(tok)
            return
        m = _NUMSTAT_RE.match(tok)
        if not m:
            return  # tolerate unknown records
        a_raw, r_raw, path = m.group(1), m.group(2), m.group(3)
        binary = a_raw == b"-" or r_raw == b"-"
        if path == b"":
            # Rename header: the next two tokens are old and new path.
            self._pending_rename = True
            self._rename_paths = []
            if binary:
                self._rename_file = None
            else:
                self._rename_file = ParsedFile("", int(a_raw), int(r_raw))
            return
        if binary:
            return
        added, removed = int(a_raw), int(r_raw)
        if added + removed == 0:
            return  # e.g. mode-only changes: no line impact
        assert self._commit is not None
        self._commit.files.append(ParsedFile(_dec(path), added, removed))

    def _start(self, tok: bytes) -> None:
        fields = tok.split(FIELD_SEP)
        if len(fields) < 5:
            return
        parents = _dec(fields[1]).split()
        subject = _dec(FIELD_SEP.join(fields[5:])) if len(fields) > 5 else ""
        self._commit = ParsedCommit(
            hash=_dec(fields[0]),
            parent=parents[0] if parents else None,
            ts=int(fields[2]),
            author_name=_dec(fields[3]).strip(),
            author_email=_dec(fields[4]).strip(),
            subject=subject,
        )
        self._after_header = True

    def _flush(self, out: list[ParsedCommit]) -> None:
        if self._commit is not None:
            out.append(self._commit)
            self._commit = None


def _base_cmd(git_dir: str) -> list[str]:
    return [
        "git",
        "-C",
        str(git_dir),
        "-c",
        "diff.renameLimit=32767",
        "-c",
        "core.quotePath=false",
    ]


def _env() -> dict:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env["LC_ALL"] = "C"
    return env


def run_git(git_dir: str, *args: str, timeout: int | None = 3600) -> str:
    """Run a git command and return stdout (text).  Raises GitError on failure."""
    cmd = _base_cmd(git_dir) + list(args)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git command timed out: {' '.join(args)}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[:_MAX_STDERR].strip()
        raise GitError(f"git {' '.join(args)} failed: {err or 'unknown error'}")
    return proc.stdout.decode("utf-8", "replace")


def iter_log(
    git_dir: str,
    revisions: list[str] | None = None,
    exclude: list[str] | None = None,
    walk_hashes: list[str] | None = None,
):
    """Yield ParsedCommit records for the requested commit selection.

    Either pass ``revisions`` (optionally with ``exclude`` revision prefixes)
    for a normal history walk, or ``walk_hashes`` to diff exactly the listed
    commits (``--no-walk``), used for incremental/batch parsing.
    """
    cmd = _base_cmd(git_dir) + [
        "log",
        "--no-merges",
        "--use-mailmap",
        "--numstat",
        "-z",
        "-M50%",
        "--format=" + FORMAT,
    ]
    if walk_hashes:
        cmd += ["--no-walk=unsorted", *walk_hashes]
    else:
        cmd += list(revisions or [])
        cmd += ["^" + e for e in (exclude or [])]

    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, env=_env())
        parser = LogParser()
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                yield from parser.feed(chunk)
            yield from parser.close()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if proc.returncode != 0:
            errf.seek(0)
            err = errf.read(_MAX_STDERR).decode("utf-8", "replace").strip()
            raise GitError(f"git log failed ({proc.returncode}): {err or 'unknown error'}")


def rev_list(git_dir: str, *args: str) -> list[str]:
    out = run_git(git_dir, "rev-list", *args)
    return [line for line in out.splitlines() if line]


def rev_list_count(git_dir: str, *args: str) -> int:
    out = run_git(git_dir, "rev-list", "--count", *args)
    return int(out.strip() or "0")


def rev_parse(git_dir: str, rev: str) -> str:
    return run_git(git_dir, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()


def list_refs(git_dir: str) -> list[dict]:
    """Head, branches, remote branches and tags, newest tags first."""
    out = run_git(
        git_dir,
        "for-each-ref",
        "--sort=-creatordate",
        "--format=%(refname)%09%(objectname)%09%(creatordate:unix)%09%(objecttype)",
    )
    refs: list[dict] = []
    heads: list[dict] = []
    remotes: list[dict] = []
    tags: list[dict] = []
    seen: set[str] = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        refname, objhash, creatordate, objtype = parts[0], parts[1], parts[2], parts[3]
        if refname.startswith("refs/heads/"):
            heads.append({"name": refname[len("refs/heads/"):], "hash": objhash})
        elif refname.startswith("refs/remotes/"):
            short = refname[len("refs/remotes/"):]
            if short.endswith("/HEAD"):
                continue
            remotes.append({"name": short, "hash": objhash})
        elif refname.startswith("refs/tags/"):
            short = refname[len("refs/tags/"):]
            if objtype != "commit":
                # tag of tag/blob: resolve lazily; keep as-is, rev-parse wins later
                pass
            tags.append({"name": short, "hash": objhash, "ts": int(creatordate or 0)})
    try:
        head_hash = rev_parse(git_dir, "HEAD")
        refs.append({"name": "HEAD", "hash": head_hash})
        seen.add(head_hash)
    except GitError:
        pass
    for group, limit in ((heads, 200), (remotes, 100), (tags, 200)):
        added = 0
        for r in group:
            if r["name"] in seen:
                continue
            refs.append({"name": r["name"], "hash": r["hash"]})
            seen.add(r["name"])
            added += 1
            if added >= limit:
                break
    return refs


def head_commit(git_dir: str) -> str | None:
    try:
        return rev_parse(git_dir, "HEAD")
    except GitError:
        return None
