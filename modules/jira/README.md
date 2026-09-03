---
type: doc
purpose: Reference pattern for the optional, opt-in Jira module.
---

# Optional Jira module

A self-contained, opt-in integration with any Atlassian Cloud Jira instance.
It is the reference pattern for wiring an external service into this scaffold:
disabled by default, gated behind a single flag, credentials from the
environment only, and confined to this directory. Nothing in the core scaffold
imports it, and the core runs with zero Jira dependency when the module is off.

## What it does

When enabled, two plain Python entry points run against your Jira instance:

| Script | Purpose | Outputs (in the data dir) |
|---|---|---|
| `jira_sync.py` | Pull ticket state for your project keys via the REST API. | `jira-sync.json`, `jira-summary.md`, `jira-sync-log.jsonl` |
| `jira_health_audit.py` | Three read-only checks over the sync outputs (no network). | `jira-health-audit.md`, `jira-health-audit-log.jsonl`, `jira-health-audit-ALERT.txt` |

`jira_config.py` holds the shared config resolution and the opt-in gate. The
health audit runs three checks: stale tickets (from the sync cache), sync
uptime over the past 30 days (from the sync log), and label drift against an
optional `jira-label-taxonomy.md` you supply. It reads only this module's data
dir - it never touches the memory tree, handoffs, or any core scanner.

## Off by default

Every code path is gated behind `jira_config.is_enabled()`. On a fresh clone
that returns `False`, so:

- no credential is read,
- no network call is made,
- both scripts print a one-line notice and exit 0.

Running either script before opting in is a safe no-op. You never need to set
any Jira variable unless you decide to turn the module on.

## Turning it on

1. Set the opt-in flag. Either export `PA_JIRA_ENABLED=1`, or add a `jira`
   block to `memory/workstream_config.yml`:

   ```yaml
   jira:
     enabled: true
     base_url: <JIRA_BASE_URL>          # e.g. https://your-domain.atlassian.net
     project_keys:                      # or the mapping form below
       - <KEY_A>
       - <KEY_B>
     # projects:                        # optional: key -> display name
     #   <KEY_A>: <Project A name>
   ```

2. Provide the two credentials as environment variables (never in a committed
   file). Create a Jira API token from your Atlassian account settings.

3. Configure the base URL and project keys (either the config block above or
   the env vars below).

4. Run `python modules/jira/jira_sync.py`. It writes the cache, summary, and log
   into the data dir.

## Configuration reference

Non-secret settings resolve with the scaffold's standard 3-level precedence:
environment variable, then `memory/workstream_config.yml`, then a default.
Credentials are the exception: they are read from the environment only, with no
config key and no default value.

| Variable | Kind | Source | Meaning |
|---|---|---|---|
| `PA_JIRA_ENABLED` | flag | env or `jira.enabled` | Master opt-in. Default off. |
| `PA_JIRA_BASE_URL` | setting | env or `jira.base_url` | Instance base URL. The `https://` prefix is added if you omit it. |
| `PA_JIRA_PROJECT_KEYS` | setting | env or `jira.project_keys` / `jira.projects` | Project keys to sync (env form is comma-separated). |
| `PA_JIRA_EMAIL` | credential | env only | Account email for HTTP Basic auth. |
| `PA_JIRA_API_TOKEN` | credential | env only | Atlassian API token for HTTP Basic auth. |
| `PA_JIRA_DATA_DIR` | setting | env or `paths.working_dir` | Where outputs are written. Default `<repo>/data`. |
| `PA_TZ_OFFSET_HOURS` | setting | env or `locale.utc_offset_hours` | Offset for generated timestamps. Default 0. |
| `PA_TZ_NAME` | setting | env or `locale.tz_name` | Timezone label in generated files. Default UTC. |

If `PA_JIRA_PROJECT_KEYS` is unset the module also falls back to the shared
top-level `jira_project_keys` list in `workstream_config.yml`.

### Credential handling

- The API token is read from `PA_JIRA_API_TOKEN` at run time, held only long
  enough to build the auth header, and never printed, logged, or written to any
  output file.
- Missing credentials are reported by variable name only, never by value.
- Keep secrets out of the repo. `.env` and `*.local.*` are git-ignored; export
  the credentials in your shell or your scheduler's environment instead.

## Scheduling

`cron.template` in this directory has two ready-to-edit entries: a daily sync
and a monthly health audit. Replace `<REPO_ROOT>` with your clone's absolute
path and export the `PA_JIRA_*` variables in the crontab header. Both jobs
no-op until the module is opted in, so registering them early is safe.

## Requirements

- Python 3.10+ (the scripts use `X | Y` type unions).
- `PyYAML` (already used elsewhere in the scaffold) for config parsing.
- Standard library only for the HTTP calls (`urllib`).
