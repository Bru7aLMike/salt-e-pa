---
type: doc
purpose: How to wire the optional SessionStart hooks and daily scanner jobs.
---

# Automation: SessionStart hooks and scheduled scans

This scaffold ships two kinds of automation you opt into by hand:

1. Two Claude Code `SessionStart` hooks, wired through `settings.template.json`.
2. Two daily scanner jobs, run by your OS scheduler (cron or equivalent).

Everything is generic. You substitute one absolute path and, optionally, a
scheduled-task prefix. Nothing here is enabled until you wire it up, so a fresh
clone runs nothing on its own.

## SessionStart hooks

`settings.template.json` (repo root) wires ONLY the two generic SessionStart
hooks the scaffold ships:

| Hook script | What it does |
|---|---|
| `scripts/has/has-session-start.sh` | Records the transcript path so `/wrap` can find it at session end. |
| `scripts/inject-unslop.sh` | Injects the always-active writing-discipline catalog into context. |

The HAS hook only records the transcript path, so it is inert until you run
`/wrap`. The unslop hook ships a bundled skill at `skills/unslop/SKILL.md` and
injects it out of the box, so on a fresh checkout it adds the writing-discipline
catalog to context from the first session (no env or config needed). Point
`PA_UNSLOP_SKILL` or the `unslop.skill_file` config key at your own skill to
override the bundle; the hook only falls silent if that bundled file is removed
and nothing else resolves. Wiring both hooks early is safe.

### Placeholder convention

Claude Code hook commands need an absolute path. The template uses a single
placeholder, `<REPO_ROOT>`, for the absolute path of your clone. Replace every
`<REPO_ROOT>` with that path, for example:

- `/home/you/salt-e-pa` on Linux or macOS
- `C:/Users/you/salt-e-pa` on Windows (forward slashes work in Git Bash)

### Install

1. Copy `settings.template.json` to `~/.claude/settings.json`, or merge just its
   `hooks` block into an existing `~/.claude/settings.json`.
2. Replace every `<REPO_ROOT>` with your clone's absolute path.
3. Delete the `_comment` key if you prefer a clean file (it is only documentation;
   JSON has no comment syntax).
4. Start a new Claude Code session and confirm the hooks run.

The template carries no `permissions`, `env`, `statusLine`, or `enabledPlugins`
keys. Add those separately in your own settings; keeping them out of the template
avoids shipping anyone else's machine config.

## Scheduled scans

Two generic recurring jobs keep the generated memory files fresh. Both are plain
Python entry points with no arguments:

| Job | Script | Suggested time |
|---|---|---|
| Daily deadline scan | `scripts/deadline_scanner.py` | 08:10 local |
| Daily workspace scan | `scripts/workspace_scanner.py` | 08:15 local |

Run the workspace scan a few minutes after the deadline scan so the workspace
snapshot reflects the day's freshly computed deadlines.

### crontab example

Replace `<REPO_ROOT>` with your clone's absolute path:

```cron
# daily deadline scan at 08:10
10 8 * * * cd <REPO_ROOT> && python scripts/deadline_scanner.py

# daily workspace scan at 08:15
15 8 * * * cd <REPO_ROOT> && python scripts/workspace_scanner.py
```

If your scanners read paths or a timezone from environment variables (see
`memory/workstream_config.yml` for the `PA_*` names), export them in the same
line or in the crontab header so the scheduled run resolves them the same way
your interactive shell does.

### Task-ID prefix convention

If you run these through a named-task scheduler (rather than bare crontab lines),
give every task ID a short prefix that is unique to this workspace so the IDs
never collide with scheduled tasks from another project on the same machine.
Pick your own prefix, for example `mypa-`:

- `mypa-deadline-scan`
- `mypa-workspace-scan`

The prefix is a convention, not a requirement of the scripts. Choose any short
string you will recognize and apply it consistently.

### Not shipped

Only the two generic scanner jobs above are part of the scaffold. Any
project-specific recurring jobs (external data syncs, health audits, watchers)
are yours to add per workstream; the scaffold ships none of them.
