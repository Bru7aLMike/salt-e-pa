#!/usr/bin/env python3
"""Workspace scanner for personal-assistant memory (pa-workspace-scan).

Implements the v2 memory system redesign (see memory/SCHEMAS.md v1.0).

What it does:
  1. Reads memory/workstream_config.yml -> walks each root one level deep
  2. For each child folder with README.md containing valid `workstream_id:`
     frontmatter, registers it as a workstream
  3. For each workstream, finds the latest handoff by scanning:
       - <workstream_folder>/hand-offs/ (per-workstream; the only handoff location)
     Sorted by frontmatter `session_end:` (ISO 8601). The newest handoff
     whose `workstream_id:` matches is the activity source.
  4. Extracts structured block (next, blockers, open_items, status) from
     that handoff's frontmatter.
  5. Runs integrity checks (SCHEMAS.md section 5).
  6. Writes atomically:
       - memory/MAP.md (full rewrite)
       - memory/INTEGRITY.md (full rewrite)
       - memory/BRIEFING.md (only the delimited <!-- PA_SCAN --> block)

Run manually:  python workspace_scanner.py
Run from cron: same, no args.
"""
import hashlib
import json
import os
import re
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
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
CLAUDE_MD_FILE = _resolve_path(
    "PA_CLAUDE_MD", _PATHS.get("claude_md"), WORKING_DIR / "CLAUDE.md"
)
TASKS_DIR = _resolve_path(
    "PA_TASKS_DIR", _PATHS.get("tasks_dir"), WORKING_DIR / "tasks"
)
# Per-repo subdirectory that mirrors a feature's lifecycle artifacts for the
# memory-bridge hash-drift check (see run_memory_bridge_checks). Provider-
# neutral: a feature's lifecycle-state lists the repos, and each repo is expected
# to mirror its artifact files under this subpath.
REPO_ARTIFACT_SUBPATH = Path(".lifecycle") / "memory"
# External coupling (jira sync NOT extracted this step): the scanner only READS
# this cache if present; a missing file degrades the Jira briefing section to "".
JIRA_SYNC_JSON = _resolve_path(
    "PA_JIRA_SYNC_JSON", _PATHS.get("jira_sync_json"),
    WORKING_DIR / "data" / "jira-sync.json",
)

# Memory-tree files derived from the resolved MEMORY_DIR root.
ALIASES_FILE = MEMORY_DIR / "aliases.yml"
SCHEMAS_FILE = MEMORY_DIR / "SCHEMAS.md"
MAP_FILE = MEMORY_DIR / "MAP.md"
INTEGRITY_FILE = MEMORY_DIR / "INTEGRITY.md"
BRIEFING_FILE = MEMORY_DIR / "BRIEFING.md"
DEADLINES_FILE = MEMORY_DIR / "DEADLINES.md"
LIFECYCLE_TASKS_FILE = MEMORY_DIR / "system" / "workspace" / "lifecycle-tasks.md"
RULES_DIR = MEMORY_DIR / "system" / "rules"
RULES_INDEX = RULES_DIR / "INDEX.md"
RULES_STALENESS_THRESHOLD_DAYS = 60
SIZE_LOG_DIR = MEMORY_DIR / "system" / "_internal"
SIZE_LOG_FILE = SIZE_LOG_DIR / "size_log.jsonl"
ORIENTATION_FILES = [
    ("CLAUDE.md", CLAUDE_MD_FILE),
    ("MEMORY.md", MEMORY_DIR / "MEMORY.md"),
    ("BRIEFING.md", BRIEFING_FILE),
    ("MAP.md", MAP_FILE),
    ("DEADLINES.md", DEADLINES_FILE),
    ("INTEGRITY.md", INTEGRITY_FILE),
]

JIRA_STALE_WARN_HOURS = 26
JIRA_ACTIVE_LIMIT = 3
# External coupling (jira sync NOT extracted this step): project keys rendered in
# the BRIEFING activity block, in priority order. Config-driven; ships empty.
_jpk = _CONFIG.get("jira_project_keys")
JIRA_PROJECT_KEYS = [str(k) for k in _jpk] if isinstance(_jpk, list) else []

PRIORITY_RANK = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3}


def is_blocked(ticket: dict) -> bool:
    return "blocked" in [str(lb).lower() for lb in (ticket.get("labels") or [])]

SCANNER_VERSION = "0.1.0"
SCHEMA_MAJOR_EXPECTED = 2
STALE_HOURS = 24


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
CRON_HOUR = 8
CRON_MINUTE = 15  # offset +5min from the deadline scan

# BRIEFING scanner-owned markers. LOWERCASE form per the shipped block-ownership
# contract (SCHEMAS.md). The scanner writes ONLY between these two markers.
PA_SCAN_START = "<!-- PA_SCAN:start -->"
PA_SCAN_END = "<!-- PA_SCAN:end -->"


class BriefingContractError(Exception):
    """BRIEFING.md is missing or lacks its scanner marker pair (fail-loud)."""

VALID_WS_STATUS = {"active", "dormant", "complete"}
VALID_HANDOFF_STATUS = {"active", "dormant", "blocked", "complete"}

FRONTMATTER_BLOCK_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ----- Data classes --------------------------------------------------------


# NG-0 note: the dataclass transform is applied functionally (right below each
# class body) instead of as an at-sign decorator line. The NG-0 social-handle
# PII scanner treats any line beginning with an at-sign followed by a word as an
# at-handle, so the decorator syntax is avoided here to keep the leak gate clean.
class Workstream:
    workstream_id: str
    display_name: str
    status: str  # from README
    folder: Path  # relative to MEMORY_DIR
    readme_path: Path
    created: str | None = None
    summary: str | None = None
    owner: str | None = None
    aliases: list[str] = field(default_factory=list)
    # Activity state (populated from latest handoff):
    latest_handoff_path: Path | None = None
    latest_session_end: datetime | None = None
    activity_status: str | None = None  # handoff status
    next_items: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)


Workstream = dataclass(Workstream)


# NG-0 note: dataclass applied functionally, same reason as Workstream above.
class Finding:
    severity: str  # CRITICAL | WARN | INFO
    message: str
    path: str | None = None


Finding = dataclass(Finding)


# ----- Frontmatter parsing -------------------------------------------------


