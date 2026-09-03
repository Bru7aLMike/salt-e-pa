---
type: doc
purpose: Project overview and quick start for the Salt-e PA scaffold.
---

# Salt-e PA

A file-based personal assistant scaffold for Claude Code power users - clone the empty structure, fill it with your own life.

Salt-e PA gives a Claude Code assistant durable, layered memory across sessions plus a small set of deterministic scripts that keep that memory fresh. The repository ships as an empty scaffold: every memory file is a placeholder template, every path is configurable, and no automation runs until you wire it up. You supply the content; the machinery keeps it organized.

This README is the clone-to-standup path. For how the pieces fit together, read `docs/ARCHITECTURE.md`. To contribute machinery back, read `CONTRIBUTING.md`.

## Quick start

The steps below take a fresh clone to a working, empty assistant. Do them in order. Everything is generic - substitute your own paths and names where shown.

Requirements: Python 3.11 or newer with PyYAML (`pip install pyyaml`), Claude Code, and on Windows a Git Bash environment for the shell hooks.

1. Clone the repository.

   ```sh
   git clone <YOUR_FORK_OR_CLONE_URL> salt-e-pa
   cd salt-e-pa
   ```

2. Onboard with `/init` (recommended). Open the repository in Claude Code and run the `/init` command. It interviews you - assistant persona, who you are, your two directory paths, your isolation allow-list, which content areas to keep, and your first workstream - then fills the slots for you: the identity, path, and allow-list slots in `CLAUDE.md`; the section-purpose rows in `memory/MEMORY.md`; the discovery `roots:` in `memory/workstream_config.yml`; a short profile file; and, if you want, your first workstream. It writes only into your clone, fills only empty placeholders, and confirms before overwriting anything you already set. The command definition ships at `.claude/commands/init.md`.

   Prefer to fill by hand? The slots are plain text you can edit directly - this is the fallback path. Two files carry them, written as `{{UPPER_SNAKE}}` or `<UPPER_SNAKE>`:

   - `CLAUDE.md` - the assistant charter. Replace the persona slots (`{{ASSISTANT_NAME}}`, `{{USER_NAME}}`, `{{RELATIONSHIP_STYLE}}`, `{{TONE}}`), the two path slots (`{{MEMORY_DIR}}`, `{{WORKING_DIR}}`), and the allow-list slots (`{{ALLOWED_SERVICES}}`, `{{ALLOWED_REPOS}}`, `{{ALLOWED_PATHS}}`). Prose that is not a slot is generic guidance meant to stay.
   - `memory/workstream_config.yml` - the script configuration. Every key is optional and resolves with a fixed 3-level precedence: an environment variable (the `PA_*` / `HAS_*` name noted per key), then the value here, then a repo-relative default. A placeholder token like `<MEMORY_DIR>` means "unset, use the default"; replace it to pin a value, or delete the line to keep the default. A fresh clone runs with zero edits, so change only what you need.

3. Run the first session. If you onboarded with `/init` above you have already done this - the command runs inside a Claude Code session. Otherwise, open the repository in Claude Code now. The harness auto-loads `CLAUDE.md` (working dir) and `memory/MEMORY.md` (memory dir); the assistant orients from what you have filled in so far, and reports no workstreams until you create one (step 5).

4. Run the two scanners once by hand to generate the orientation files. Run the deadline scan first, then the workspace scan, so the workspace snapshot reflects the freshly computed deadlines.

   ```sh
   python scripts/deadline_scanner.py
   python scripts/workspace_scanner.py
   ```

   The deadline scan writes `memory/DEADLINES.md`; the workspace scan writes `memory/MAP.md`, `memory/INTEGRITY.md`, and the scanner-owned block of `memory/BRIEFING.md`. Over an empty tree they run clean and report no workstreams.

5. Create your first workstream. A workstream is a folder with a `README.md` whose frontmatter declares a `workstream_id`; the scanners discover work by walking the folders listed under `roots:`. Two ordered steps:

   First, tell the scanner where to look. The workspace scanner only walks folders named under `roots:` in `memory/workstream_config.yml`, which ships empty. Add the root folder name(s) you want to hold workstreams, one per line, for example:

   ```yaml
   roots:
     - content/work
   ```

   Then create the workstream under a configured root. Use the bootstrap script rather than writing the folder by hand - it guarantees the frontmatter lands where the scanner reads it. Pass `--root` with a folder you just listed under `roots:` (here, `content/work`):

   ```sh
   python scripts/init_workstream.py --name "My New Thing" --root content/work --dry-run
   python scripts/init_workstream.py --name "My New Thing" --root content/work
   ```

   Preview with `--dry-run` first, then run for real. Because `content/work` is a configured root, re-running the workspace scanner afterward picks the new workstream up. If you create a workstream under a folder that is not listed in `roots:`, the script warns you and the scanner will not discover it until you add that folder to `roots:`.

6. Wire the two SessionStart hooks. `settings.template.json` (repo root) wires only the two generic hooks the scaffold ships: `scripts/has/has-session-start.sh` (records the transcript path so a session-end `/wrap` can find it) and `scripts/inject-unslop.sh` (injects always-active writing-discipline guidance). The scaffold bundles an unslop skill at `skills/unslop/SKILL.md`, so this hook works out of the box - it injects the catalog from the first session with no extra configuration, and you can point `PA_UNSLOP_SKILL` or the `unslop.skill_file` config key at your own skill to override the bundle. Copy the template to `~/.claude/settings.json`, or merge just its `hooks` block into an existing settings file, then replace every `<REPO_ROOT>` with the absolute path of your clone. See `docs/automation.md` for the full walkthrough.

