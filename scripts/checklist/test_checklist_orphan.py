#!/usr/bin/env python3
"""Scanner orphan-checklist WARN test.

The workspace scanner emits one WARN for each task CHECKLIST.md whose
frontmatter says `status: open` and whose mtime is older than 48h. A `done`
checklist of any age produces no finding, and a missing tasks/ folder (fresh
clone) produces no finding and no crash.

Fixtures are filled from the shipped template
memory/system/workspace/templates/CHECKLIST.md. The scanner runs as a
subprocess with PA_MEMORY_DIR, PA_WORKING_DIR and PA_TASKS_DIR pointed at temp
copies, so the repo's own memory/ tree is never written.

Run:  python scripts/checklist/test_checklist_orphan.py
  or:  python -m pytest scripts/checklist/test_checklist_orphan.py -v
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # scripts/checklist/
SCRIPTS_DIR = HERE.parent                        # scripts/
REPO_ROOT = SCRIPTS_DIR.parent                   # repo root
SCANNER_PY = SCRIPTS_DIR / "workspace_scanner.py"
MEMORY_TEMPLATE = REPO_ROOT / "memory"
CHECKLIST_TEMPLATE = (
    MEMORY_TEMPLATE / "system" / "workspace" / "templates" / "CHECKLIST.md"
)
ORPHAN_MARK = "Orphan checklist:"


def _fill_checklist(status: str, task_slug: str) -> str:
    text = CHECKLIST_TEMPLATE.read_text(encoding="utf-8")
    fills = {
        "<TASK_GOAL>": f"Fixture goal for {task_slug}",
        "<WORKSTREAM_ID>": "sample-ws",
        "<STATUS>": status,
        "<CREATED>": "2026-01-01",
        "<SESSION_ID>": "fixture-session",
        "{{TASK_SLUG}}": task_slug,
        "{{ITEM_1}}": "first item",
        "{{SUB_ITEM_1}}": "first sub item",
        "{{ITEM_2}}": "second item",
    }
    for slot, value in fills.items():
        assert slot in text, f"template lost slot {slot}"
        text = text.replace(slot, value)
    if status == "done":
        text = text.replace("- [ ]", "- [x]")
    assert "<" + "STATUS>" not in text and "{{" not in text, "unfilled slot"
    return text


def _write_fixture(tasks_dir: Path, slug: str, status: str, age_hours: float) -> Path:
    path = tasks_dir / "sample-ws" / slug / "CHECKLIST.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_fill_checklist(status, slug), encoding="utf-8")
    ts = time.time() - age_hours * 3600
    os.utime(path, (ts, ts))
    return path


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _run_scanner(mem: Path, work: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PA_MEMORY_DIR"] = str(mem)
    env["PA_WORKING_DIR"] = str(work)
    env["PA_TASKS_DIR"] = str(work / "tasks")
    env["PA_CLAUDE_MD"] = str(REPO_ROOT / "CLAUDE.md")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(SCANNER_PY)],
        env=env, capture_output=True, text=True,
    )


def _warn_lines(integrity: str) -> list[str]:
    lines = integrity.splitlines()
    out: list[str] = []
    in_warn = False
    for line in lines:
        if line.startswith("## "):
            in_warn = line.startswith("## WARN")
            continue
        if in_warn and line.startswith("- ") and line != "- (none)":
            out.append(line)
    return out


def _scan(tmp: Path, with_tasks: bool) -> tuple[subprocess.CompletedProcess, list[str]]:
    mem = tmp / "memory"
    work = tmp / "work"
    shutil.copytree(MEMORY_TEMPLATE, mem)
    work.mkdir()
    if with_tasks:
        tasks = work / "tasks"
        _write_fixture(tasks, "open-47h", "open", 47)
        _write_fixture(tasks, "open-49h", "open", 49)
        _write_fixture(tasks, "done-100h", "done", 100)
    result = _run_scanner(mem, work)
    integrity = (mem / "INTEGRITY.md").read_text(encoding="utf-8")
    return result, _warn_lines(integrity)


def test_orphan_warn_only_for_stale_open_checklist():
    before = _tree_digest(MEMORY_TEMPLATE)
    with tempfile.TemporaryDirectory() as td:
        result, warns = _scan(Path(td), with_tasks=True)
    assert result.returncode == 0, f"scanner exit {result.returncode}: {result.stderr}"
    orphan = [w for w in warns if ORPHAN_MARK in w]
    assert len(orphan) == 1, f"expected 1 orphan WARN, got {orphan}"
    assert "tasks/sample-ws/open-49h/CHECKLIST.md" in orphan[0], orphan[0]
    for w in warns:
        assert "open-47h" not in w, f"47h checklist flagged: {w}"
        assert "done-100h" not in w, f"done checklist flagged: {w}"
    assert _tree_digest(MEMORY_TEMPLATE) == before, "repo memory/ was modified"
    print(f"orphan WARN: {orphan[0]}")


def test_no_tasks_dir_no_crash_no_warn():
    before = _tree_digest(MEMORY_TEMPLATE)
    with tempfile.TemporaryDirectory() as td:
        result, warns = _scan(Path(td), with_tasks=False)
    assert result.returncode == 0, f"scanner exit {result.returncode}: {result.stderr}"
    assert not [w for w in warns if ORPHAN_MARK in w], warns
    assert _tree_digest(MEMORY_TEMPLATE) == before, "repo memory/ was modified"


def main() -> int:
    failed = 0
    for name, fn in [
        ("test_orphan_warn_only_for_stale_open_checklist",
         test_orphan_warn_only_for_stale_open_checklist),
        ("test_no_tasks_dir_no_crash_no_warn", test_no_tasks_dir_no_crash_no_warn),
    ]:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"{2 - failed}/2 passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
