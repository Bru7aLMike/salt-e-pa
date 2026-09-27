#!/usr/bin/env python3
"""init_workstream.py regression test: generated files pass the NG-0 gates.

Runs `scripts/init_workstream.py` non-interactively against a temp copy of the
shipped memory tree (PA_CONFIG_FILE and PA_MEMORY_DIR point at the copy, so the
real memory/ is never written), then proves:

  1. The generated README.md has OKF frontmatter (`type: workstream` plus a
     one-line `purpose:`) AND keeps every key the workspace scanner reads
     (`workstream_id`, `display_name`, `status`, `created`, `summary`), all at
     the top level, plus the `Backlog: TODO.md` pointer line.
  2. The generated TODO.md has `type: todo`, `purpose:`, the workstream's
     `workstream_id`, `## Open` and `## Done` sections, and no unfilled slot.
  3. `scripts/ng0/okf_check.py <tempdir>` exits 0 over the whole temp tree,
     including both generated files.
  4. `scripts/ng0/template_lint.py` exits 0 over the shipped templates.

Run:  python -m pytest scripts/test_init_workstream.py -v
  or:  python scripts/test_init_workstream.py
"""

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
INIT_PY = SCRIPTS_DIR / "init_workstream.py"
OKF_CHECK = SCRIPTS_DIR / "ng0" / "okf_check.py"
TEMPLATE_LINT = SCRIPTS_DIR / "ng0" / "template_lint.py"
TEMPLATES_DIR = REPO_ROOT / "memory" / "system" / "workspace" / "templates"

SCANNER_KEYS = ("workstream_id", "display_name", "status", "created", "summary")
SLOT_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}|<[A-Z0-9_]+>")
NL = "\n"

# (name, summary, expected slug). The second summary carries a colon-space,
# which YAML would misread if it were written unquoted into the frontmatter.
CASES = (
    ("Sample Stream", "", "sample-stream"),
    ("Colon Case", "Tracks one thing: and another", "colon-case"),
)


def frontmatter(text):
    assert text.startswith("---" + NL), "file does not open with a frontmatter fence"
    block = text.split(NL + "---" + NL, 1)[0][len("---" + NL):]
    data = yaml.safe_load(block)
    assert isinstance(data, dict), "frontmatter is not a mapping"
    return data


_GENERATED = []


def generated():
    """Build the temp tree once per run and return its root (cached)."""
    if _GENERATED:
        return _GENERATED[0]
    root = Path(tempfile.mkdtemp(prefix="init_ws_"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    memory = root / "memory"
    shutil.copytree(REPO_ROOT / "memory", memory)
    env = dict(os.environ)
    env["PA_CONFIG_FILE"] = str(memory / "workstream_config.yml")
    env["PA_MEMORY_DIR"] = str(memory)
    for name, summary, _slug in CASES:
        cmd = [sys.executable, str(INIT_PY), "--name", name,
               "--root", "content/work", "--aliases", "sample"]
        if summary:
            cmd += ["--summary", summary]
        proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    _GENERATED.append(root)
    return root


def test_readme_frontmatter():
    for name, summary, slug in CASES:
        readme = generated() / "memory" / "content" / "work" / slug / "README.md"
        text = readme.read_text(encoding="utf-8")
        fm = frontmatter(text)
        assert fm.get("type") == "workstream"
        purpose = fm.get("purpose")
        assert isinstance(purpose, str) and purpose.strip()
        assert NL not in purpose
        if summary:
            assert purpose == summary
            assert fm.get("summary") == summary
        for key in SCANNER_KEYS:
            assert key in fm, f"{slug}: README lost scanner key `{key}`"
        assert fm["workstream_id"] == slug
        assert fm["display_name"] == name
        assert "metadata" not in fm
        assert NL + "Backlog: TODO.md" in text


def test_todo_file():
    for _name, _summary, slug in CASES:
        todo = generated() / "memory" / "content" / "work" / slug / "TODO.md"
        text = todo.read_text(encoding="utf-8")
        fm = frontmatter(text)
        assert fm.get("type") == "todo"
        assert isinstance(fm.get("purpose"), str) and fm["purpose"].strip()
        assert fm.get("workstream_id") == slug
        assert NL + "## Open" + NL in text and NL + "## Done" + NL in text
        assert text.index("## Open") < text.index("## Done")
        assert not SLOT_RE.search(text), f"{slug}: TODO.md has an unfilled slot"


def test_okf_check_passes_on_generated_tree():
    proc = subprocess.run([sys.executable, str(OKF_CHECK), str(generated())],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_template_lint_passes_on_templates():
    proc = subprocess.run([sys.executable, str(TEMPLATE_LINT),
                           str(TEMPLATES_DIR)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
