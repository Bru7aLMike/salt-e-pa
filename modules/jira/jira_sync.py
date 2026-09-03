#!/usr/bin/env python3
"""Optional Jira sync for the Salt-e PA scaffold.

DISABLED by default. Every code path below is gated behind
`jira_config.is_enabled()`; with the flag off the script prints a notice and
exits 0 without reading a credential or opening a socket.

When enabled, it pulls Jira state via the Atlassian Cloud REST API for any
instance and any set of project keys, then writes into the module data dir:
  - jira-sync.json      -- structured cache
  - jira-summary.md     -- bounded human-readable markdown
  - jira-sync-log.jsonl -- one JSON line appended per run

Authentication is HTTP Basic with PA_JIRA_EMAIL + PA_JIRA_API_TOKEN read from
the environment only. The token is never printed, logged, or written anywhere.

Run manually:  python modules/jira/jira_sync.py
Run from cron: same, no args (see cron.template).
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import jira_config

# ----- Fetch shape ---------------------------------------------------------

FIELDS = "key,summary,status,priority,labels,duedate,parent,updated"
PAGE_SIZE = 100
ACTIVE_TICKET_LIMIT = 15  # max active tickets shown per project in summary
DONE_STATUS = "Done"


# ----- HTTP helpers --------------------------------------------------------


def make_auth_header(email: str, api_token: str) -> str:
    """Return an HTTP Basic auth header value. Secret stays local to this call."""
    creds = f"{email}:{api_token}"
    encoded = base64.b64encode(creds.encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def api_post(url: str, auth_header: str, payload: dict) -> dict:
    """POST a Jira REST endpoint with a JSON body. Returns parsed JSON."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


# ----- Jira field extraction -----------------------------------------------


def extract_status(issue: dict) -> str:
    try:
        return issue["fields"]["status"]["name"]
    except (KeyError, TypeError):
        return "Unknown"


def extract_priority(issue: dict) -> str | None:
    try:
        return issue["fields"]["priority"]["name"]
    except (KeyError, TypeError):
        return None


def extract_epic(issue: dict) -> str | None:
    """Best-effort epic resolution.

    Team-managed projects: the parent field holds the epic when the issue is a
    child of one. Company-managed: an Epic Link custom field. Try the parent
    summary/key first, then fall back to common Epic Link custom fields.
    """
    fields = issue.get("fields") or {}

    parent = fields.get("parent")
    if parent:
        parent_fields = parent.get("fields") or {}
        return parent_fields.get("summary") or parent.get("key")

    for cf_key in ("customfield_10014", "customfield_10008"):
        val = fields.get(cf_key)
        if val:
            return str(val)

    return None


def extract_ticket(issue: dict, tz) -> dict:
    """Extract a structured ticket dict from a raw Jira issue."""
    fields = issue.get("fields") or {}
    labels = fields.get("labels") or []
    due_raw = fields.get("duedate")  # "YYYY-MM-DD" or null
    updated_raw = fields.get("updated") or ""
    updated = updated_raw
    if updated_raw:
        try:
            # Jira returns ISO 8601 with an offset, e.g. 2026-05-25T14:30:00.000+0000.
            # Normalize Z and bare +HHMM/-HHMM offsets for Python < 3.11 compatibility.
            normalized = updated_raw.replace("Z", "+00:00")
            normalized = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", normalized)
            dt = datetime.fromisoformat(normalized)
            updated = dt.astimezone(tz).isoformat(timespec="seconds")
        except ValueError:
            pass

    return {
        "key": issue.get("key", ""),
        "summary": fields.get("summary", ""),
        "status": extract_status(issue),
        "priority": extract_priority(issue),
        "labels": labels,
        "due_date": due_raw,
        "epic": extract_epic(issue),
        "updated": updated,
    }


# ----- Pagination ----------------------------------------------------------


def build_jql(projects: dict) -> str:
    """Derive a JQL project filter from the configured project keys."""
    keys = ", ".join(projects.keys())
    return f"project in ({keys}) ORDER BY updated DESC"