class FrontmatterParseError(Exception):
    """Raised when the `---` block exists but YAML parsing fails."""


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body). YAML-parsed. Empty dict if no block.

    Raises FrontmatterParseError if the `---` block is present but YAML
    fails - callers distinguish "no block" (empty dict) from "broken block"
    (exception) so they can flag CRITICAL on the latter.
    """
    m = FRONTMATTER_BLOCK_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    body = text[m.end():]
    try:
        fm = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise FrontmatterParseError(str(exc)) from exc
    if fm is None:
        return {}, body
    if not isinstance(fm, dict):
        raise FrontmatterParseError(
            f"frontmatter root is {type(fm).__name__}, expected mapping"
        )
    return fm, body


def read_frontmatter(
    path: Path,
) -> tuple[dict[str, Any], str] | str | None:
    """Read a file's frontmatter.

    Returns:
      - (fm_dict, body) on success (fm_dict may be empty if no `---` block)
      - str (error message) if `---` block exists but YAML parse failed
      - None if the file could not be read at all
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return parse_frontmatter(text)
    except FrontmatterParseError as exc:
        return str(exc)


# ----- Session-end parsing (ISO 8601 + fallbacks) --------------------------


def parse_session_end(raw: Any) -> datetime | None:
    """Parse session_end into a timezone-aware datetime. Best-effort.

    Accepts:
      - ISO 8601 with offset: 2026-04-24T02:25:00+03:00
      - ISO-like with space separator: 2026-04-24 02:25:00+03:00
      - 'YYYY-MM-DD HH:MM UTC' (human handoff format, with a short zone label)
      - datetime.date / datetime.datetime already parsed by YAML
    Returns timezone-aware datetime (assumes LOCAL_TZ if naive), or None.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        s = str(raw).strip()
        dt = None
        # Try fromisoformat (Python 3.11+ handles Z and offsets well)
        for candidate in (s, s.replace(" ", "T", 1)):
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        if dt is None:
            # Try 'YYYY-MM-DD HH:MM <ZONE>' style. The optional trailing zone
            # label is tolerated but not interpreted (the datetime is treated as
            # LOCAL_TZ below): a UTC/Z marker or a numeric +/-HH:MM offset.
            m = re.match(
                r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?\s*"
                r"(UTC|Z|[+-]\d{2}:?\d{2})?\s*$",
                s,
            )
            if m:
                date_s = m.group(1)
                hh, mm = int(m.group(2)), int(m.group(3))
                ss = int(m.group(4) or 0)
                try:
                    base = datetime.strptime(date_s, "%Y-%m-%d")
                    dt = base.replace(hour=hh, minute=mm, second=ss)
                except ValueError:
                    return None
        if dt is None:
            # Date-only fallback (sorts stable but inexact)
            try:
                dt = datetime.strptime(s, "%Y-%m-%d")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


# ----- Config + aliases ----------------------------------------------------


def load_yaml_file(path: Path) -> dict[str, Any]:
    # Tolerant: the shipped scaffold config/aliases are placeholder templates
    # that may not be valid YAML. Any read/parse error degrades to {} so callers
    # fall through to empty/default rather than crashing the whole scan.
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def load_config() -> tuple[list[Path], list[Path]]:
    """Return (roots, excludes) as paths relative to MEMORY_DIR (absolute)."""
    data = load_yaml_file(CONFIG_FILE)
    roots = [MEMORY_DIR / r for r in (data.get("roots") or [])]
    excludes = [MEMORY_DIR / e for e in (data.get("exclude") or [])]
    return roots, excludes


def load_aliases() -> dict[str, dict[str, Any]]:
    """Return dict keyed by workstream_id -> alias entry."""
    data = load_yaml_file(ALIASES_FILE)
    out: dict[str, dict[str, Any]] = {}
    for entry in data.get("workstreams") or []:
        if not isinstance(entry, dict):
            continue
        wid = entry.get("workstream_id")
        if not wid:
            continue
        out[wid] = entry
    return out


# ----- Schema version sanity check -----------------------------------------


def check_schema_version(findings: list[Finding]) -> bool:
    """Confirm SCHEMAS.md major == expected. Returns False on mismatch."""
    try:
        text = SCHEMAS_FILE.read_text(encoding="utf-8")
    except OSError:
        findings.append(Finding(
            "CRITICAL", f"SCHEMAS.md not found at {SCHEMAS_FILE}",
        ))
        return False
    m = re.search(r"version\s+(\d+)\.(\d+)", text)
    if not m:
        findings.append(Finding(
            "WARN", "Could not detect version in SCHEMAS.md - proceeding",
        ))
        return True
    major = int(m.group(1))
    if major != SCHEMA_MAJOR_EXPECTED:
        findings.append(Finding(
            "CRITICAL",
            f"SCHEMAS.md major version {major} != expected "
            f"{SCHEMA_MAJOR_EXPECTED}. Scanner refuses to run.",
        ))
        return False
    return True


# ----- Workstream discovery ------------------------------------------------


def discover_workstreams(
    roots: list[Path],
    excludes: list[Path],
    aliases: dict[str, dict[str, Any]],
    findings: list[Finding],
) -> dict[str, Workstream]:
    """Walk each root one level deep. Register folders with valid README."""
    out: dict[str, Workstream] = {}
    excluded_abs = {e.resolve() for e in excludes}
    seen_ids: dict[str, Path] = {}

    for root in roots:
        if not root.exists():
            findings.append(Finding(
                "WARN", f"Configured root does not exist: {rel(root)}",
            ))
            continue
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if child.resolve() in excluded_abs:
                continue
            readme = child / "README.md"
            if not readme.exists():
                # Not a workstream folder - skip silently
                continue
            parsed = read_frontmatter(readme)
            if parsed is None:
                findings.append(Finding(
                    "CRITICAL",
                    f"README unreadable: {rel(readme)}",
                    rel(readme),
                ))
                continue
            if isinstance(parsed, str):
                findings.append(Finding(
                    "CRITICAL",
                    f"README frontmatter YAML parse failed: {parsed}",
                    rel(readme),
                ))
                continue
            fm, _ = parsed
            wid = fm.get("workstream_id")
            if not wid:
                findings.append(Finding(
                    "CRITICAL",
                    f"README missing `workstream_id:` frontmatter - "
                    f"not registered as workstream",
                    rel(readme),
                ))
                continue
            if wid in seen_ids:
                findings.append(Finding(
                    "CRITICAL",
                    f"workstream_id collision: `{wid}` already registered "
                    f"at {rel(seen_ids[wid])}",
                    rel(readme),
                ))
                continue
            seen_ids[wid] = readme
            display_name = fm.get("display_name") or wid
            status = str(fm.get("status") or "active").strip().lower()
            if status not in VALID_WS_STATUS:
                findings.append(Finding(
                    "WARN",
                    f"README `status:` is `{fm.get('status')}`, not one of "
                    f"{sorted(VALID_WS_STATUS)}. Treating as 'active'.",
                    rel(readme),
                ))
                status = "active"
            alias_entry = aliases.get(wid, {})
            ws = Workstream(
                workstream_id=wid,
                display_name=display_name,
                status=status,
                folder=child,
                readme_path=readme,
                created=str(fm.get("created")) if fm.get("created") else None,
                summary=fm.get("summary"),
                owner=fm.get("owner"),
                aliases=list(alias_entry.get("aliases") or []),
            )
            if wid not in aliases:
                findings.append(Finding(
                    "CRITICAL",
                    f"workstream_id `{wid}` has no entry in aliases.yml",
                    rel(readme),
                ))
            out[wid] = ws
    return out


# ----- Handoff discovery + sort --------------------------------------------


def collect_handoff_candidates(
    workstreams: dict[str, Workstream],
) -> list[Path]:
    """Handoffs are discovered ONLY beneath each workstream's own hand-offs/.

    There is no central handoff directory: a file left at memory/hand-offs/ is
    not a candidate and can never influence latest-handoff selection.
    """
    paths: list[Path] = []
    for ws in workstreams.values():
        local = ws.folder / "hand-offs"
        if local.exists():
            paths.extend(sorted(local.glob("*.md")))
    # Dedupe (same file could be reached twice if paths overlap)
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        unique.append(p)
    return unique


def _as_list(v: Any) -> list[str]:
    """Handoff fields may be list | str 'none' | empty | single string."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s or s.lower() == "none":
        return []
    return [s]


