"""Frontend sanity checks.

A single syntax error anywhere in the ES modules blanks the entire app, so
every module is parsed with node (when available) and asset references are
validated.  The browser-level behaviour is otherwise covered manually.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
JS_FILES = sorted((FRONTEND / "js").glob("*.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_js_modules_parse(path, tmp_path):
    # copied to .mjs so node treats the `import` statements as ES module syntax
    copy = tmp_path / (path.name + ".mjs")
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr.strip()


def test_index_references_exist():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs, "index.html references no static assets"
    for ref in refs:
        assert (FRONTEND / ref.removeprefix("/static/")).is_file(), ref


def test_module_imports_resolve():
    for path in JS_FILES:
        text = path.read_text(encoding="utf-8")
        for spec in re.findall(r'from\s+"(\./[^"]+)"', text):
            assert (path.parent / spec).is_file(), f"{path.name} imports {spec}"