def fetch_all_issues(base_url: str, auth_header: str, projects: dict) -> list[dict]:
    """Fetch all matching issues via cursor pagination (POST /search/jql).

    The current Atlassian Cloud API uses nextPageToken cursor pagination; the
    legacy GET /rest/api/3/search returns 410 Gone.
    """
    issues: list[dict] = []
    endpoint = f"{base_url}/rest/api/3/search/jql"
    next_token: str | None = None
    jql = build_jql(projects)

    while True:
        payload: dict = {
            "jql": jql,
            "fields": FIELDS.split(","),
            "maxResults": PAGE_SIZE,
        }
        if next_token:
            payload["nextPageToken"] = next_token

        data = api_post(endpoint, auth_header, payload)

        batch = data.get("issues") or []
        issues.extend(batch)

        is_last = data.get("isLast", False)
        next_token = data.get("nextPageToken")

        if is_last or not batch or not next_token:
            break

    return issues


# ----- Cache read/write ----------------------------------------------------


def read_existing_cache(sync_json: Path) -> dict | None:
    if not sync_json.exists():
        return None
    try:
        return json.loads(sync_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_project_block(project_name: str, tickets: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    for t in tickets:
        status = t.get("status", "Unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "name": project_name,
        "total": len(tickets),
        "by_status": by_status,
        "tickets": tickets,
    }


def write_cache(sync_json: Path, now: datetime, projects_data: dict) -> None:
    cache = {
        "synced_at": now.isoformat(timespec="seconds"),
        "projects": projects_data,
    }
    tmp = sync_json.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, sync_json)


def write_failed_cache(sync_json: Path, now: datetime, existing: dict | None) -> None:
    """On failure, retain the last-good cache and add sync_failed_at. Never destroys good data."""
    if existing is None:
        failure_cache = {
            "sync_failed_at": now.isoformat(timespec="seconds"),
            "projects": {},
        }
    else:
        failure_cache = dict(existing)
        failure_cache["sync_failed_at"] = now.isoformat(timespec="seconds")
    tmp = sync_json.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(failure_cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, sync_json)


# ----- Summary markdown ----------------------------------------------------


def build_summary(now: datetime, tz_label: str, projects_data: dict) -> str:
    """Build bounded summary markdown."""
    lines: list[str] = []
    lines.append("<!-- Generated by modules/jira/jira_sync.py - do not hand-edit. -->")
    lines.append(f"# Jira Sync Summary - {now.strftime('%Y-%m-%d %H:%M')} {tz_label}")
    lines.append("")

    for project_key, proj in projects_data.items():
        proj_name = proj.get("name", project_key)
        by_status = proj.get("by_status") or {}
        tickets = proj.get("tickets") or []

        status_parts = [f"{count} {status}" for status, count in sorted(by_status.items())]
        status_str = " / ".join(status_parts) if status_parts else "0 tickets"
        lines.append(f"**{project_key}** ({proj_name}): {status_str}")

        active = [t for t in tickets if t.get("status") != DONE_STATUS]
        shown = active[:ACTIVE_TICKET_LIMIT]
        overflow = len(active) - len(shown)

        for t in shown:
            key = t.get("key", "")
            summary = t.get("summary", "")
            status = t.get("status", "")
            priority = t.get("priority") or "?"
            labels = t.get("labels") or []
            due_date = t.get("due_date")

            line = f"- {key} [{status}, {priority}] {summary}"
            if "blocked" in [str(l).lower() for l in labels]:
                line += " (blocked)"
            if due_date:
                line += f" (due {due_date})"
            lines.append(line)

        if overflow > 0:
            lines.append(f"- ...and {overflow} more (see board)")

        lines.append("")

    return "\n".join(lines)


def write_summary(summary_path: Path, content: str) -> None:
    tmp = summary_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, summary_path)


# ----- Log -----------------------------------------------------------------


def append_log(log_path: Path, now: datetime, status: str, counts: dict, error: str | None) -> None:
    """Append one JSONL line. Never includes credentials."""
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "status": status,
        "projects": counts,
        "error": error,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ----- Main ----------------------------------------------------------------