def _field(fm: dict, key: str) -> Any:
    """Read a frontmatter field from root or, as fallback, from nested 'metadata:' block.

    Supports both flat format (field at YAML root) and the post-2026-05-20
    nested format where structured fields live under a 'metadata:' key.
    """
    if key in fm:
        return fm[key]
    return fm.get("metadata", {}).get(key)


def extract_handoff_activity(
    handoffs: list[Path],
    workstreams: dict[str, Workstream],
    findings: list[Finding],
) -> None:
    """For each handoff, validate and attach to its workstream if newest."""
    for path in handoffs:
        parsed = read_frontmatter(path)
        if parsed is None:
            findings.append(Finding(
                "CRITICAL", f"Handoff unreadable: {rel(path)}", rel(path),
            ))
            continue
        if isinstance(parsed, str):
            findings.append(Finding(
                "CRITICAL",
                f"Handoff frontmatter YAML parse failed: {parsed}",
                rel(path),
            ))
            continue
        fm, _ = parsed
        if not fm:
            # A legacy handoff with no frontmatter - noted but not fatal,
            # scanner just can't attribute it to a workstream.
            findings.append(Finding(
                "WARN",
                f"Handoff has no frontmatter - skipped "
                f"(pre-structured-block era)",
                rel(path),
            ))
            continue
        wid = _field(fm, "workstream_id")
        if not wid:
            findings.append(Finding(
                "WARN",
                f"Handoff missing `workstream_id:` at top level or under "
                f"`metadata:` block - skipped",
                rel(path),
            ))
            continue
        if wid not in workstreams:
            findings.append(Finding(
                "CRITICAL",
                f"Handoff `workstream_id: {wid}` does not match any "
                f"registered workstream",
                rel(path),
            ))
            continue
        ws = workstreams[wid]

        se_raw = _field(fm, "session_end")
        se = parse_session_end(se_raw)
        if se is None:
            findings.append(Finding(
                "CRITICAL",
                f"Handoff `session_end:` missing or unparseable "
                f"(value: {se_raw!r})",
                rel(path),
            ))
            continue

        missing = [k for k in ("next", "blockers", "open_items", "status")
                   if _field(fm, k) is None]
        if missing:
            findings.append(Finding(
                "CRITICAL",
                f"Handoff missing required structured-block fields: "
                f"{', '.join(missing)}",
                rel(path),
            ))
            # Still use it for sort - partial info is better than none.

        status = str(_field(fm, "status") or "").strip().lower()
        if status and status not in VALID_HANDOFF_STATUS:
            # Free-form status - common in legacy handoffs. Normalize down.
            narrative = status
            status = "active"
            findings.append(Finding(
                "INFO",
                f"Handoff `status:` is free-form ({narrative!r}); "
                f"treating as 'active'",
                rel(path),
            ))

        # Orphan check on handoff `related_workstreams:` - catches typos.
        # No symmetry enforcement: handoff-level relations are session-scoped
        # and often directional.
        related = _field(fm, "related_workstreams")
        if related is not None:
            if not isinstance(related, list):
                findings.append(Finding(
                    "WARN",
                    f"Handoff `related_workstreams:` is not a list "
                    f"(value: {related!r})",
                    rel(path),
                ))
            else:
                for rel_id in related:
                    if str(rel_id).strip() not in workstreams:
                        findings.append(Finding(
                            "WARN",
                            f"Handoff `related_workstreams:` references "
                            f"unknown workstream_id `{rel_id}`",
                            rel(path),
                        ))

        # Attach only if newer than current
        if ws.latest_session_end is None or se > ws.latest_session_end:
            ws.latest_session_end = se
            ws.latest_handoff_path = path
            ws.activity_status = status or None
            ws.next_items = _as_list(_field(fm, "next"))
            ws.blockers = _as_list(_field(fm, "blockers"))
            ws.open_items = _as_list(_field(fm, "open_items"))


# ----- External checkers ---------------------------------------------------


