# /wrap - HAS Handoff Pipeline

Session ID: ${CLAUDE_SESSION_ID}

## Instructions

This is the Handoff-as-Subagent (HAS) pipeline. It turns the current session's
transcript into a durable, structured handoff written to the ACTIVE workstream's
own `hand-offs/` directory. Execute these steps IN ORDER. Do not skip steps.

### Step 0: Resolve paths

Everything resolves relative to the clone (the repo root, `<REPO_ROOT>`) or from
config, with the scaffold's standard 3-level precedence (environment variable,
then `memory/workstream_config.yml`, then a repo-relative default). No absolute
personal path is baked in.

- `MEMORY_DIR`: `PA_MEMORY_DIR` env, then `paths.memory_dir` in
  `memory/workstream_config.yml`, then default `<REPO_ROOT>/memory`.
- `SCRATCH_DIR`: `HAS_SCRATCH_DIR` env, then `has.scratch_dir` in config, then
  default `<REPO_ROOT>/scratch`.
- `WRAP_STATE_DIR`: `HAS_WRAP_STATE_DIR` env, then `has.wrap_state_dir` in
  config, then default `$HOME/.claude/wrap-state`.

### Step 1: Read scratch file

Use the Read tool to read the SessionStart hook scratch file:

```
${WRAP_STATE_DIR}/${CLAUDE_SESSION_ID}.json
```

If the file doesn't exist, STOP and report:
> ERROR: SessionStart hook scratch file not found for session ${CLAUDE_SESSION_ID}. The hook may not have fired. Write a manual handoff using the degraded-mode procedure below.

Extract `transcript_path` and `session_started` from the JSON.

### Step 2: Transcript readiness check

Read the last 20 lines of the transcript JSONL:

```bash
tail -20 "<transcript_path>"
```

Verify that a line contains the string `wrap` in a user message (this command's
own invocation). If NOT found, this is a hard error:
> ERROR: /wrap invocation not found in transcript tail. Wrong transcript or corrupted write. Use manual handoff.

### Step 3: Run filter script

```bash
python "<REPO_ROOT>/scripts/has/has-filter.py" "<transcript_path>" "${SCRATCH_DIR}/filtered_${CLAUDE_SESSION_ID}" --verbose
```

Capture the output (file paths). If the script errors, STOP and report.

### Step 4: Resolve the active workstream and its handoff directory

The handoff is written per-workstream - there is no central handoff directory.
As the agent that ran this session, identify the PRIMARY workstream the session
worked on, then resolve its folder:

1. Determine the active workstream (the one this session actually worked on). If
   the session touched more than one, pick the primary one; if it is genuinely
   ambiguous, ask the user which thread to file the handoff under.
2. Resolve that workstream's folder path. Prefer `${MEMORY_DIR}/MAP.md`: the
   workspace scanner writes one bullet per workstream under the "Active
   workstreams" / "Dormant / completed workstreams" sections, each of the form
   `- **[Display Name](relative/path/)** - `workstream_id` - aliases: ...`. Find
   the bullet whose backtick-wrapped id equals the active `workstream_id`, and
   read its folder from that same line's Markdown link target `(relative/path/)`
   (the path is relative to `MEMORY_DIR`). If MAP is stale/missing or the id is
   not present there, fall back to the `roots:` listed in
   `memory/workstream_config.yml` (each root is a folder under `MEMORY_DIR`; the
   workstream is a child folder whose `README.md` carries a matching
   `workstream_id`).
3. Set `HANDOFF_DIR` = `<workstream_folder>/hand-offs/`. Create it if it does
   not exist (`mkdir -p`). This is the ONLY location the handoff is written to.

If no workstream can be resolved (empty/placeholder memory tree), STOP and report:
> ERROR: no active workstream resolved; cannot compute a per-workstream handoff path. Name a workstream or run /init first.

### Step 5: Spawn handoff subagent

Use the Read tool to read the subagent prompt template:

```
<REPO_ROOT>/scripts/has/has-subagent-prompt.md
```