7. Schedule the two daily scans. Point your OS scheduler (cron or equivalent) at the same two scanner entry points so the generated memory files stay fresh. `docs/automation.md` carries a ready-to-edit crontab example and a task-ID prefix convention; both jobs take no arguments and run the workspace scan a few minutes after the deadline scan.

8. Enable optional modules if you want them. The scaffold ships one reference integration - the Jira module under `modules/jira/` - disabled by default. Leave it off and the core runs with zero external dependency. To turn it on, follow `modules/jira/README.md`.

At this point you have an empty but fully wired assistant: filled identity, generated orientation files, one workstream, both hooks active, and both scans scheduled. From here you add content through normal sessions.

## Architecture

Salt-e PA splits into two roots that must never be confused: a **memory directory** holding durable, layered knowledge and a **working directory** holding the charter, scripts, task orchestration, and scratch artifacts. The memory directory has three tiers: an always-on index and config at the root (`MEMORY.md`, `BRIEFING.md`, `MAP.md`, `DEADLINES.md`, and their siblings), a `system/` subtree for the assistant's own machinery (`_internal/`, `rules/`, `workspace/`), and a `content/` subtree for the user's life (areas such as `work/`, `personal/`, and `entrepreneurial/`, each holding workstreams that carry their own per-workstream `hand-offs/`). Files are loaded by when a session needs them, so the assistant reads only what it must.

The core design is the split between hand-authored files (where you and the assistant record knowledge) and generated files (`MAP.md`, `DEADLINES.md`, `INTEGRITY.md`, and the scanner block of `BRIEFING.md`), which two deterministic Python scanners rebuild from the hand-authored source. You never edit a generated file by hand. A session-end handoff pipeline (HAS), driven by the `/wrap` command that ships in `.claude/commands/` (alongside `/init`), turns a raw transcript into a structured handoff, and a 3-level config precedence (env var, then config, then default) keeps every path out of the code.

For the full model - the tier layout, the BRIEFING byte-splice contract, the scanner internals, the HAS pipeline, and the cross-platform support matrix - read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Extending the scaffold

Once you are running, you grow the system in three main ways. All three keep the assistant oriented as long as you follow the conventions the scanners expect.

### Add a behavioral rule

Rules live under `memory/system/rules/`, one file per rule, with an `INDEX.md` that lists them. The scaffold ships three generic rules as worked examples. To add your own:

1. Write a new rule file under `memory/system/rules/` (for example `no_weekend_pings.md`), one rule per file, stating the rule and when it applies.
2. Add a row for it to `memory/system/rules/INDEX.md`. The workspace scanner cross-checks the rules directory against this index and flags any rule that is present in one but not the other, so keep them in sync.
3. If the rule should apply on every session, also inline a one-line summary in `CLAUDE.md` under the always-active rules - the charter is auto-loaded, the full rule file is read on demand.

### Add a content area

`content/` ships three empty area stubs (`work/`, `personal/`, `entrepreneurial/`); you are not limited to them. To add a new area (for example `content/studies/`):

1. Create the area folder under `memory/content/` with its own `INDEX.md`.
2. Add a row for the area to the top index in `memory/MEMORY.md` so the assistant learns it exists at session start.
3. Add the area folder to `roots:` in `memory/workstream_config.yml` so the workspace scanner walks it for workstreams.
4. Create workstreams inside it with `python scripts/init_workstream.py --name "..." --root content/studies`, exactly as for the shipped areas.

The same steps let you retire a shipped area you do not want: remove its row and drop it from `roots:`.

### Produce a handoff

You do not hand-author handoffs. A handoff is produced by the `/wrap` command at the end of a session: it runs the HAS pipeline (filter the transcript, spawn the `has-handoff` subagent, write a structured handoff into the active workstream's own `hand-offs/` folder, then refresh the generated briefing). Run `/wrap` when you finish a session and the next session starts warm. If the pipeline ever fails, the charter's session-end section documents the manual fallback. See `scripts/has/README.md` for the pipeline internals.

## Optional modules

Modules are self-contained, opt-in integrations under `modules/`. They are the reference pattern for wiring an external service into the scaffold: disabled by default, gated behind a single flag, credentials from the environment only, and confined to their own directory. Nothing in the core imports a module, and the core runs with zero module dependency when they are off.

- **Jira** (`modules/jira/`) - pulls ticket state from an Atlassian Cloud Jira instance and runs read-only health checks over the pulled data. Every code path is gated behind an opt-in flag that defaults off, so on a fresh clone no credential is read and no network call is made; both scripts print a notice and exit cleanly. Turn it on and configure it per `modules/jira/README.md`.

## Data-boundary promise

Salt-e PA is built to publish machinery without publishing a life. The scaffold's single hardest non-goal is that the published tree ships **zero** of the author's personal data: every memory file is an empty placeholder template, every path is configurable rather than hard-coded, and no real name, identifier, path, or credential is baked into any script.

That boundary is enforced, not just asserted. `scripts/ng0/` holds a leak-scanning tool (`secret_pii_scan.py`) that scans the tree and the full git history for secrets and PII, and a template linter (`template_lint.py`) that proves memory files contain only empty placeholder scaffolding. The scanner runs in two tiers: a generic `--patterns-only` mode safe to run in untrusted CI (including forked pull requests), and a maintainer-only mode that also loads literal identifiers from a git-ignored denylist that never ships. See `scripts/ng0/README.md` for the tooling and [`CONTRIBUTING.md`](CONTRIBUTING.md) for the boundary rule contributors must follow.

The promise is mutual: the scaffold ships no author data, and when you fill it with yours, that content stays yours. The optional `scripts/push-backups.sh` helper mirrors your filled memory and working trees to **private** repos you own, and no-ops until you configure both remotes, so a fresh clone can never back up to someone else's account by accident.

## License

See [`LICENSE`](LICENSE).