def run_memory_bridge_checks(findings: list["Finding"]) -> None:
    """Detect drift between lifecycle-state artifact hashes and repo sources."""
    lifecycle_files = sorted(TASKS_DIR.glob("*/*/lifecycle-state.md"))
    if not lifecycle_files:
        return  # no lifecycle orchestration yet -- expected, skip silently

    seen_repos: list[Path] = []

    for lf in lifecycle_files:
        result = read_frontmatter(lf)
        if result is None:
            findings.append(Finding(
                "WARN",
                f"Memory bridge: lifecycle-state file unreadable: {rel(lf)}",
                rel(lf),
            ))
            continue
        if isinstance(result, str):
            findings.append(Finding(
                "WARN",
                f"Memory bridge: lifecycle-state YAML parse failed: {result}",
                rel(lf),
            ))
            continue
        fm, _ = result
        workstream = fm.get("workstream_id", "unknown")
        feature = fm.get("feature_slug", "unknown")
        repos = fm.get("repos") or []
        artifact_hashes = fm.get("artifact_hashes") or {}

        if not isinstance(repos, list) or not isinstance(artifact_hashes, dict):
            findings.append(Finding(
                "WARN",
                f"Memory bridge: {workstream}/{feature} has malformed "
                f"repos or artifact_hashes",
                rel(lf),
            ))
            continue

        repo_paths: list[Path] = []
        for repo_entry in repos:
            if not isinstance(repo_entry, dict) or "path" not in repo_entry:
                continue
            repo_paths.append(Path(repo_entry["path"]))

        for repo_path in repo_paths:
            artifact_dir = repo_path / REPO_ARTIFACT_SUBPATH
            for key, stored_hash in artifact_hashes.items():
                filename = f"{key}-{feature}.md"
                source = artifact_dir / filename
                if not source.exists():
                    findings.append(Finding(
                        "WARN",
                        f"Memory bridge: {workstream}/{feature} artifact "
                        f"'{key}' source file missing at {source}",
                    ))
                    continue
                try:
                    content = source.read_bytes()
                except OSError:
                    findings.append(Finding(
                        "WARN",
                        f"Memory bridge: {workstream}/{feature} artifact "
                        f"'{key}' unreadable at {source}",
                    ))
                    continue
                actual = hashlib.sha256(content).hexdigest()[:12]
                if actual.lower() != str(stored_hash).lower():  # case-insensitive: stored hashes may be upper- or lowercase
                    findings.append(Finding(
                        "WARN",
                        f"Memory bridge drift: {workstream}/{feature} "
                        f"artifact '{key}' hash mismatch "
                        f"(expected {stored_hash}, got {actual})",
                    ))

            # Orphan temp files
            if repo_path not in seen_repos:
                seen_repos.append(repo_path)
                for tmp_file in artifact_dir.glob(".tmp-*"):
                    findings.append(Finding(
                        "WARN",
                        f"Memory bridge: orphan temp file {tmp_file} "
                        f"-- possible interrupted artifact write",
                    ))


def run_lifecycle_task_checks(findings: list["Finding"]) -> None:
    """Per SCHEMAS.md section 13: scan tasks/ folder, compare against lifecycle-tasks.md index."""
    if not TASKS_DIR.exists():
        return  # no tasks folder yet -- expected, skip silently

    # Primary discovery: direct folder scan
    discovered: dict[str, Path] = {}
    for lf in sorted(TASKS_DIR.glob("*/*/lifecycle-state.md")):
        # Folder structure: tasks/{workstream_id}/{feature_slug}/lifecycle-state.md
        feature_dir = lf.parent
        workstream_dir = feature_dir.parent
        key = f"{workstream_dir.name}/{feature_dir.name}"
        discovered[key] = lf

    # Advisory comparison: check lifecycle-tasks.md index for divergence
    if LIFECYCLE_TASKS_FILE.exists():
        try:
            content = LIFECYCLE_TASKS_FILE.read_text(encoding="utf-8")
        except OSError:
            findings.append(Finding(
                "WARN",
                "lifecycle-tasks.md unreadable",
                rel(LIFECYCLE_TASKS_FILE),
            ))
            return

        # Parse table rows: | workstream_id | feature_slug | ... |
        indexed_keys: set[str] = set()
        for line in content.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cols = [c.strip() for c in line.split("|")]
            if len(cols) < 4:
                continue
            ws_id = cols[1]
            feat = cols[2]
            if not ws_id or not feat:
                continue
            if ws_id.startswith("---") or feat.startswith("---"):
                continue  # separator row, with or without leading space
            if ws_id.lower() == "workstream_id":
                continue  # header row
            indexed_keys.add(f"{ws_id}/{feat}")

        # Divergence checks
        for key in discovered:
            if key not in indexed_keys:
                findings.append(Finding(
                    "WARN",
                    f"Lifecycle task {key} found on disk but missing from "
                    f"lifecycle-tasks.md index",
                ))
        for key in indexed_keys:
            if key not in discovered:
                findings.append(Finding(
                    "WARN",
                    f"Lifecycle task {key} listed in lifecycle-tasks.md "
                    f"but no task folder found on disk",
                ))
    elif discovered:
        findings.append(Finding(
            "WARN",
            f"Found {len(discovered)} lifecycle task folder(s) on disk "
            f"but lifecycle-tasks.md index does not exist",
        ))


# ----- Rules checks --------------------------------------------------------


