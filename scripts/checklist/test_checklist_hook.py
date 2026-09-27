#!/usr/bin/env python3
"""Unit test for scripts/checklist/checklist-session-start.sh.

Builds throwaway task trees from the shipped CHECKLIST.md template
(memory/system/workspace/templates/CHECKLIST.md) by substituting its slots, then
runs the hook through bash with PA_TASKS_DIR pointed at the tree and a JSON
object on stdin, the same way Claude Code runs a SessionStart hook.

Run from the repo root:
    python scripts/checklist/test_checklist_hook.py

Exits 0 when all 12 asserts pass. Also collectable by pytest. Set
CHECKLIST_TEST_BASH to force a specific bash binary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
HOOK = HERE / "checklist-session-start.sh"
TEMPLATE = REPO_ROOT / "memory" / "system" / "workspace" / "templates" / "CHECKLIST.md"
TEMPLATE_ITEM_LINES = ["- [ ] {{ITEM_1}}", "  - [ ] {{SUB_ITEM_1}}", "- [ ] {{ITEM_2}}"]

ID_A = "sess-aaaa-1111"
ID_B = "sess-bbbb-2222"

# Items per fixture checklist. Unchecked lines are what the hook must list.
ITEMS_A = [
    "- [x] Copy the sample files",
    "- [ ] Import batch two",
    "  - [ ] Verify CSV headers",
    "  - [x] Count rows",
    "- [ ] Write the import summary",
]
UNCHECKED_A = ["- [ ] Import batch two", "  - [ ] Verify CSV headers",
               "- [ ] Write the import summary"]
ITEMS_B = ["- [ ] Other session item one", "- [ ] Other session item two"]
ITEMS_DONE = ["- [x] Finished item one", "- [ ] Leftover item on a done file"]
ITEMS_BLANK = ["- [ ] Blank stamp item one"]


def find_bash() -> str:
    forced = os.environ.get("CHECKLIST_TEST_BASH", "").strip()
    if forced:
        return forced
    found = shutil.which("bash")
    if not found:
        raise RuntimeError("bash not found on PATH")
    if os.name == "nt" and "system32" in found.lower():
        raise RuntimeError("PATH resolves bash to the WSL launcher (%s); run from Git Bash "
                           "or set CHECKLIST_TEST_BASH" % found)
    return found


BASH = find_bash()


def render_checklist(*, purpose: str, workstream: str, status: str, session_id: str,
                     slug: str, items: list[str]) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    for line in TEMPLATE_ITEM_LINES:
        assert line in text.splitlines(), "template item line missing: " + line
    subs = {
        "<TASK_GOAL>": purpose,
        "<WORKSTREAM_ID>": workstream,
        "<STATUS>": status,
        "<CREATED>": "2026-01-15",
        "<SESSION_ID>": session_id,
        "{{TASK_SLUG}}": slug,
    }
    for slot, value in subs.items():
        assert slot in text, "template slot missing: " + slot
        text = text.replace(slot, value)
    lines = text.splitlines()
    start = lines.index(TEMPLATE_ITEM_LINES[0])
    lines[start:start + len(TEMPLATE_ITEM_LINES)] = items
    out = "\n".join(lines) + "\n"
    assert "<" + "SESSION_ID>" not in out and "{{" not in out, "unfilled slot left"
    return out


def write_checklist(root: Path, workstream: str, slug: str, **kw) -> Path:
    path = root / workstream / slug / "CHECKLIST.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_checklist(workstream=workstream, slug=slug, **kw), encoding="utf-8")
    return path


def make_full_tree(root: Path) -> dict[str, Path]:
    return {
        "a": write_checklist(root, "ws-one", "task-a", purpose="Import the sample batches",
                             status="open", session_id=ID_A, items=ITEMS_A),
        "b": write_checklist(root, "ws-one", "task-b", purpose="Other session task",
                             status="open", session_id=ID_B, items=ITEMS_B),
        "done": write_checklist(root, "ws-two", "task-done", purpose="Finished task",
                                status="done", session_id=ID_A, items=ITEMS_DONE),
        "blank": write_checklist(root, "ws-two", "task-blank", purpose="Unstamped task",
                                 status="open", session_id="", items=ITEMS_BLANK),
    }


def run_hook(tasks_dir: Path, stdin: str) -> tuple[subprocess.CompletedProcess, float]:
    env = dict(os.environ)
    env["PA_TASKS_DIR"] = str(tasks_dir)
    env["PA_CONFIG_FILE"] = str(tasks_dir.parent / "no-such-config.yml")
    env.pop("PA_WORKING_DIR", None)
    t0 = time.perf_counter()
    proc = subprocess.run([BASH, str(HOOK)], input=stdin.encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    elapsed = time.perf_counter() - t0
    proc.stdout = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    proc.stderr = proc.stderr.decode("utf-8").replace("\r\n", "\n")
    return proc, elapsed


def payload(source: str, session_id=None, *, include_id: bool = True) -> str:
    obj = {"hook_event_name": "SessionStart", "source": source,
           "transcript_path": "/tmp/transcript.jsonl", "cwd": "/tmp"}
    if include_id:
        obj["session_id"] = session_id
    return json.dumps(obj)


def stderr_lines(proc) -> int:
    return len([l for l in proc.stderr.splitlines() if l.strip()])


def checklist_lines(stdout: str) -> list[str]:
    return [l for l in stdout.splitlines() if l.lstrip().startswith("- [")]


# ---------------------------------------------------------------- the 12 cases

def case_a_compact_lists_own_open(tmp: Path):
    paths = make_full_tree(tmp / "tasks")
    proc, elapsed = run_hook(tmp / "tasks", payload("compact", ID_A))
    assert proc.returncode == 0, proc
    out = proc.stdout
    assert out.splitlines()[0] == "session_id: " + ID_A, out
    assert paths["a"].as_posix() in out, out
    assert "purpose: Import the sample batches" in out, out
    assert checklist_lines(out) == UNCHECKED_A, checklist_lines(out)
    for other in ("b", "done", "blank"):
        assert paths[other].as_posix() not in out, other
    return elapsed


def case_b_compact_other_session_only(tmp: Path):
    root = tmp / "tasks"
    write_checklist(root, "ws-one", "task-b", purpose="Other session task",
                    status="open", session_id=ID_B, items=ITEMS_B)
    proc, _ = run_hook(root, payload("compact", ID_A))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "session_id: " + ID_A, proc.stdout


def case_c_compact_done_only(tmp: Path):
    root = tmp / "tasks"
    write_checklist(root, "ws-two", "task-done", purpose="Finished task",
                    status="done", session_id=ID_A, items=ITEMS_DONE)
    proc, _ = run_hook(root, payload("compact", ID_A))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "session_id: " + ID_A, proc.stdout


def case_d_startup(tmp: Path):
    make_full_tree(tmp / "tasks")
    proc, _ = run_hook(tmp / "tasks", payload("startup", ID_A))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "session_id: " + ID_A, proc.stdout


def case_e_malformed_stdin(tmp: Path):
    make_full_tree(tmp / "tasks")
    proc, _ = run_hook(tmp / "tasks", '{"session_id": "x", "source": "compact"')
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", proc.stdout
    assert stderr_lines(proc) <= 1, proc.stderr


def case_f_missing_tasks_dir(tmp: Path):
    proc, _ = run_hook(tmp / "no-tasks-here", payload("compact", ID_A))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "session_id: " + ID_A, proc.stdout
    assert stderr_lines(proc) <= 1, proc.stderr


def case_g_wall_time(tmp: Path):
    make_full_tree(tmp / "tasks")
    run_hook(tmp / "tasks", payload("compact", ID_A))  # warm-up
    proc, elapsed = run_hook(tmp / "tasks", payload("compact", ID_A))
    assert proc.returncode == 0 and checklist_lines(proc.stdout) == UNCHECKED_A
    assert elapsed < 1.0, "wall time %.3fs" % elapsed
    return elapsed


def case_h_compact_missing_id(tmp: Path):
    make_full_tree(tmp / "tasks")
    proc, _ = run_hook(tmp / "tasks", payload("compact", include_id=False))
    assert proc.returncode == 0
    assert proc.stdout == "", proc.stdout
    assert stderr_lines(proc) <= 1, proc.stderr


def case_i_compact_blank_id(tmp: Path):
    make_full_tree(tmp / "tasks")
    for blank in ("", "   ", "\t"):
        proc, _ = run_hook(tmp / "tasks", payload("compact", blank))
        assert proc.returncode == 0
        assert proc.stdout == "", (repr(blank), proc.stdout)
        assert stderr_lines(proc) <= 1, proc.stderr


def case_j_resume(tmp: Path):
    make_full_tree(tmp / "tasks")
    proc, _ = run_hook(tmp / "tasks", payload("resume", ID_A))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "session_id: " + ID_A, proc.stdout


def case_k_fork(tmp: Path):
    make_full_tree(tmp / "tasks")
    proc, _ = run_hook(tmp / "tasks", payload("fork", ID_A))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "session_id: " + ID_A, proc.stdout


def case_l_repo_relative_default(tmp: Path):
    # No PA_TASKS_DIR: the hook falls back to <repo>/tasks, two levels above
    # scripts/checklist/. Run a copy of the hook inside a temp repo so the
    # fallback path (and Git Bash path handling on Windows) is exercised.
    repo = tmp / "repo"
    (repo / "scripts" / "checklist").mkdir(parents=True)
    hook_copy = repo / "scripts" / "checklist" / HOOK.name
    shutil.copy2(HOOK, hook_copy)
    paths = make_full_tree(repo / "tasks")
    env = dict(os.environ)
    env.pop("PA_TASKS_DIR", None)
    env.pop("PA_WORKING_DIR", None)
    env["PA_CONFIG_FILE"] = str(tmp / "no-such-config.yml")
    proc = subprocess.run([BASH, str(hook_copy)], input=payload("compact", ID_A).encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    out = proc.stdout.decode("utf-8").replace("\r\n", "\n")
    assert proc.returncode == 0, proc
    assert paths["a"].as_posix() in out, out
    assert checklist_lines(out) == UNCHECKED_A, checklist_lines(out)


CASES = [
    ("a", "compact + open stamped A -> path and exactly its unchecked items",
     case_a_compact_lists_own_open),
    ("b", "compact + open stamped B only -> no checklist", case_b_compact_other_session_only),
    ("c", "compact + done stamped A -> no checklist", case_c_compact_done_only),
    ("d", "startup + A -> session_id only", case_d_startup),
    ("e", "malformed stdin -> exit 0, <=1 stderr line", case_e_malformed_stdin),
    ("f", "missing tasks dir -> exit 0, <=1 stderr line", case_f_missing_tasks_dir),
    ("g", "case (a) wall time under 1s", case_g_wall_time),
    ("h", "compact, no session_id key -> empty stdout", case_h_compact_missing_id),
    ("i", "compact, blank or whitespace session_id -> empty stdout", case_i_compact_blank_id),
    ("j", "resume + A -> session_id only, no listing", case_j_resume),
    ("k", "fork + A -> session_id only, no listing", case_k_fork),
    ("l", "compact, no PA_TASKS_DIR -> repo-relative tasks/ default", case_l_repo_relative_default),
]


def _run_case(fn):
    with tempfile.TemporaryDirectory() as td:
        return fn(Path(td))


# pytest entry points
def test_a(): _run_case(case_a_compact_lists_own_open)
def test_b(): _run_case(case_b_compact_other_session_only)
def test_c(): _run_case(case_c_compact_done_only)
def test_d(): _run_case(case_d_startup)
def test_e(): _run_case(case_e_malformed_stdin)
def test_f(): _run_case(case_f_missing_tasks_dir)
def test_g(): _run_case(case_g_wall_time)
def test_h(): _run_case(case_h_compact_missing_id)
def test_i(): _run_case(case_i_compact_blank_id)
def test_j(): _run_case(case_j_resume)
def test_k(): _run_case(case_k_fork)
def test_l(): _run_case(case_l_repo_relative_default)


def main() -> int:
    failed = 0
    for key, desc, fn in CASES:
        try:
            result = _run_case(fn)
            extra = " (%.3fs)" % result if isinstance(result, float) else ""
            print("PASS (%s) %s%s" % (key, desc, extra))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL (%s) %s: %s" % (key, desc, exc))
    print("%d/%d passed" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
