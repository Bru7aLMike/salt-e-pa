---
type: doc
purpose: Architecture overview of the scaffold's memory tiers and deterministic scripts.
---

# Architecture

Salt-e PA is a scaffold for a file-based personal assistant that runs inside
Claude Code. It gives an assistant durable, layered memory across sessions plus
a small set of deterministic scripts that keep that memory fresh. This document
explains the moving parts so you can understand the system before you invest in
filling it with your own content.

Nothing here is enabled on a fresh clone. The scanners and hooks run only after
you wire them up (see `docs/automation.md`), and every memory file ships as an
empty placeholder template. This document describes what the machinery does, not
what any particular person put in it.

## The two directories

The system splits into two roots that must never be confused:

- **Memory directory** (`memory/` in this scaffold) holds durable knowledge: the
  top-level index and config files at the root, plus two subtrees - `system/`
  (the assistant's own machinery) and `content/` (the user's life). See The
  three-tier memory layout below.
- **Working directory** (the repo root in this scaffold) holds the charter
  (`CLAUDE.md`), the scripts, task orchestration folders, drafts, and scratch
  artifacts.

Keeping them separate means a scratch file never lands in memory and a memory
file never gets treated as disposable. Both roots are configurable, so you can
point the scripts at directories that live anywhere on disk (see Configuration
below).

## The three-tier memory layout

Memory splits into three tiers that separate the assistant's own machinery from
the user's life, so the machinery-vs-data boundary is a fact on disk and not just
a claim in this document.

```
memory/
  # Tier 0 - always-on index and config at the root
  MEMORY.md  BRIEFING.md  MAP.md  DEADLINES.md  INTEGRITY.md
  SCHEMAS.md  MIGRATIONS.md  aliases.yml  workstream_config.yml

  # Tier 1 - system/: the assistant's own machinery and its own workstreams
  system/
    _internal/            passive bookkeeping the scanners maintain
    rules/                behavioral rules (the always-active subset is inlined in CLAUDE.md)
    workspace/            operation and orchestration reference
    <infra-workstream>/   optional: the assistant's own dev projects (README.md, hand-offs/)

  # Tier 2 - content/: the user's life, all user data
  content/
    work/<workstream>/            README.md, hand-offs/, topic files
    personal/<workstream>/...
    entrepreneurial/<workstream>/...
    <custom-area>/<workstream>/...
```

- **Tier 0** stays pure index and config at the memory root. Membership is fixed:
  `MEMORY.md`, `BRIEFING.md`, `MAP.md`, `DEADLINES.md`, `INTEGRITY.md`,
  `SCHEMAS.md`, `MIGRATIONS.md`, plus `aliases.yml` and `workstream_config.yml`.
- **`system/`** holds the assistant's own machinery: `_internal/` bookkeeping,
  the `rules/` catalog, `workspace/` reference, and any workstream that is about
  the assistant itself rather than the user's life.
- **`content/`** holds the user's life, organized into areas. Three areas ship as
  empty stubs (`work/`, `personal/`, `entrepreneurial/`); you can add your own.
- Handoffs are **per-workstream**: each workstream folder carries its own
  `hand-offs/`. There is no central handoff directory. The scanners synthesize the
  cross-workstream chronological view into `BRIEFING.md` and `MAP.md`, so nothing
  is lost by keeping handoffs local to their workstream.

Within a tier, memory is still a navigation hierarchy: a top index points down to
section indexes, which point down to topic files. Each level links down and never
duplicates the level below. One file has one purpose.

### When files are read

The layout above is about where content lives; a second, orthogonal question is
when each file gets loaded, because unused context is a cost, not a safety margin.

| Load order | When read | Files |
| --- | --- | --- |
| Always | Auto-loaded by the harness every session | `CLAUDE.md` (charter, working dir), `MEMORY.md` (top index, memory dir) |
| Session start | Read explicitly at the start of each session | `BRIEFING.md`, `MAP.md`, `DEADLINES.md` |
| Conditional | Read only when a condition triggers | `INTEGRITY.md` (when it reports a CRITICAL count above zero) |
| On demand | Read when a topic needs it | Section `INDEX.md` files, workstream `README.md` files, `SCHEMAS.md`, individual rule files, archived handoffs |

The always-on and session-start reads are the fixed price of orienting;
everything else stays unread until the conversation calls for it.

### The memory files

- **`MEMORY.md`** is the top index. One row per section, each pointing to that
  section's own `INDEX.md`. It never holds detail itself.
- **`BRIEFING.md`** is the daily status file. It has two regions with two
  independent owners (see The BRIEFING contract below): a hand-authored region
  for standing reminders and the active-handoff list, and a scanner-owned block
  that reports current workstream activity.
- **`MAP.md`** is a generated orientation map: the entry point for looking a
  topic or alias up and finding which workstream it belongs to. The workspace
  scanner rewrites it in full; hand edits are overwritten.
- **`DEADLINES.md`** is a generated register of dated items pulled from across
  the memory tree, sorted into overdue, imminent, and upcoming. The deadline
  scanner rewrites it in full.
