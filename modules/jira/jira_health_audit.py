#!/usr/bin/env python3
"""Optional monthly health audit for the Jira module (Salt-e PA scaffold).

DISABLED by default. Gated behind `jira_config.is_enabled()`; with the flag off
the script prints a notice and exits 0 without reading anything.

When enabled, it runs three read-only checks against the module's own output
files (no network) and writes into the module data dir:
  - jira-health-audit.md        -- full report (always written)
  - jira-health-audit-log.jsonl -- one JSON line appended per run
  - jira-health-audit-ALERT.txt -- written only when actionable flags exist;
                                   cleared (truncated) when clean

Checks:
  1. Stale tickets  - non-Done tickets not updated in >= STALE_DAYS, from the
                      sync cache (jira-sync.json).
  2. Sync uptime    - success rate over the past 30 days, from the sync log
                      (jira-sync-log.jsonl).
  3. Label drift    - ticket labels not present in an OPTIONAL canonical
                      taxonomy file (jira-label-taxonomy.md); skipped with a
                      note when that file is absent.

Scope note: this audit reads only the Jira module's own data dir. It does not
reach into the memory tree, handoffs, or any core scanner - the module stays
cleanly separable from the rest of the scaffold.

Run manually:  python modules/jira/jira_health_audit.py
Run from cron: same, no args (see cron.template).
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import jira_config

# ----- Thresholds ----------------------------------------------------------

STALE_DAYS = 14
UPTIME_WINDOW_DAYS = 30


# ----- Atomic write --------------------------------------------------------


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ----- Cache loader --------------------------------------------------------


def load_sync_cache(sync_json: Path) -> dict | None:
    if not sync_json.exists():
        return None
    try:
        data = json.loads(sync_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "projects" not in data:
        return None
    return data


def all_tickets(cache: dict) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for proj_key, proj in (cache.get("projects") or {}).items():
        for ticket in (proj.get("tickets") or []):
            out.append((proj_key, ticket))
    return out


# ----- Check 1: Stale tickets ----------------------------------------------


def check_stale_tickets(cache: dict | None, today: date, tz) -> dict:
    """Flag non-Done tickets not updated in >= STALE_DAYS days."""
    if cache is None:
        return {"flags": [], "note": "sync cache unavailable - check skipped"}

    flags: list[dict] = []
    for _proj_key, ticket in all_tickets(cache):
        status = ticket.get("status", "")
        if status.lower() == "done":
            continue
        updated_raw = ticket.get("updated", "")
        if not updated_raw:
            continue
        try:
            updated_dt = datetime.fromisoformat(updated_raw)
        except ValueError:
            continue
        updated_dt = updated_dt.astimezone(tz)
        age = (today - updated_dt.date()).days
        if age >= STALE_DAYS:
            flags.append({
                "key": ticket.get("key", "?"),
                "status": status,
                "age_days": age,
                "summary": ticket.get("summary", ""),
            })

    flags.sort(key=lambda x: -x["age_days"])
    return {"flags": flags, "note": None}


# ----- Check 2: Sync uptime ------------------------------------------------


def check_sync_uptime(sync_log: Path, now: datetime, tz) -> dict:
    """Compute sync success rate over the past 30 days from the sync log."""
    cutoff = now - timedelta(days=UPTIME_WINDOW_DAYS)

    if not sync_log.exists():
        return {
            "uptime_pct": None, "runs": 0, "successes": 0,
            "earliest_ts": None, "window_note": None,
            "note": "jira-sync-log.jsonl not found",
        }
    try:
        lines = sync_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {
            "uptime_pct": None, "runs": 0, "successes": 0,
            "earliest_ts": None, "window_note": None,
            "note": "jira-sync-log.jsonl unreadable",
        }

    runs = 0
    successes = 0
    earliest_ts_raw: str | None = None
    earliest_dt: datetime | None = None
    total_entries = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = entry.get("ts", "")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=tz)
        total_entries += 1
        if ts < cutoff:
            continue
        runs += 1
        if entry.get("status") == "success":
            successes += 1
        if earliest_dt is None or ts < earliest_dt:
            earliest_dt = ts
            earliest_ts_raw = ts_raw

    if runs == 0:
        window_note = "no data in window"
        if total_entries:
            window_note = (
                f"log exists but no runs in past {UPTIME_WINDOW_DAYS} days "
                f"(total log entries: {total_entries})"
            )
        return {
            "uptime_pct": None, "runs": 0, "successes": 0,
            "earliest_ts": None, "window_note": window_note, "note": None,
        }

    uptime_pct = successes / runs * 100.0
    window_note: str | None = None
    if earliest_dt is not None:
        data_age_days = (now - earliest_dt).days
        if data_age_days < UPTIME_WINDOW_DAYS - 1:
            window_note = (
                f"data is younger than {UPTIME_WINDOW_DAYS} days "
                f"(earliest in-window run: {earliest_ts_raw})"
            )

    return {
        "uptime_pct": uptime_pct, "runs": runs, "successes": successes,
        "earliest_ts": earliest_ts_raw, "window_note": window_note, "note": None,
    }


# ----- Check 3: Label drift ------------------------------------------------


def parse_canonical_labels(taxonomy: Path) -> tuple[set[str], bool]:
    """Parse the canonical label set from an OPTIONAL taxonomy markdown file.

    Labels are read from the first backtick-wrapped cell of each table row,
    e.g. a row starting `| `blocked` | ...`. Returns (labels, found_file).
    """
    if not taxonomy.exists():
        return set(), False
    try:
        text = taxonomy.read_text(encoding="utf-8")
    except OSError:
        return set(), False

    canonical: set[str] = set()
    for line in text.splitlines():
        canonical.update(re.findall(r"^\|\s*`([^`]+)`", line))
    return canonical, True


def check_label_drift(cache: dict | None, taxonomy: Path) -> dict:
    """Flag ticket labels not present in the canonical taxonomy."""
    if cache is None:
        return {
            "flags": [], "canonical_count": 0, "actual_count": 0,
            "note": "sync cache unavailable - check skipped", "inconclusive": False,
        }

    canonical, found = parse_canonical_labels(taxonomy)
    if not found:
        return {
            "flags": [], "canonical_count": 0, "actual_count": 0,
            "note": (
                "no jira-label-taxonomy.md present - drift check skipped "
                "(add one to enable it)"
            ),
            "inconclusive": True,
        }
    if not canonical:
        return {
            "flags": [], "canonical_count": 0, "actual_count": 0,
            "note": (
                "label taxonomy parsed to 0 labels - format may have changed; "
                "skipping drift check"
            ),
            "inconclusive": True,
        }

    label_to_keys: dict[str, list[str]] = {}
    for _proj_key, ticket in all_tickets(cache):
        t_key = ticket.get("key", "?")
        for lbl in (ticket.get("labels") or []):
            label_to_keys.setdefault(lbl, [])
            if t_key not in label_to_keys[lbl]:
                label_to_keys[lbl].append(t_key)

    actual = set(label_to_keys.keys())
    drift = sorted(actual - canonical)
    flags = [{"label": lbl, "ticket_keys": sorted(label_to_keys.get(lbl, []))} for lbl in drift]

    return {
        "flags": flags, "canonical_count": len(canonical),
        "actual_count": len(actual), "note": None, "inconclusive": False,
    }


# ----- Report builder ------------------------------------------------------


def build_report(
    now: datetime, today: date, tz_label: str,
    stale: dict, uptime: dict, label_drift: dict,
) -> str:
    lines: list[str] = []
    lines.append("---")
    lines.append(f"generated_at: {now.isoformat(timespec='seconds')}")
    lines.append("next_run: monthly - register via your scheduler (see cron.template)")
    lines.append("---")
    lines.append("")
    lines.append("<!-- Generated by modules/jira/jira_health_audit.py - do not hand-edit. -->")
    lines.append("")
    lines.append(f"# Jira Health Audit - {now.strftime('%Y-%m-%d %H:%M')} {tz_label}")
    lines.append("")

    # ---- Check 1 ----------------------------------------------------------
    lines.append("## Check 1 - Stale tickets")
    lines.append("")
    if stale.get("note"):
        lines.append(f"> {stale['note']}")
    else:
        stale_flags = stale.get("flags", [])
        if stale_flags:
            lines.append(
                f"**{len(stale_flags)} stale tickets** "
                f"(non-Done, not updated in >={STALE_DAYS} days as of {today.isoformat()}):"
            )
            lines.append("")
            lines.append("| Key | Status | Age (days) | Summary |")
            lines.append("|---|---|---|---|")
            for flag in stale_flags:
                summary = flag["summary"]
                if len(summary) > 80:
                    summary = summary[:77] + "..."
                lines.append(
                    f"| {flag['key']} | {flag['status']} | {flag['age_days']} | {summary} |"
                )
        else:
            lines.append(
                f"No stale tickets. All non-Done tickets updated within the past {STALE_DAYS} days."
            )
    lines.append("")

    # ---- Check 2 ----------------------------------------------------------
    lines.append(f"## Check 2 - Sync uptime (past {UPTIME_WINDOW_DAYS} days)")
    lines.append("")
    if uptime.get("note"):
        lines.append(f"> {uptime['note']}")
    elif uptime.get("uptime_pct") is None:
        lines.append(f"> {uptime.get('window_note') or 'no data'}")
    else:
        pct = uptime["uptime_pct"]
        earliest = uptime.get("earliest_ts") or "unknown"
        lines.append(
            f"**{uptime['runs']} runs** since {earliest}, "
            f"**{uptime['successes']} success** = **{pct:.1f}%** uptime."
        )
        if uptime.get("window_note"):
            lines.append("")
            lines.append(f"> Note: {uptime['window_note']}")
    lines.append("")

    # ---- Check 3 ----------------------------------------------------------
    lines.append("## Check 3 - Label drift")
    lines.append("")
    if label_drift.get("note"):
        lines.append(f"> {label_drift['note']}")
    else:
        canonical_count = label_drift.get("canonical_count", 0)
        actual_count = label_drift.get("actual_count", 0)
        drift_flags = label_drift.get("flags", [])
        if drift_flags:
            lines.append(
                f"**{len(drift_flags)} drifted labels** "
                f"(actual={actual_count}, canonical={canonical_count}):"
            )
            lines.append("")
            for flag in drift_flags:
                keys_str = ", ".join(flag["ticket_keys"])
                lines.append(f"- `{flag['label']}` - tickets: {keys_str}")
        else:
            lines.append(
                f"No label drift. Canonical={canonical_count}, actual={actual_count}. "
                "All labels match the taxonomy."
            )
    lines.append("")

    # ---- Summary ----------------------------------------------------------
    stale_count = len(stale.get("flags", []))
    drift_count = len(label_drift.get("flags", []))
    uptime_val = uptime.get("uptime_pct")
    uptime_str = f"{uptime_val:.1f}%" if uptime_val is not None else "n/a"

    lines.append("---")
    lines.append("")
    lines.append(
        f"**Summary:** stale={stale_count}, uptime={uptime_str}, "
        f"label-drift={drift_count}. "
        f"Total actionable flags: {stale_count + drift_count}."
    )
    lines.append("")
    return "\n".join(lines)


# ----- Alert file ----------------------------------------------------------


def write_alert(alert_path: Path, stale: dict, label_drift: dict) -> None:
    """Write ALERT.txt if any actionable flags; clear it if all clean."""
    stale_flags = stale.get("flags", [])
    drift_flags = label_drift.get("flags", [])

    if not (stale_flags or drift_flags):
        try:
            alert_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        return

    alert_lines: list[str] = []
    if stale_flags:
        keys = ", ".join(f["key"] for f in stale_flags)
        alert_lines.append(f"CHECK-1: {len(stale_flags)} stale tickets ({keys})")
    if drift_flags:
        labels = ", ".join(f["label"] for f in drift_flags)
        alert_lines.append(f"CHECK-3: {len(drift_flags)} drifted labels ({labels})")

    atomic_write(alert_path, "\n".join(alert_lines) + "\n")


# ----- Log append ----------------------------------------------------------


def append_audit_log(
    log_path: Path, now: datetime, status: str,
    stale: dict, uptime: dict, label_drift: dict, error: str | None,
) -> None:
    uptime_pct = uptime.get("uptime_pct")
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "status": status,
        "checks": {
            "stale": len(stale.get("flags", [])),
            "uptime_pct": round(uptime_pct, 1) if uptime_pct is not None else None,
            "label_drift": len(label_drift.get("flags", [])),
        },
        "error": error,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ----- Main ----------------------------------------------------------------


def main() -> int:
    # Gate: with the module off there is no file read and no work done.
    if not jira_config.is_enabled():
        print(
            "Jira module disabled. Set PA_JIRA_ENABLED=1 (or jira.enabled: true "
            "in memory/workstream_config.yml) to turn it on. No action taken."
        )
        return 0

    tz = jira_config.local_tz()
    tz_label = jira_config.tz_label()
    now = datetime.now(tz)
    today = now.date()

    data_dir = jira_config.get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    sync_json = data_dir / "jira-sync.json"
    sync_log = data_dir / "jira-sync-log.jsonl"
    taxonomy = data_dir / "jira-label-taxonomy.md"
    audit_report = data_dir / "jira-health-audit.md"
    audit_log = data_dir / "jira-health-audit-log.jsonl"
    audit_alert = data_dir / "jira-health-audit-ALERT.txt"

    cache = load_sync_cache(sync_json)

    stale = check_stale_tickets(cache, today, tz)
    uptime = check_sync_uptime(sync_log, now, tz)
    label_drift = check_label_drift(cache, taxonomy)

    report = build_report(now, today, tz_label, stale, uptime, label_drift)
    try:
        atomic_write(audit_report, report)
        write_alert(audit_alert, stale, label_drift)
        append_audit_log(audit_log, now, "success", stale, uptime, label_drift, None)
    except Exception as exc:
        write_error = f"{type(exc).__name__}: {exc}"
        try:
            append_audit_log(audit_log, now, "fail", stale, uptime, label_drift, write_error)
        except Exception:
            pass
        print(f"ERROR writing outputs: {write_error}", file=sys.stderr)
        return 1

    stale_count = len(stale.get("flags", []))
    drift_count = len(label_drift.get("flags", []))
    uptime_val = uptime.get("uptime_pct")
    uptime_str = f"{uptime_val:.1f}%" if uptime_val is not None else "n/a"

    print(f"Jira health audit complete - {now.strftime('%Y-%m-%d %H:%M')} {tz_label}")
    print(f"  CHECK-1 stale tickets: {stale_count}")
    if uptime.get("note") or uptime_val is None:
        print(f"  CHECK-2 sync uptime:   {uptime.get('window_note') or uptime.get('note') or 'no data'}")
    else:
        print(f"  CHECK-2 sync uptime:   {uptime_str} ({uptime['successes']}/{uptime['runs']} runs)")
    if label_drift.get("inconclusive"):
        print(f"  CHECK-3 label drift:   skipped ({label_drift.get('note')})")
    else:
        print(f"  CHECK-3 label drift:   {drift_count} drifted labels")
    print(f"Wrote {audit_report}")
    if audit_alert.exists() and audit_alert.stat().st_size > 0:
        print(f"Alert written to {audit_alert}")
    else:
        print("No actionable flags - alert file cleared")

    return 0


if __name__ == "__main__":
    sys.exit(main())
