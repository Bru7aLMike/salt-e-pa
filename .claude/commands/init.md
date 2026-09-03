Run the guided onboarding interview for a fresh Salt-e PA clone. You interview
the user, then fill their private clone's placeholder slots and (optionally)
create their first workstream. You write ONLY into this clone. Never touch any
other directory, remote, or external service.

## When this command applies (shared trigger-detection rule)

Memory is "empty/placeholder" - and onboarding is needed - when BOTH hold:

1. The auto-loaded charter and index still contain unreplaced `{{UPPER_SNAKE}}`
   slots. Concretely: `CLAUDE.md` still shows slots such as `{{ASSISTANT_NAME}}`,
   `{{USER_NAME}}`, `{{MEMORY_DIR}}`, or `{{WORKING_DIR}}`, and/or
   `memory/MEMORY.md` still shows `{{SECTION_PURPOSE}}` rows.
2. No discoverable workstream exists. A workstream is a folder with a `README.md`
   whose frontmatter carries a top-level `workstream_id`, found under a folder
   listed in `roots:` in `memory/workstream_config.yml` (the `system/` tier or a
   `content/*` area). On a fresh clone `roots:` is empty and none exist.

This is the exact same signal the session-start ritual uses to decide whether to
route a fresh clone here. If the user runs `/init` on an already-populated clone,
do not start over: fill only the gaps and confirm before overwriting anything
that already holds a real value (see "Fill-only discipline" below).

## How to run the interview

Ask the questions below in order, in plain conversation. Group related questions
so the user is not interrogated one line at a time, but do collect every field.
When you have the answers, restate the full plan (which files you will write and
the values you will set) and get a confirmation before writing anything.

1. Assistant persona. What should the assistant be called, what tone should it
   take, and what is the working relationship (peer, aide, coach, etc.)?
   Fills `{{ASSISTANT_NAME}}`, `{{TONE}}`, `{{RELATIONSHIP_STYLE}}` in `CLAUDE.md`.

2. The user. Their name, and one paragraph of profile - role, what they are
   working toward, how they like to work, any standing constraints. The name
   fills `{{USER_NAME}}` in `CLAUDE.md`; the paragraph becomes a short profile
   file under a content area (see step 6 and the write list).

3. Paths. The absolute memory directory and working directory paths for this
   install. FILL the two path slots - do not invent a new folder layout. On a
   self-contained clone both are usually the clone itself (working dir = repo
   root, memory dir = `<repo>/memory`); confirm and use whatever the user gives.
   Fills `{{MEMORY_DIR}}`, `{{WORKING_DIR}}` in `CLAUDE.md`.

4. Isolation allow-list (DEFAULT-DENY). Which external services, which
   repositories, and which extra file paths - if any - is the assistant allowed
   to touch? Everything not named stays denied. It is fine to allow nothing;
   record "none" explicitly rather than leaving a slot open. Fills
   `{{ALLOWED_SERVICES}}`, `{{ALLOWED_REPOS}}`, `{{ALLOWED_PATHS}}` in `CLAUDE.md`.

5. Content areas. The scaffold ships three content areas - `content/work`,
   `content/personal`, `content/entrepreneurial`. Which does the user want to
   keep, and do they want any additional custom areas under `content/`? Keeping
   an area means its `MEMORY.md` row gets a real one-line purpose and its folder
   stays a discovery root; dropping an area means removing its `MEMORY.md` row
   (and, if the user wants, its now-unused folder).

6. First workstream. A name for the first workstream, and which content area it
   belongs to (its discovery root). This drives one `init_workstream.py` call.
   The user may name more than one; create each with its own call. It is also
   valid to defer and create none now.

## Files you write (all inside this clone)

Write only the files below. Preserve top-level frontmatter on every memory-dir
file (see "Write-wrap trap"). Do not run the scanners at the end (see
"Do not run the scanners").

### `CLAUDE.md` (working dir)

Replace the identity, path, and allow-list slots with the interview answers:
`{{ASSISTANT_NAME}}`, `{{USER_NAME}}`, `{{RELATIONSHIP_STYLE}}`, `{{TONE}}`,
`{{MEMORY_DIR}}`, `{{WORKING_DIR}}`, `{{ALLOWED_SERVICES}}`, `{{ALLOWED_REPOS}}`,
`{{ALLOWED_PATHS}}`. Also delete the instructional note at the top of the file
(the blockquote that begins "Fill in the placeholder slots below") once the
slots are filled. Leave every non-slot line of generic guidance untouched.