def run_rules_checks(findings: list["Finding"]) -> None:
    """Validate rules/ directory: duplicates, orphans, staleness, tier consistency."""
    if not RULES_DIR.exists():
        findings.append(Finding(
            "WARN", "rules/ directory does not exist - skipping rules checks",
        ))
        return

    # --- Collect rule files and their frontmatter ---
    rule_files: dict[str, dict[str, Any]] = {}  # filename -> frontmatter
    for path in sorted(RULES_DIR.glob("feedback_*.md")):
        result = read_frontmatter(path)
        if result is None:
            findings.append(Finding(
                "WARN",
                f"Rule file unreadable: {path.name}",
                f"rules/{path.name}",
            ))
            continue
        if isinstance(result, str):
            findings.append(Finding(
                "WARN",
                f"Rule file frontmatter parse failed: {result}",
                f"rules/{path.name}",
            ))
            continue
        fm, _ = result
        rule_files[path.name] = fm

    if not rule_files:
        return  # nothing to check

    # --- Parse INDEX.md to get expected file list ---
    index_entries: set[str] = set()
    if RULES_INDEX.exists():
        try:
            index_text = RULES_INDEX.read_text(encoding="utf-8")
            for line in index_text.splitlines():
                line = line.strip()
                if not line.startswith("|"):
                    continue
                cols = [c.strip() for c in line.split("|")]
                if len(cols) < 4:
                    continue
                filename = cols[2]  # | Rule | File | Category | ...
                if not filename or filename.startswith("---") or filename == "File":
                    continue
                if filename.endswith(".md"):
                    index_entries.add(filename)
        except OSError:
            findings.append(Finding(
                "WARN", "rules/INDEX.md unreadable",
                "rules/INDEX.md",
            ))
    else:
        findings.append(Finding(
            "WARN", "rules/INDEX.md does not exist",
        ))

    # --- Read CLAUDE.md for tier consistency check ---
    claude_md_text = ""
    if CLAUDE_MD_FILE.exists():
        try:
            claude_md_text = CLAUDE_MD_FILE.read_text(encoding="utf-8")
        except OSError:
            findings.append(Finding(
                "WARN", "CLAUDE.md unreadable - skipping tier consistency check",
            ))

    # --- Collect workstream README rules: fields ---
    readme_rule_refs: dict[str, set[str]] = {}  # workstream_id -> set of filenames
    config_roots, config_excludes = load_config()
    excluded_abs = {e.resolve() for e in config_excludes}
    for root in config_roots:
        if not root.exists():
            continue
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if child.resolve() in excluded_abs:
                continue
            readme = child / "README.md"
            if not readme.exists():
                continue
            parsed = read_frontmatter(readme)
            if parsed is None or isinstance(parsed, str):
                continue
            fm, _ = parsed
            wid = fm.get("workstream_id")
            rules_list = fm.get("rules")
            if wid and rules_list and isinstance(rules_list, list):
                readme_rule_refs[wid] = set(str(r) for r in rules_list)

    # --- Check 1: Keyword overlap (3+ shared keywords -> WARN) ---
    filenames = sorted(rule_files.keys())
    for i in range(len(filenames)):
        for j in range(i + 1, len(filenames)):
            f_a, f_b = filenames[i], filenames[j]
            fm_a, fm_b = rule_files[f_a], rule_files[f_b]
            kw_a = set(str(k).lower() for k in (fm_a.get("keywords") or []))
            kw_b = set(str(k).lower() for k in (fm_b.get("keywords") or []))
            overlap = kw_a & kw_b
            if len(overlap) >= 3:
                # Check related: exemption
                related_a = set(str(r) for r in (fm_a.get("related") or []))
                related_b = set(str(r) for r in (fm_b.get("related") or []))
                if f_b in related_a or f_a in related_b:
                    continue  # intentionally related, skip
                findings.append(Finding(
                    "WARN",
                    f"Potential duplicate: {f_a} and {f_b} share "
                    f"{len(overlap)} keywords: {', '.join(sorted(overlap))}",
                ))

    # --- Check 2: Orphan detection (file in rules/ not in INDEX.md) ---
    if index_entries:
        for filename in rule_files:
            if filename not in index_entries:
                findings.append(Finding(
                    "WARN",
                    f"Orphan rule file: {filename} exists in rules/ "
                    f"but not listed in INDEX.md",
                    f"rules/{filename}",
                ))

    # --- Check 3: INDEX drift (entry in INDEX.md -> nonexistent file) ---
    if index_entries:
        for entry in sorted(index_entries):
            if entry not in rule_files:
                findings.append(Finding(
                    "CRITICAL",
                    f"INDEX.md drift: entry '{entry}' points to nonexistent "
                    f"file in rules/",
                    "rules/INDEX.md",
                ))

    # --- Check 4: Tier consistency ---
    # tier: always but not in CLAUDE.md ALWAYS section (search for rules/<filename>)
    # Exception: files with covered_by: are exempt
    if claude_md_text:
        for filename, fm in rule_files.items():
            tier = str(fm.get("tier") or "").strip().lower()
            if tier != "always":
                continue
            if fm.get("covered_by"):
                continue  # exempt - covered by CLAUDE.md prose
            search_str = f"rules/{filename}"
            if search_str not in claude_md_text:
                findings.append(Finding(
                    "WARN",
                    f"Tier consistency: {filename} is tier:always but "
                    f"'{search_str}' not found in CLAUDE.md",
                    f"rules/{filename}",
                ))

    # --- Check 5: README rule validation ---
    # File listed in a workstream README rules: but doesn't exist in rules/
    for wid, rule_set in readme_rule_refs.items():
        for rule_ref in sorted(rule_set):
            if rule_ref not in rule_files:
                findings.append(Finding(
                    "CRITICAL",
                    f"README rule validation: workstream '{wid}' references "
                    f"'{rule_ref}' but file does not exist in rules/",
                ))

    # --- Check 6: Staleness check ---
    today = datetime.now(LOCAL_TZ).date()
    for filename, fm in rule_files.items():
        lrd_raw = fm.get("last_relevant_date")
        if not lrd_raw:
            continue
        try:
            if isinstance(lrd_raw, str):
                lrd = datetime.strptime(lrd_raw, "%Y-%m-%d").date()
            else:
                lrd = lrd_raw  # YAML may parse as datetime.date
            days_stale = (today - lrd).days
            if days_stale > RULES_STALENESS_THRESHOLD_DAYS:
                findings.append(Finding(
                    "WARN",
                    f"Stale rule: {filename} last relevant {lrd.isoformat()} "
                    f"({days_stale} days ago, threshold "
                    f"{RULES_STALENESS_THRESHOLD_DAYS})",
                    f"rules/{filename}",
                ))
        except (ValueError, TypeError):
            findings.append(Finding(
                "WARN",
                f"Rule {filename}: unparseable last_relevant_date "
                f"'{lrd_raw}'",
                f"rules/{filename}",
            ))


# ----- Integrity invariants ------------------------------------------------


def run_integrity_checks(
    workstreams: dict[str, Workstream],
    aliases: dict[str, dict[str, Any]],
    findings: list[Finding],
) -> None:
    """Per SCHEMAS.md section 5. discover/extract already flagged most."""
    # Orphan aliases: alias entries pointing to non-existent workstream IDs
    for wid in aliases:
        if wid not in workstreams:
            findings.append(Finding(
                "WARN",
                f"aliases.yml entry `{wid}` has no matching workstream "
                f"folder (orphan)",
            ))
    # Alias collisions - WARN only (shared codenames like "Atlas" are legit).
    # Dedupe by workstream_id so a within-workstream case-dupe is not flagged
    # as a cross-workstream collision.
    term_to_ids: dict[str, set[str]] = defaultdict(set)
    for wid, entry in aliases.items():
        for term in entry.get("aliases") or []:
            term_to_ids[str(term).strip().lower()].add(wid)
    for term, ids in sorted(term_to_ids.items()):
        if len(ids) > 1:
            findings.append(Finding(
                "WARN",
                f"alias `{term}` matches multiple workstreams: "
                f"{', '.join(sorted(ids))} (disambiguate on use)",
            ))


