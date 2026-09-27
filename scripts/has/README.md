---
type: doc
purpose: Overview of the HAS handoff-as-subagent pipeline that runs at /wrap.
---

# HAS - Handoff-as-Subagent pipeline

HAS is the session-end machinery that turns a raw Claude Code transcript into a
durable, structured handoff. It runs at `/wrap`. This directory holds the three
components plus the contract test.

```
scripts/has/
  has-session-start.sh          SessionStart hook: records the transcript path
  has-filter.py                 transcript -> filtered, chunked, low-signal-stripped text
  has-subagent-prompt.md        prose prompt for the handoff subagent (template)
  test_has_briefing_contract.py scanner <-> HAS BRIEFING boundary test
  README.md                     this file
```

## Flow

```
session start ──> has-session-start.sh ──> $WRAP_STATE_DIR/<session_id>.json
                                             (records transcript_path)

/wrap ──> has-filter.py <transcript> <out_base>   -> filtered_*.txt chunk(s)
      ──> spawn handoff subagent with has-subagent-prompt.md (vars substituted)
      │      reads chunks -> typed ledger -> re-verifies git -> writes:
      │        - <active workstream>/hand-offs/<date>_NN_<descriptor>.md
      │        - appends a row to the workstream README "Active handoffs" list
      │        - a structured summary to <scratch>/has-output.txt
      └──> workspace_scanner.py refresh -> rewrites ONLY the BRIEFING PA_SCAN block
```

## The BRIEFING block-ownership contract (what the test guards)

`memory/BRIEFING.md` has two independent producers. Each owns exactly one region
and writes nothing in the other's:

| Region | Owner | Content |
|---|---|---|
| Between `<!-- PA_SCAN:start -->` and `<!-- PA_SCAN:end -->` | workspace scanner | generated activity block |
| Everything OUTSIDE that marker pair | HAS pipeline | Session Reminders, Active Handoffs, prose |

The scanner rewrites its block with a raw-**byte** splice (not a text-mode
rewrite), so it cannot translate newlines or shift a single byte of the
hand-authored region. If the marker pair is missing it **fails loud** (non-zero
exit, no write) rather than overwriting or appending - a missing-marker file
would otherwise get clobbered. `test_has_briefing_contract.py` proves both
halves:

- round-trip: after a scanner refresh (the concluding step of a HAS run), the
  hand-authored region is byte-identical.
- fail-loud: strip the markers and the scanner refuses to write, which aborts
  the pipeline so HAS never silently clobbers the hand-authored region.

Run it:

```bash
python -m pytest scripts/has/test_has_briefing_contract.py -v
# or, no pytest on PATH:
python scripts/has/test_has_briefing_contract.py
```

## Configuration

All personal constants resolve with the same 3-level precedence as the scanners
(env var first, then `memory/workstream_config.yml`, then a repo-relative
default), so the pipeline runs unedited in a fresh checkout.

| Key | Env var | Config (`has:`) | Default |
|---|---|---|---|
| wrap-state dir | `HAS_WRAP_STATE_DIR` | `has.wrap_state_dir` | `$HOME/.claude/wrap-state` |
| scratch dir | `HAS_SCRATCH_DIR` | `has.scratch_dir` | `<repo>/scratch` |
| config file path | `HAS_CONFIG_FILE` | (n/a) | `<repo>/memory/workstream_config.yml` |

`has-filter.py` takes its input transcript and output base as CLI arguments, so
it needs no configuration of its own.

## has-subagent-prompt.md template variables

The prompt is a template. The `/wrap` command substitutes these before spawning
the subagent:

| Variable | Meaning |
|---|---|
| `{{TRANSCRIPT_FILES}}` | filter output path(s), read in order |
| `{{MEMORY_DIR}}` | memory-tree root |
| `{{HANDOFF_DIR}}` | the active workstream's own `hand-offs/` directory, resolved by `/wrap`; the only place the handoff is written (there is no central handoff directory) |
| `{{SESSION_DATE}}` | `YYYY-MM-DD` |
| `{{SESSION_END}}` | ISO-8601 timestamp with offset |
| `{{HANDOFF_FILENAME}}` | `YYYY-MM-DD_NN_<descriptor>.md` |
| `{{SCRATCH_DIR}}` | scratch dir for `has-output.txt` |
| `{{JIRA_TICKETS_TOUCHED}}` | YAML list of ticket keys, or `[]` |
| `{{USER_NAME}}` | the human whose review/decisions the handoff surfaces |

Ticket keys use a generic `PROJECT-NN` form; the classifier is the leading
`[A-Z]+-\d+` prefix, not any specific project key.

## Wiring the SessionStart hook

Register `has-session-start.sh` as a Claude Code `SessionStart` hook so it
records the transcript path for `/wrap`. Claude Code pipes a JSON object
(`session_id`, `transcript_path`) to the hook on stdin.

## has-filter.py usage

```bash
python scripts/has/has-filter.py <transcript.jsonl> <output_base> \
    [--chunk-size TOKENS] [--verbose]
```

It strips low-signal content (thinking blocks, Read/Grep/Glob results,
system reminders), keeps all user/assistant text and git tool output verbatim,
normalizes Bash commands for git classification, and chunks at message
boundaries when the filtered text exceeds `--chunk-size` (default 15000 tokens).
It prints the output path(s), one per line.
