#!/usr/bin/env python3
"""Deadline scanner for personal-assistant memory.

Walks the memory directory, extracts deadline-bearing items from .md files,
and writes DEADLINES.md with overdue / imminent / upcoming categorization.

Conventions detected:
- Frontmatter `deadline: YYYY-MM-DD` -> whole-file deadline
- Inline `**Due:** YYYY-MM-DD` -> per-line deadline
- Inline `**Deadline:** YYYY-MM-DD` -> per-line deadline (alias)

Run manually:  python deadline_scanner.py
Run from cron: same, no args needed.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

# ----- Config-driven path resolution ---------------------------------------
#
# Nothing personal is hard-coded. Every path and locale constant resolves with a
# fixed precedence, so a cloner never edits this file:
#   1. environment variable (the per-key PA_* name)
#   2. the value in memory/workstream_config.yml
#   3. a sensible repo-relative default
#
# The config file itself is located by PA_CONFIG_FILE, else the repo-relative
# default memory/workstream_config.yml (relative to this script's parent dir).
# Locating the config independently of memory_dir avoids a chicken-and-egg on
# where the config lives while still letting memory_dir be config/env-driven.

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPO_ROOT = SCRIPT_DIR.parent

_PLACEHOLDER_RE = re.compile(r"^\s*(?:\{\{[A-Z0-9_]+\}\}|<[A-Z0-9_]+>)\s*$")


def _is_placeholder(value: Any) -> bool:
    """True if a config value is unset or a shipped placeholder token."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return not value.strip() or bool(_PLACEHOLDER_RE.match(value.strip()))