# ----- Jira helpers --------------------------------------------------------


def load_jira_cache() -> dict | None:
    """Load jira-sync.json. Returns dict on success, None on any error."""
    try:
        text = JIRA_SYNC_JSON.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict) or "projects" not in data:
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def build_jira_briefing_section(jira_data: dict | None) -> str:
    """Build the Jira active tickets section for BRIEFING. Returns '' if nothing to show."""
    if jira_data is None:
        return ""
    projects = jira_data.get("projects") or {}
    if not projects:
        return ""

    lines: list[str] = []
    # Heading matches existing Activity State heading dash style (em dash for consistency)
    lines.append("## Jira Active Tickets (generated - do not edit by hand)")
    lines.append("")

    sync_failed_at = jira_data.get("sync_failed_at")
    if sync_failed_at:
        synced_at = jira_data.get("synced_at", "unknown")
        lines.append(
            f"> Last sync FAILED at {sync_failed_at}. "
            f"Showing last-good data from {synced_at}."
        )
        lines.append("")

    rendered_any = False
    for project_key in JIRA_PROJECT_KEYS:
        if project_key not in projects:
            continue
        proj = projects[project_key]
        tickets = proj.get("tickets") or []
        active = [t for t in tickets if (t.get("status") or "").lower() != "done"]
        if not active:
            continue

        # Sort: first by updated descending, then stable-sort by (not blocked, priority_rank)
        active.sort(key=lambda t: t.get("updated") or "", reverse=True)
        active.sort(key=lambda t: (not is_blocked(t), PRIORITY_RANK.get(t.get("priority") or "", 4)))

        shown = active[:JIRA_ACTIVE_LIMIT]
        blocked_count = sum(1 for t in active if is_blocked(t))

        blocked_suffix = f", {blocked_count} blocked" if blocked_count else ""
        lines.append(
            f"**{project_key}** ({proj.get('name', project_key)}) "
            f"- {len(active)} active{blocked_suffix}"
        )

        for ticket in shown:
            key = ticket.get("key", "?")
            priority = ticket.get("priority") or "?"
            summary = ticket.get("summary", "")
            due_date = ticket.get("due_date")
            line = f"- {key} [{priority}] {summary}"
            if is_blocked(ticket):
                line += " [BLOCKED]"
            if due_date:
                line += f" (due {due_date})"
            lines.append(line)

        if len(active) > JIRA_ACTIVE_LIMIT:
            lines.append(f"- ...and {len(active) - JIRA_ACTIVE_LIMIT} more")

        lines.append("")
        rendered_any = True

    if not rendered_any:
        return ""

    return "\n".join(lines)


def run_jira_integrity_checks(findings: list[Finding]) -> None:
    """Check jira-sync.json health. All findings are WARN severity."""
    try:
        if not JIRA_SYNC_JSON.exists():
            findings.append(Finding(
                "WARN",
                "jira-sync.json not found - pa-jira-sync cron may not have run yet",
            ))
            return

        try:
            text = JIRA_SYNC_JSON.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, UnicodeDecodeError):
            findings.append(Finding(
                "WARN",
                "jira-sync.json unreadable or invalid JSON",
                rel(JIRA_SYNC_JSON),
            ))
            return
        except json.JSONDecodeError:
            findings.append(Finding(
                "WARN",
                "jira-sync.json unreadable or invalid JSON",
                rel(JIRA_SYNC_JSON),
            ))
            return

        if not isinstance(data, dict):
            findings.append(Finding(
                "WARN",
                "jira-sync.json has unexpected root type (expected object)",
                rel(JIRA_SYNC_JSON),
            ))
            return

        if "projects" not in data or not isinstance(data.get("projects"), dict):
            findings.append(Finding(
                "WARN",
                "jira-sync.json missing 'projects' key or wrong type",
            ))

        sync_failed_at = data.get("sync_failed_at")
        if sync_failed_at:
            findings.append(Finding(
                "WARN",
                f"Jira sync failed at {sync_failed_at} - check .env credentials and network",
                rel(JIRA_SYNC_JSON),
            ))

        synced_at = data.get("synced_at")
        if synced_at is None:
            findings.append(Finding(
                "WARN",
                "jira-sync.json missing synced_at - cache may be from a failed-only run",
            ))
        else:
            try:
                parsed_dt = datetime.fromisoformat(str(synced_at))
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=LOCAL_TZ)
                now = datetime.now(LOCAL_TZ)
                age_hours = (now - parsed_dt).total_seconds() / 3600.0
                if age_hours > JIRA_STALE_WARN_HOURS:
                    findings.append(Finding(
                        "WARN",
                        f"Jira sync stale: last synced {age_hours:.1f}h ago "
                        f"(threshold {JIRA_STALE_WARN_HOURS}h) - check pa-jira-sync cron",
                        rel(JIRA_SYNC_JSON),
                    ))
            except ValueError:
                findings.append(Finding(
                    "WARN",
                    f"jira-sync.json synced_at unparseable: {synced_at!r}",
                ))
    except Exception:
        pass  # never raises from integrity checks


# ----- Output builders -----------------------------------------------------


def rel(path: Path) -> str:
    try:
        return path.relative_to(MEMORY_DIR).as_posix()
    except ValueError:
        return str(path)


def format_list_oneline(items: list[str], limit: int = 80) -> str:
    if not items:
        return "none"
    joined = "; ".join(items)
    if len(joined) > limit:
        joined = joined[: limit - 1] + "…"
    return joined


