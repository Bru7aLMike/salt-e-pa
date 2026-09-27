#!/usr/bin/env sh
# Task-checklist SessionStart hook - tells the model its own session id and,
# after a compaction, lists the open CHECKLIST.md files this session owns so it
# can re-read them before doing anything else.
#
# Claude Code invokes this on session start and pipes a JSON object on stdin:
#   {"session_id": "...", "source": "startup|resume|clear|compact|fork", ...}
# Plain stdout is injected into the model's context. SessionStart cannot block,
# and this hook always exits 0.
#
# Behavior:
#   - session_id missing, blank, or stdin not valid JSON -> no stdout at all
#     (at most one stderr line).
#   - any source with a non-empty session_id -> prints "session_id: <id>".
#   - source "compact" only -> also globs <tasks_dir>/*/*/CHECKLIST.md and lists
#     every file whose frontmatter has status: open AND a non-empty session_id
#     equal to stdin's: its path, its purpose, and each unchecked "- [ ]" line
#     (sub-items included). resume and fork never list checklists; the charter
#     tells the model to follow the handoff pointer and restamp instead.
#   - missing tasks dir on compact -> at most one stderr line.
#
# Tasks dir resolution (first match wins - same layered pattern as the scanner):
#   1. environment variable  PA_TASKS_DIR
#   2. paths.tasks_dir in the workstream config YAML
#   3. <working_dir>/tasks, where working_dir is PA_WORKING_DIR, else
#      paths.working_dir, else the repo root
# The config file is PA_CONFIG_FILE, else <repo>/memory/workstream_config.yml.
# Placeholder values like <TASKS_DIR> count as unset.
#
# No personal paths are baked in. POSIX sh compatible; runs under Git Bash on
# Windows as well.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Repo root = two levels up from scripts/checklist/.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PY=python
if ! command -v python >/dev/null 2>&1; then
    if command -v python3 >/dev/null 2>&1; then
        PY=python3
    else
        echo "checklist hook: no python found" >&2
        exit 0
    fi
fi

CHECKLIST_HOOK_INPUT="$(cat 2>/dev/null || true)"
export CHECKLIST_HOOK_INPUT

"$PY" - "$REPO_ROOT" <<'PY' || true
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PLACEHOLDER = re.compile(r"^<[A-Z0-9_]+>$")
UNCHECKED = re.compile(r"^\s*- \[ \] ")


def warn(msg):
    sys.stderr.write("checklist hook: " + msg + "\n")


def unset(val):
    return not isinstance(val, str) or not val.strip() or bool(PLACEHOLDER.match(val.strip()))


def load_paths(repo_root):
    cfg_env = os.environ.get("PA_CONFIG_FILE", "")
    cfg = Path(cfg_env) if cfg_env.strip() else repo_root / "memory" / "workstream_config.yml"
    try:
        import yaml
        with open(cfg, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    paths = data.get("paths") if isinstance(data, dict) else None
    return paths if isinstance(paths, dict) else {}


def resolve_tasks_dir(repo_root):
    env = os.environ.get("PA_TASKS_DIR", "")
    if env.strip():
        return Path(env).expanduser()
    paths = load_paths(repo_root)
    if not unset(paths.get("tasks_dir")):
        return Path(paths["tasks_dir"].strip()).expanduser()
    wd_env = os.environ.get("PA_WORKING_DIR", "")
    if wd_env.strip():
        return Path(wd_env).expanduser() / "tasks"
    if not unset(paths.get("working_dir")):
        return Path(paths["working_dir"].strip()).expanduser() / "tasks"
    return repo_root / "tasks"


def split_frontmatter(text):
    """Return (dict of top-level scalar keys, body lines). Empty values -> None."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, lines
    fm = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return fm, lines[i + 1:]
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].strip()
        fm[key.strip()] = val or None
    return {}, lines


def main():
    repo_root = Path(sys.argv[1])
    raw = os.environ.get("CHECKLIST_HOOK_INPUT", "")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:
        warn("stdin is not a JSON object; nothing to report")
        return
    sid = data.get("session_id")
    sid = sid.strip() if isinstance(sid, str) else ""
    if not sid:
        warn("no session_id in stdin; nothing to report")
        return

    out = ["session_id: " + sid]
    if data.get("source") == "compact":
        tasks_dir = resolve_tasks_dir(repo_root)
        if not tasks_dir.is_dir():
            warn("tasks dir not found: " + str(tasks_dir))
        else:
            found = []
            for path in sorted(tasks_dir.glob("*/*/CHECKLIST.md")):
                try:
                    fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if fm.get("status") != "open" or fm.get("session_id") != sid:
                    continue
                found.append((path, fm, [l.rstrip() for l in body if UNCHECKED.match(l)]))
            if found:
                out.append("")
                out.append("Open CHECKLIST.md owned by this session. Before any other action, "
                           "re-read it and state the next unchecked item.")
                for path, fm, items in found:
                    out.append("")
                    out.append("checklist: " + path.as_posix())
                    out.append("purpose: " + (fm.get("purpose") or ""))
                    out.append("unchecked:")
                    out.extend(items if items else ["(none)"])
    print("\n".join(out))


try:
    main()
except Exception as exc:
    warn("unexpected error: " + exc.__class__.__name__)
PY

exit 0