- **`INTEGRITY.md`** is a generated report of consistency findings, bucketed
  CRITICAL / WARN / INFO. The workspace scanner rewrites it in full. Session
  start reads its CRITICAL counter and stops to fix problems before proceeding.
- **`SCHEMAS.md`** holds the contracts that shipped memory files must honor,
  one section per contract. It is hand-authored, read on demand.
- **`MIGRATIONS.md`** is a hand-authored log of schema changes over time.
- **`content/` areas** (for example `work/`, `personal/`, `entrepreneurial/`)
  each carry an `INDEX.md` and, when populated, workstream folders. A
  **workstream** is a unit of ongoing work: a folder with a `README.md` whose
  frontmatter declares a `workstream_id`. The scanners discover workstreams by
  walking the roots configured in `workstream_config.yml`.
- **`system/rules/`** holds individual behavioral rules plus an `INDEX.md`. The
  workspace scanner cross-checks the rules against the index and the charter.
- **`system/workspace/`** holds operation and orchestration reference material.
- **Per-workstream `hand-offs/`** is the archive of session handoffs for a single
  workstream, one file per session, named so they sort chronologically. Each
  workstream owns its own; there is no central handoff directory.
- **`system/_internal/`** holds scanner bookkeeping (for example a size log) and
  is never loaded as context.

### Generated versus hand-authored

The split between generated and hand-authored files is the core of the design.
Hand-authored files (`MEMORY.md`, `SCHEMAS.md`, the workstream READMEs, the
rules, the hand-authored region of `BRIEFING.md`) are where you and the
assistant record knowledge. Generated files (`MAP.md`, `DEADLINES.md`,
`INTEGRITY.md`, and the scanner block of `BRIEFING.md`) are derived views that a
script rebuilds from the hand-authored source. You never edit a generated file
by hand, because the next scan overwrites it. This keeps the assistant's
orientation reads (tier 1) always consistent with the underlying source without
anyone maintaining them manually.

## The scanners

Two deterministic Python scripts keep the generated files fresh. Both are plain
entry points that take no arguments, so they run the same from a shell or a
scheduler. They are meant to run once a day, the deadline scan a few minutes
before the workspace scan so the workspace snapshot reflects the day's freshly
computed deadlines.

### Deadline scanner (`scripts/deadline_scanner.py`)

Walks every markdown file in the memory tree and extracts dated items using
three conventions:

- `deadline: YYYY-MM-DD` in a file's frontmatter marks a whole-file deadline.
- `**Due:** YYYY-MM-DD` inline marks a per-line deadline.
- `**Deadline:** YYYY-MM-DD` inline is an accepted alias for the same.

It ignores dates inside fenced code blocks and inline code spans so quoted
examples do not register as real deadlines. It sorts the collected items into
overdue (past), imminent (within 7 days), and upcoming (8 to 30 days), then
writes `DEADLINES.md`. It can also fold in dated items from an optional external
cache if one is configured, and degrades to zero such items when it is absent.

### Workspace scanner (`scripts/workspace_scanner.py`)

Builds the activity picture the assistant reads at session start. In order, it:

1. Reads `workstream_config.yml` and walks each configured root one level deep.
2. Registers every child folder whose `README.md` frontmatter carries a valid
   `workstream_id` as a workstream.
3. For each workstream, finds the newest handoff in that workstream's own
   `hand-offs/` folder, sorting by the handoff's `session_end` timestamp.
4. Extracts the structured fields from that newest handoff (next actions,
   blockers, open items, status).
5. Runs a battery of integrity checks: workstream id collisions, missing or
   unparseable frontmatter, orphaned aliases, alias collisions, and rules that
   drift from their index.
6. Writes three outputs atomically: `MAP.md` (full rewrite), `INTEGRITY.md`
   (full rewrite), and the scanner-owned block of `BRIEFING.md` (surgical
   splice, described next).

The scanner refuses to run if `SCHEMAS.md` declares a major schema version it
was not built for, so a schema change cannot silently corrupt the generated
files. It also records a byte-size log of the orientation files on each run so
you can watch the fixed context cost over time.

### The BRIEFING contract

`BRIEFING.md` has two producers that must never clobber each other. The scanner
owns the block between the `<!-- PA_SCAN:start -->` and `<!-- PA_SCAN:end -->`
markers; the handoff pipeline owns everything outside that pair. The scanner
enforces this two ways:

- It rewrites its block with a raw-**byte** splice rather than a text-mode
  rewrite, so it cannot translate newlines or shift a single byte of the
  hand-authored region.
- If either marker is missing it **fails loud** (non-zero exit, no write)
  instead of creating or appending, because a blind write would clobber the
  hand-authored region.

The contract is documented in `SCHEMAS.md` and guarded by a test (see the HAS
pipeline below).

## The HAS pipeline

HAS (handoff-as-subagent) is the session-end machinery that turns a raw Claude
Code transcript into a durable, structured handoff. It lives in `scripts/has/`
and runs when the user ends a session. It has three parts plus a contract test.