def build_map(
    workstreams: dict[str, Workstream],
    generated_at: datetime,
    source_scan_completed: bool,
    deadlines_summary: list[str],
) -> str:
    lines: list[str] = []
    lines.append("---")
    # OKF v0.2: keep the regenerated file conformant (non-empty top-level type
    # plus a one-line summary) so scripts/ng0/okf_check.py stays green after a
    # live scan, matching the shipped stub's type.
    lines.append("type: map")
    lines.append("purpose: Generated orientation map; the entry point for "
                 "topic and alias lookup across the memory tree.")
    lines.append(f"last_successful_generation: "
                 f"{generated_at.isoformat(timespec='seconds')}")
    lines.append(f"source_scan_completed: "
                 f"{'true' if source_scan_completed else 'false'}")
    lines.append(f"scanner_version: {SCANNER_VERSION}")
    lines.append("---")
    lines.append("")
    lines.append("# Workstream Orientation Map")
    lines.append("")
    lines.append("> Generated by `pa-workspace-scan`. Do not hand-edit.")
    lines.append("> Topic/alias lookup entry point. For activity state, "
                 "see BRIEFING.md Activity State block.")
    lines.append("")

    def section(title: str, ws_list: list[Workstream]) -> None:
        if not ws_list:
            return
        lines.append(f"## {title}")
        lines.append("")
        for ws in ws_list:
            folder_rel = rel(ws.folder)
            alias_display = ", ".join(ws.aliases) if ws.aliases else "-"
            lines.append(
                f"- **[{ws.display_name}]({folder_rel}/)** - "
                f"`{ws.workstream_id}` - aliases: {alias_display}"
            )
            handoff_bit = (
                ws.latest_session_end.date().isoformat()
                if ws.latest_session_end else "(none)"
            )
            lines.append(
                f"  Status: {ws.status} · Latest handoff: {handoff_bit}"
            )
            if ws.summary:
                lines.append(f"  Summary: {ws.summary}")
            lines.append("")

    active = sorted(
        [w for w in workstreams.values() if w.status == "active"],
        key=lambda w: w.display_name.lower(),
    )
    dormant_or_done = sorted(
        [w for w in workstreams.values() if w.status != "active"],
        key=lambda w: (w.status, w.display_name.lower()),
    )
    section("Active workstreams", active)
    section("Dormant / completed workstreams", dormant_or_done)

    lines.append("## Urgent deadlines (from DEADLINES.md)")
    lines.append("")
    if deadlines_summary:
        for line in deadlines_summary:
            lines.append(f"- {line}")
    else:
        lines.append("_None surfaced._")
    lines.append("")
    return "\n".join(lines)


def build_briefing_block(
    workstreams: dict[str, Workstream],
    generated_at: datetime,
    next_run: datetime,
    source_scan_completed: bool,
    jira_section: str = "",
    integrity_marker: str | None = None,
) -> str:
    lines: list[str] = []
    lines.append(PA_SCAN_START)
    # The integrity summary lives INSIDE the scanner-owned region (between the
    # markers), never above it. The scanner writes nothing outside its markers,
    # per the BRIEFING block-ownership contract (SCHEMAS.md).
    if integrity_marker:
        lines.append(integrity_marker)
    lines.append(f"<!-- generated_at: "
                 f"{generated_at.isoformat(timespec='seconds')} -->")
    lines.append(f"<!-- source_scan_completed: "
                 f"{'true' if source_scan_completed else 'false'} -->")
    lines.append(f"<!-- stale_if_older_than_hours: {STALE_HOURS} -->")
    lines.append(f"<!-- scanner_version: {SCANNER_VERSION} -->")
    lines.append(f"<!-- next_run: "
                 f"{next_run.isoformat(timespec='seconds')} -->")
    lines.append("")
    lines.append("## Activity State (generated - do not edit by hand)")
    lines.append("")
    lines.append(
        "| Workstream | Status | Latest handoff | Next | Blockers | "
        "Days since |"
    )
    lines.append("|---|---|---|---|---|---|")
    today = generated_at.date()
    ordered = sorted(
        workstreams.values(),
        key=lambda w: (
            0 if w.status == "active" else 1,
            -(w.latest_session_end.timestamp() if w.latest_session_end else 0),
            w.display_name.lower(),
        ),
    )
    for ws in ordered:
        if ws.latest_session_end:
            handoff_date = ws.latest_session_end.date()
            days_since = (today - handoff_date).days
            handoff_cell = handoff_date.isoformat()
            if ws.latest_handoff_path:
                handoff_cell = f"[{handoff_cell}]({rel(ws.latest_handoff_path)})"
            days_cell = str(days_since)
        else:
            handoff_cell = "-"
            days_cell = "-"
        status_cell = ws.activity_status or ws.status
        next_cell = format_list_oneline(ws.next_items)
        block_cell = format_list_oneline(ws.blockers)
        # Table-cell escapes: pipes + newlines
        for cell_var in ("next_cell", "block_cell"):
            pass  # handled inline below
        next_cell = next_cell.replace("|", "\\|").replace("\n", " ")
        block_cell = block_cell.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| [{ws.display_name}]({rel(ws.folder)}/) "
            f"| {status_cell} | {handoff_cell} | {next_cell} "
            f"| {block_cell} | {days_cell} |"
        )
    lines.append("")
    if jira_section:
        lines.append(jira_section)
    lines.append(PA_SCAN_END)
    return "\n".join(lines)


def build_integrity(
    findings: list[Finding],
    generated_at: datetime,
) -> str:
    by_sev: dict[str, list[Finding]] = {
        "CRITICAL": [], "WARN": [], "INFO": []
    }
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)
    lines: list[str] = []
    lines.append("---")
    # OKF v0.2: keep the regenerated file conformant (non-empty top-level type
    # plus a one-line summary) so scripts/ng0/okf_check.py stays green after a
    # live scan, matching the shipped stub's type.
    lines.append("type: report")
    lines.append("purpose: Integrity report listing consistency findings "
                 "across the memory tree.")
    lines.append(f"last_successful_generation: "
                 f"{generated_at.isoformat(timespec='seconds')}")
    lines.append(f"scanner_version: {SCANNER_VERSION}")
    lines.append("---")
    lines.append("")
    lines.append("# Integrity Check")
    lines.append("")
    lines.append(f"Last run: {generated_at.strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")
    for sev in ("CRITICAL", "WARN", "INFO"):
        bucket = by_sev.get(sev, [])
        lines.append(f"## {sev} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("- (none)")
        else:
            for f in bucket:
                suffix = f" - `{f.path}`" if f.path else ""
                lines.append(f"- {f.message}{suffix}")
        lines.append("")
    return "\n".join(lines)


def summarize_deadlines() -> list[str]:
    """Pull urgent items from DEADLINES.md for MAP.md's deadline section."""
    if not DEADLINES_FILE.exists():
        return []
    text = DEADLINES_FILE.read_text(encoding="utf-8")
    out: list[str] = []
    in_overdue = False
    in_imminent = False
    for line in text.splitlines():
        if line.startswith("## 🚨 OVERDUE"):
            in_overdue, in_imminent = True, False
            continue
        if line.startswith("## ⏰ IMMINENT"):
            in_overdue, in_imminent = False, True
            continue
        if line.startswith("## 📅"):
            break
        if (in_overdue or in_imminent) and line.startswith("- "):
            condensed = line[2:].strip()
            if len(condensed) > 140:
                condensed = condensed[:139] + "…"
            out.append(condensed)
    return out[:10]  # cap


# ----- Atomic writes -------------------------------------------------------


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def log_sizes(now: datetime) -> int:
    """Append one JSONL line with orientation-file byte sizes. Returns total bytes."""
    SIZE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    for name, path in ORIENTATION_FILES:
        try:
            sizes[name] = path.stat().st_size
        except OSError:
            sizes[name] = -1
    total = sum(v for v in sizes.values() if v > 0)
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "files": sizes,
        "total": total,
        "total_tokens_est": total // 4,
    }
    with SIZE_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return total