def _load_config_raw(path: Path) -> dict:
    """Best-effort YAML load of the config file. Empty dict on any error.

    The shipped scaffold config is a placeholder template that may not even be
    valid YAML; degrading to {} lets every key fall through to its default so the
    scanner still runs over the empty tree.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_path(env_var: str, config_value: Any, default: Path) -> Path:
    env = os.environ.get(env_var)
    if env and env.strip():
        return Path(env).expanduser()
    if isinstance(config_value, str) and not _is_placeholder(config_value):
        return Path(config_value).expanduser()
    return default


_cfg_env = os.environ.get("PA_CONFIG_FILE")
CONFIG_FILE = (
    Path(_cfg_env).expanduser() if _cfg_env and _cfg_env.strip()
    else DEFAULT_REPO_ROOT / "memory" / "workstream_config.yml"
)
_CONFIG = _load_config_raw(CONFIG_FILE)
_PATHS = _CONFIG.get("paths") if isinstance(_CONFIG.get("paths"), dict) else {}

MEMORY_DIR = _resolve_path(
    "PA_MEMORY_DIR", _PATHS.get("memory_dir"), DEFAULT_REPO_ROOT / "memory"
)
WORKING_DIR = _resolve_path(
    "PA_WORKING_DIR", _PATHS.get("working_dir"), DEFAULT_REPO_ROOT
)
OUTPUT_FILE = MEMORY_DIR / "DEADLINES.md"
# External coupling (jira sync is NOT extracted this step): the deadline scanner
# only READS this cache if present; a missing file degrades to zero Jira items.
JIRA_SYNC_JSON = _resolve_path(
    "PA_JIRA_SYNC_JSON", _PATHS.get("jira_sync_json"),
    WORKING_DIR / "data" / "jira-sync.json",
)


def _resolve_local_tz() -> timezone:
    """Fixed-offset local timezone from locale config. Default: UTC.

    No timezone is hard-coded; the offset comes from config or env. Env
    overrides: PA_TZ_OFFSET_HOURS (float) and PA_TZ_NAME (str).
    """
    locale = _CONFIG.get("locale") if isinstance(_CONFIG.get("locale"), dict) else {}
    offset: float | None = None
    off_env = os.environ.get("PA_TZ_OFFSET_HOURS")
    if off_env and off_env.strip():
        try:
            offset = float(off_env)
        except ValueError:
            offset = None
    if offset is None:
        cv = locale.get("utc_offset_hours")
        if isinstance(cv, (int, float)):
            offset = float(cv)
    if offset is None:
        offset = 0.0
    name_env = os.environ.get("PA_TZ_NAME")
    tz_name = locale.get("tz_name")
    if name_env and name_env.strip():
        name = name_env.strip()
    elif isinstance(tz_name, str) and not _is_placeholder(tz_name):
        name = tz_name.strip()
    else:
        name = "UTC"
    return timezone(timedelta(hours=offset), name=name)


LOCAL_TZ = _resolve_local_tz()

# Schedule reference: daily at 08:00 local. Used to compute next_run.
CRON_HOUR = 8
CRON_MINUTE = 0

INLINE_DUE_RE = re.compile(r"\*\*(?:Due|Deadline):\*\*\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
FRONTMATTER_DEADLINE_RE = re.compile(r"^deadline:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
FRONTMATTER_BLOCK_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CODESPAN_RE = re.compile(r"`[^`\n]*`")  # inline code spans -- ignore quoted examples
FENCE_RE = re.compile(r"^\s*```")  # fenced code block delimiters


def parse_frontmatter(text):
    """Return (frontmatter_dict, body) from a markdown text."""
    m = FRONTMATTER_BLOCK_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    rest = text[m.end():]
    fm = {}
    for line in block.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, rest


def extract_deadlines(file_path: Path):
    """Yield (date_str, title, rel_path) tuples for one file."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return
    fm, body = parse_frontmatter(text)
    rel = file_path.relative_to(MEMORY_DIR).as_posix()

    # Whole-file deadline from frontmatter
    fm_deadline = fm.get("deadline", "")
    if DATE_ONLY_RE.match(fm_deadline):
        title = fm.get("name") or fm.get("title") or file_path.stem
        yield (fm_deadline, f"{title} (whole file)", rel)

    # Inline deadlines per line. Skip fenced code blocks; strip inline code spans.
    in_fence = False
    for line_no, line in enumerate(body.splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        scan_line = CODESPAN_RE.sub("", line)
        for m in INLINE_DUE_RE.finditer(scan_line):
            date_str = m.group(1)
            # Build a clean title by stripping the marker + common bullet/checkbox
            title = INLINE_DUE_RE.sub("", scan_line).strip()
            title = re.sub(r"^[-*+]\s*(?:\[[ xX]\]\s*)?", "", title).strip()
            title = title.strip("*_-:.·—– \t")
            if not title:
                title = f"(line {line_no})"
            yield (date_str, title, rel)


def categorize(items, today):
    overdue, imminent, upcoming = [], [], []
    for date_str, title, rel in items:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        diff = (d - today).days
        entry = (d, diff, title, rel)
        if diff < 0:
            overdue.append(entry)
        elif diff <= 7:
            imminent.append(entry)
        elif diff <= 30:
            upcoming.append(entry)
    overdue.sort(key=lambda x: x[0])
    imminent.sort(key=lambda x: x[0])
    upcoming.sort(key=lambda x: x[0])
    return overdue, imminent, upcoming


def fmt_diff(diff):
    if diff < 0:
        return f"{abs(diff)}d overdue"
    if diff == 0:
        return "today"
    return f"+{diff}d"


def load_jira_deadlines() -> list:
    """Return (date_str, title, rel_path) tuples for Jira tickets with due_date set.

    Returns empty list on any error (graceful degradation).
    Currently a no-op since all due_dates are null, but ready for when they appear.
    """
    try:
        text = JIRA_SYNC_JSON.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return []
        projects = data.get("projects") or {}
        items = []
        for proj in projects.values():
            for ticket in (proj.get("tickets") or []):
                due = ticket.get("due_date")
                if not due:
                    continue
                title = f"{ticket.get('key', '')}: {ticket.get('summary', '')}"
                items.append((due, title, "data/jira-sync.json"))
        return items
    except Exception:
        return []


def main():
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    next_run = now.replace(hour=CRON_HOUR, minute=CRON_MINUTE, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)

    items = []
    for md in MEMORY_DIR.rglob("*.md"):
        if md.name == "DEADLINES.md":
            continue
        items.extend(extract_deadlines(md))

    items.extend(load_jira_deadlines())

    overdue, imminent, upcoming = categorize(items, today)

    lines = []
    lines.append("---")
    # OKF v0.2: keep the regenerated file conformant (non-empty top-level type
    # plus a one-line summary) so scripts/ng0/okf_check.py stays green after a
    # live scan, matching the shipped stub's type.
    lines.append("type: register")
    lines.append("purpose: Deadline register generated from dated items "
                 "across the memory tree.")
    lines.append(f"last_run: {now.isoformat(timespec='seconds')}")
    lines.append(f"next_run: {next_run.isoformat(timespec='seconds')}")
    lines.append("generator: pa-deadline-scan")
    lines.append("---")
    lines.append("")
    lines.append("# Active Deadlines (auto-generated)")
    lines.append("")
    lines.append("> Generated by pa-deadline-scan. Do not edit by hand.")
    lines.append("> To add a deadline: use `**Due:** YYYY-MM-DD` inline in any tasks file, or `deadline: YYYY-MM-DD` in frontmatter for whole-file deadlines.")
    lines.append("")
    lines.append(f"**Last run:** {now.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append(f"**Next scheduled run:** {next_run.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")

    def fmt_block(title, items):
        if not items:
            return [f"## {title}", "", "_None._", ""]
        out = [f"## {title}", ""]
        for d, diff, t, rel in items:
            out.append(f"- {d.isoformat()} ({fmt_diff(diff)}) - {t} - `{rel}`")
        out.append("")
        return out

    lines.extend(fmt_block("🚨 OVERDUE", overdue))
    lines.extend(fmt_block("⏰ IMMINENT (next 7 days)", imminent))
    lines.extend(fmt_block("📅 UPCOMING (8-30 days)", upcoming))

    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")
    print(f"  Overdue: {len(overdue)}  Imminent: {len(imminent)}  Upcoming: {len(upcoming)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