1. **Session-start hook** (`has-session-start.sh`). Registered as a Claude Code
   `SessionStart` hook, it records the current transcript path to a per-session
   scratch file so the end-of-session step can find the transcript later. It
   also clears the previous session's filter output.
2. **Transcript filter** (`has-filter.py`). Takes the transcript and an output
   base path as arguments. It strips low-signal content (thinking blocks,
   file-read and search results, system reminders), keeps all user and assistant
   text plus git command output verbatim, normalizes shell commands so git
   operations can be classified, and splits the result into chunks at message
   boundaries when it exceeds a token threshold. It prints the output path or
   paths.
3. **Subagent prompt** (`has-subagent-prompt.md`). A prose template that the
   session-end command fills in (transcript paths, memory dir, session date and
   timestamp, target handoff filename, scratch dir, touched ticket keys, user
   name) before spawning a subagent. The subagent reads the filtered chunks,
   builds a typed ledger of what happened, re-verifies git state, then writes the
   handoff file, appends a row to the workstream README's active-handoff list,
   and drops a summary in the scratch dir.

After the subagent writes the handoff, the workspace scanner runs to refresh the
`BRIEFING.md` activity block. The **contract test** (`test_has_briefing_contract.py`)
proves the boundary between HAS and the scanner holds: after a scanner refresh
the hand-authored region is byte-identical, and with the markers stripped the
scanner refuses to write.

## Bootstrap and writing-discipline utilities

- **`scripts/init_workstream.py`** bootstraps a new, empty workstream. It creates
  the folder, writes a `README.md` with the required frontmatter filled and the
  body left as prompts to complete, creates an empty `hand-offs/` subfolder, and
  registers the workstream in `aliases.yml`. It writes the README with a direct
  file write so no auto-memory layer can wrap it in an envelope that would hide
  the `workstream_id` from the scanner. Run it with `--dry-run` to preview.
- **`scripts/inject-unslop.sh`** is a `SessionStart` hook that injects an
  always-active writing-discipline catalog into context, so the guidance is
  present without a manual load and is re-injected after a compaction. If the
  configured skill file is absent the hook exits silently, so a fresh clone is
  unaffected.

There is also `scripts/ng0/`, the leak-scanning tooling that proves the
published tree ships none of the author's personal data, and
`scripts/push-backups.sh`, an optional manual backup helper. Those are described
in `scripts/ng0/README.md` and in the automation and contributing docs; they are
support tooling rather than part of the runtime memory loop.

## Configuration

No personal path is hard-coded. Every path and locale constant resolves with a
fixed three-level precedence, so a fresh clone runs with zero edits and you
override only what you need:

1. An environment variable (the per-key `PA_*` or `HAS_*` name).
2. The value in `memory/workstream_config.yml`.
3. A repo-relative default.

The config file ships as placeholder tokens (for example `<MEMORY_DIR>`). A
placeholder means "unset, use the default"; replace it with a real value to pin
it, or delete the line to keep the default. The same pattern configures the
memory and working roots, the locale used for date math and generated
timestamps, the workstream discovery roots, the HAS scratch and wrap-state
directories, and the unslop skill path. Because the discovery roots ship empty,
the scanners run cleanly over an empty tree and simply report no workstreams
until you add some.

## Cross-platform support

The runtime is split between Python and POSIX shell, which affects where each
piece runs.

| Script | Language | Runs on |
| --- | --- | --- |
| `scripts/deadline_scanner.py` | Python 3 | Windows, macOS, Linux |
| `scripts/workspace_scanner.py` | Python 3 | Windows, macOS, Linux |
| `scripts/init_workstream.py` | Python 3 | Windows, macOS, Linux |
| `scripts/has/has-filter.py` | Python 3 | Windows, macOS, Linux |
| `scripts/has/test_has_briefing_contract.py` | Python 3 | Windows, macOS, Linux |
| `scripts/ng0/secret_pii_scan.py` | Python 3 | Windows, macOS, Linux (git-history mode needs `git` on PATH) |
| `scripts/has/has-session-start.sh` | POSIX shell (bash) | macOS, Linux natively; Windows via Git Bash |
| `scripts/inject-unslop.sh` | POSIX shell (bash) | macOS, Linux natively; Windows via Git Bash |
| `scripts/push-backups.sh` | POSIX shell (bash) | macOS, Linux natively; Windows via Git Bash |

Notes:

- The Python scripts target Python 3.11 or newer (they rely on modern
  `datetime.fromisoformat` parsing and union type hints) and depend only on
  **PyYAML** beyond the standard library.
- The two SessionStart hooks and the backup helper are bash scripts. On Windows,
  Claude Code runs hook commands through the shell it is configured to use; Git
  Bash provides the POSIX environment they need. The hooks shell out to Python
  to read the config, so Python must be on PATH there too.
- The scanners write their outputs atomically (write to a temporary file, then
  replace) and splice `BRIEFING.md` on raw bytes, which keeps line endings intact
  across platforms rather than translating them.