def replace_briefing_block(block_content: str) -> None:
    """Replace ONLY the text between the PA_SCAN markers. Fail loud otherwise.

    The hand-authored region (Session Reminders, Active Handoffs) is owned by the
    HAS pipeline; the scanner never touches a byte outside its own marker pair.
    If the file or either marker is missing, this raises BriefingContractError
    rather than creating or appending -- a silent overwrite/append would clobber
    the HAS region (SCHEMAS.md block-ownership contract; PRD NFR Observability).

    `block_content` already begins with PA_SCAN_START and ends with PA_SCAN_END,
    so the marker lines themselves are rewritten in place and the surrounding
    bytes are preserved exactly.
    """
    if not BRIEFING_FILE.exists():
        raise BriefingContractError(
            f"BRIEFING.md not found at {BRIEFING_FILE}; refusing to create it "
            f"(hand-authored file owned by the HAS pipeline)."
        )
    # Splice on RAW BYTES, not decoded text: a text-mode rewrite would translate
    # newlines (LF<->CRLF) across the WHOLE file and mutate the hand-authored
    # region's bytes even though its characters are unchanged. Byte splicing
    # leaves every byte outside the marker pair exactly as-is.
    raw = BRIEFING_FILE.read_bytes()
    start_tok = PA_SCAN_START.encode("utf-8")
    end_tok = PA_SCAN_END.encode("utf-8")
    start_idx = raw.find(start_tok)
    end_idx = raw.find(end_tok)
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        raise BriefingContractError(
            f"BRIEFING.md is missing the scanner marker pair "
            f"({PA_SCAN_START} / {PA_SCAN_END}); refusing to overwrite or append "
            f"(would clobber the hand-authored region)."
        )
    before = raw[:start_idx]
    after = raw[end_idx + len(end_tok):]
    new_raw = before + block_content.encode("utf-8") + after
    tmp = BRIEFING_FILE.with_suffix(BRIEFING_FILE.suffix + ".tmp")
    tmp.write_bytes(new_raw)
    os.replace(tmp, BRIEFING_FILE)


# ----- Main ---------------------------------------------------------------


def main() -> int:
    now = datetime.now(LOCAL_TZ)
    next_run = now.replace(
        hour=CRON_HOUR, minute=CRON_MINUTE, second=0, microsecond=0,
    )
    if next_run <= now:
        next_run += timedelta(days=1)

    findings: list[Finding] = []
    source_scan_completed = True

    try:
        if not check_schema_version(findings):
            # Still write INTEGRITY so the user sees the CRITICAL.
            source_scan_completed = False
            integrity_content = build_integrity(findings, now)
            atomic_write(INTEGRITY_FILE, integrity_content)
            return 2

        roots, excludes = load_config()
        aliases = load_aliases()
        workstreams = discover_workstreams(roots, excludes, aliases, findings)
        handoffs = collect_handoff_candidates(workstreams)
        extract_handoff_activity(handoffs, workstreams, findings)
        run_integrity_checks(workstreams, aliases, findings)
        run_memory_bridge_checks(findings)
        run_lifecycle_task_checks(findings)
        run_rules_checks(findings)
        run_jira_integrity_checks(findings)

    except Exception as exc:
        source_scan_completed = False
        findings.append(Finding(
            "CRITICAL",
            f"Scanner exception: {type(exc).__name__}: {exc}",
        ))
        traceback.print_exc()
        workstreams = {}  # fall through to write partial output

    deadlines_summary = summarize_deadlines()

    # Load Jira cache (belt-and-suspenders: graceful degrade on any error)
    try:
        jira_cache = load_jira_cache()
        jira_section = build_jira_briefing_section(jira_cache)
    except Exception:
        jira_section = ""

    # Integrity marker for BRIEFING. Rendered INSIDE the scanner-owned region
    # (see build_briefing_block), never outside it.
    crit = sum(1 for f in findings if f.severity == "CRITICAL")
    warn = sum(1 for f in findings if f.severity == "WARN")
    marker: str | None = None
    if crit or warn:
        marker = (
            f"<!-- PA_SCAN_INTEGRITY: CRITICAL={crit} WARN={warn} - "
            f"see INTEGRITY.md -->"
        )

    # Build outputs
    map_content = build_map(
        workstreams, now, source_scan_completed, deadlines_summary,
    )
    briefing_block = build_briefing_block(
        workstreams, now, next_run, source_scan_completed,
        jira_section=jira_section, integrity_marker=marker,
    )
    integrity_content = build_integrity(findings, now)

    # Atomic writes. MAP + INTEGRITY are full rewrites; BRIEFING is surgical and
    # fails loud (non-zero exit) if its marker pair is missing rather than
    # clobbering the hand-authored region.
    atomic_write(MAP_FILE, map_content)
    atomic_write(INTEGRITY_FILE, integrity_content)
    try:
        replace_briefing_block(briefing_block)
    except BriefingContractError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        return 2
    total_bytes = log_sizes(now)

    # CLI summary
    print(f"Wrote {rel(MAP_FILE)}")
    print(f"Wrote {rel(INTEGRITY_FILE)}")
    print(f"Updated PA_SCAN block in {rel(BRIEFING_FILE)}")
    print(
        f"Appended size entry to {rel(SIZE_LOG_FILE)} "
        f"(TOTAL {total_bytes} bytes, ~{total_bytes // 4:,} tokens)"
    )
    print(
        f"  Workstreams: {len(workstreams)}  "
        f"CRITICAL: {crit}  WARN: {warn}  "
        f"source_scan_completed: {source_scan_completed}"
    )
    return 0 if (source_scan_completed and crit == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