### `memory/MEMORY.md` (memory dir)

Fill the per-area section-purpose rows in the first table ("Memory Index") only.
For each row, replace `{{SECTION_PURPOSE}}` with a real one-line purpose:

- For each content area the user keeps, write what that area holds for them.
- For an area the user drops, remove that row entirely.
- For a custom content area, add a new row (`content/<area>` in the left cell).
- For the three `system/` rows (`system/workspace`, `system/rules`,
  `system/_internal`), write the standard machinery purpose - these are fixed
  for every install (workspace conventions and how-this-works notes; behavioral
  rules; internal scanner bookkeeping). Do not drop the `system/` rows.

Leave zero `{{SECTION_PURPOSE}}` tokens behind. Do NOT touch the second table,
"Self-architecture legend" - it is architecture-fixed and identical for every
install. This is a direct frontmatter-preserving edit of an existing file.

### `memory/workstream_config.yml` (memory dir)

Set `roots:` to the content areas the user kept, in tier-prefixed form, one per
line, for example:

```yaml
roots:
  - content/work
  - content/personal
```

Add `system` to `roots:` only if the user plans machinery/infra workstreams.
Every first-workstream root you use in step 6 MUST appear here. Fill only
`roots:` (and `exclude:` if the user asked to skip a folder); leave the other
config keys at their shipped placeholder defaults unless the user pins a path.

### A user-profile file under the chosen content area (memory dir)

Write the one-paragraph profile from question 2 to a file such as
`memory/content/personal/user_profile.md` (use the area the user chose for their
profile). Give it top-level YAML frontmatter and a short body, written directly
so the top-level frontmatter is preserved:

```markdown
---
type: profile
purpose: One-paragraph profile of the user.
---

# <User name> - profile

<the one-paragraph profile>
```

### First workstream(s) via `scripts/init_workstream.py` (memory dir)

For each first workstream, call the bootstrap script - never hand-write the
workstream folder. Preview with `--dry-run`, then run for real:

```sh
python scripts/init_workstream.py --name "<Workstream name>" --root content/<area> --dry-run
python scripts/init_workstream.py --name "<Workstream name>" --root content/<area>
```

Pass a `--root` that you just listed under `roots:`. The script writes the
README with a top-level `workstream_id` (Write-wrap safe by construction) and
registers the workstream in `memory/aliases.yml`. If the user deferred, skip
this and tell them to run the script later.

## Fill-only discipline (do not clobber real values)

- Fill placeholders only. A slot is a placeholder when it still reads as
  `{{UPPER_SNAKE}}` or `<UPPER_SNAKE>` (or an empty config value). Replace those.
- For any field that ALREADY holds a real (non-placeholder) value, do NOT
  overwrite it silently. Surface the current value, ask whether to change it,
  and change it only on an explicit yes. On a re-run against a populated clone,
  this means you fill the remaining gaps and leave settled values alone.

## Write-wrap trap (HARD requirement)

Every file you write under the memory directory MUST keep its frontmatter at the
TOP LEVEL. Route memory-dir writes through `scripts/init_workstream.py` (for
workstream READMEs) or a direct frontmatter-preserving write (for `MEMORY.md`,
`workstream_config.yml`, and the profile file). NEVER use any tool or path that
wraps the file in a `name` / `metadata` / `node_type` envelope and nests
`workstream_id` (or any field) under `metadata:`. That envelope silently breaks
scanner discovery: the scanner reads `workstream_id` at the top level only, so a
wrapped README scans as missing its id and the workstream disappears.

## Do not run the scanners

`/init` is a pure write step. Do NOT run `deadline_scanner.py` or
`workspace_scanner.py` at the end. The normal session-start freshness check owns
regenerating `MAP.md`, `DEADLINES.md`, `INTEGRITY.md`, and the `BRIEFING.md`
scanner block; leave that to the next session start.

## Done

Onboarding is complete when: `CLAUDE.md` has no remaining identity, path, or
allow-list `{{SLOTS}}`; `memory/MEMORY.md` has real section-purpose rows and no
`{{SECTION_PURPOSE}}` tokens (legend untouched); `memory/workstream_config.yml`
`roots:` lists the kept content areas; a profile file exists under the chosen
area; and any first workstream is registered in `memory/aliases.yml`. Tell the
user the next step is a normal session start, which will run the scanners and
generate their orientation files.