Prepare the template variables:
- `{{TRANSCRIPT_FILES}}`: the file path(s) output by the filter script in Step 3
- `{{MEMORY_DIR}}`: the resolved `MEMORY_DIR` from Step 0
- `{{HANDOFF_DIR}}`: the resolved `HANDOFF_DIR` from Step 4 (the active
  workstream's own `hand-offs/` path - the only place the handoff is written)
- `{{SESSION_DATE}}`: today's date in YYYY-MM-DD format. Run `date +%Y-%m-%d` as
  a plain Bash call (do NOT wrap in `echo` - the `date` prefix is what the
  permission pattern matches)
- `{{SESSION_END}}`: current time in ISO 8601 with timezone. Run `date -Iseconds`
  as a separate plain Bash call
- `{{HANDOFF_FILENAME}}`: compute by checking existing handoffs for today in
  `HANDOFF_DIR` (`YYYY-MM-DD_NN_<descriptor>.md`, next sequence number)
- `{{SCRATCH_DIR}}`: the resolved `SCRATCH_DIR` from Step 0
- `{{USER_NAME}}`: the user's name (the identity the charter was initialized
  with); if unknown, use `the user`
- `{{JIRA_TICKETS_TOUCHED}}`: only relevant when the optional Jira module is
  enabled (see Step 7). A YAML list of ticket keys worked this session (e.g.
  `- PROJECT-42`), or `[]` if none or if the module is off. Passed to HAS as
  confirmatory context only; HAS classifies ticketed items by the leading key
  prefix (`[A-Z]+-\d+`) itself, so this list need not be exhaustive.

Spawn the subagent using the Agent tool:
- subagent_type: "has-handoff" (its definition at `.claude/agents/has-handoff.md`
  pins the model, effort, and the required tools - Read, Write, Bash, Glob,
  Grep, Edit)
- description: "HAS handoff subagent"
- prompt: the template with all variables substituted

Fallback: if the `has-handoff` agent is not found or the spawn fails, spawn a
generic capable subagent with the same substituted prompt, granting Read, Write,
Bash, Glob, Grep, Edit. This is a resilience path only.

Wait for the subagent to complete and read its return value.

### Step 6a: Surface subagent output

Use the Read tool to read:

```
${SCRATCH_DIR}/has-output.txt
```

Do NOT delete this file. It is ephemeral and gets overwritten on the next /wrap run.

### Step 6b: Closing-text constraint

After Step 6a, inspect the literal text of the Read output LINE BY LINE.

**If any line STARTS WITH `ERROR:` or `WARNING:` (case-sensitive, line-prefix matching):**

Your closing reply for this entire /wrap turn MUST be exactly:

> HAS subagent reported issues - see output above. Do NOT proceed without resolving.

Verbatim. No paraphrase. No additions. No quotes around it.

**If NO line starts with `ERROR:` or `WARNING:`:**

You MAY emit a brief one-liner like "Handoff written; see /wrap output above for details." or no closing text at all.

### Step 7: Refresh generated views

**Optional Jira sync (only if the Jira module is enabled).** The Jira module
ships under `modules/jira/` and is OFF by default. Run the sync ONLY when the
module is opted in - `PA_JIRA_ENABLED=1` in the environment OR a
`jira: {enabled: true}` block in `memory/workstream_config.yml`. When enabled,
sync first so the next session sees current ticket data:

```bash
python "<REPO_ROOT>/modules/jira/jira_sync.py"
```

If the sync errors, emit a warning but do NOT fail the pipeline. If the module
is disabled, SKIP this entirely - the core pipeline has no Jira dependency.

**Scanner refresh (always).** Run the workspace scanner so the next session sees
fresh activity data (the handoff just written by the subagent):

```bash
python "<REPO_ROOT>/scripts/workspace_scanner.py"
```

If the scanner errors, emit a warning but do NOT fail the pipeline - the handoff
is already written.

## If the pipeline fails at any step

Fall back to a degraded-mode handoff: write it by hand into the active
workstream's `hand-offs/` directory, following the same frontmatter schema
(`memory/SCHEMAS.md`, "Handoff frontmatter"). A degraded handoff MUST include
`degraded: true` in its frontmatter and a warning banner as its first body line,
then run the scanner refresh (Step 7) so the generated briefing reflects it.

## What this command does NOT do

- Does not update BRIEFING.md directly (the post-handoff scanner refresh in
  Step 7 handles the generated activity block)
- Does not update the top-level memory index
- Does not push to git
- Does not delete the wrap-state scratch file (it persists for concurrent-session
  safety; prune old ones on a schedule)
- Does not delete filter output (the SessionStart hook cleans up the previous
  session's `filtered_*.txt` files at next session start)