def main() -> int:
    # Gate: with the module off there is no credential read and no network.
    if not jira_config.is_enabled():
        print(
            "Jira module disabled. Set PA_JIRA_ENABLED=1 (or jira.enabled: true "
            "in memory/workstream_config.yml) to turn it on. No action taken."
        )
        return 0

    now = datetime.now(jira_config.local_tz())
    tz_label = jira_config.tz_label()

    data_dir = jira_config.get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    sync_json = data_dir / "jira-sync.json"
    sync_summary = data_dir / "jira-summary.md"
    sync_log = data_dir / "jira-sync-log.jsonl"

    projects = jira_config.get_projects()
    base_url = jira_config.get_base_url()
    email = jira_config.get_account_email()
    api_token = jira_config.get_api_token()

    # Validate config/credentials. Missing secrets are reported by env-var NAME
    # only - never a value.
    missing: list[str] = []
    if not base_url:
        missing.append("PA_JIRA_BASE_URL")
    if not email:
        missing.append("PA_JIRA_EMAIL")
    if not api_token:
        missing.append("PA_JIRA_API_TOKEN")
    if missing:
        msg = "CONFIG_INCOMPLETE: set " + ", ".join(missing)
        print(msg, file=sys.stderr)
        existing = read_existing_cache(sync_json)
        write_failed_cache(sync_json, now, existing)
        append_log(sync_log, now, "fail", {}, msg)
        return 1
    if not projects:
        msg = (
            "CONFIG_INCOMPLETE: no Jira project keys configured - set "
            "PA_JIRA_PROJECT_KEYS or jira.project_keys in workstream_config.yml"
        )
        print(msg, file=sys.stderr)
        existing = read_existing_cache(sync_json)
        write_failed_cache(sync_json, now, existing)
        append_log(sync_log, now, "fail", {}, msg)
        return 1

    auth_header = make_auth_header(email, api_token)
    existing = read_existing_cache(sync_json)

    try:
        all_issues = fetch_all_issues(base_url, auth_header, projects)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            msg = "AUTH_FAILED: verify PA_JIRA_API_TOKEN"
        elif exc.code == 403:
            msg = "AUTH_FAILED: account lacks permission for these projects"
        elif exc.code == 410:
            msg = "API_REMOVED: legacy /search endpoint is gone - this build uses /search/jql"
        else:
            msg = f"HTTP_ERROR: Jira API returned {exc.code}"
        print(msg, file=sys.stderr)
        write_failed_cache(sync_json, now, existing)
        append_log(sync_log, now, "fail", {}, msg)
        return 1
    except Exception as exc:
        msg = f"SYNC_ERROR: {type(exc).__name__}: unexpected error - check environment and network"
        print(msg, file=sys.stderr)
        write_failed_cache(sync_json, now, existing)
        append_log(sync_log, now, "fail", {}, msg)
        return 1

    tz = jira_config.local_tz()
    by_project: dict[str, list] = {k: [] for k in projects}
    for issue in all_issues:
        key = issue.get("key", "")
        project_key = key.split("-")[0] if "-" in key else ""
        if project_key in by_project:
            by_project[project_key].append(extract_ticket(issue, tz))
        # Issues for projects outside the configured set are ignored.

    projects_data: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for project_key, project_name in projects.items():
        tickets = by_project.get(project_key, [])
        projects_data[project_key] = build_project_block(project_name, tickets)
        counts[project_key] = len(tickets)

    write_cache(sync_json, now, projects_data)
    write_summary(sync_summary, build_summary(now, tz_label, projects_data))
    append_log(sync_log, now, "success", counts, None)

    print(f"Wrote {sync_json}")
    print(f"Wrote {sync_summary}")
    print(f"Appended log line to {sync_log}")
    total = sum(counts.values())
    per_proj = "  ".join(f"{k}={v}" for k, v in counts.items())
    print(f"  Total issues fetched: {total}  ({per_proj})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
