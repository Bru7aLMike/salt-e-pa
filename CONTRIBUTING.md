---
type: doc
purpose: Contribution guide and the pre-commit scan that keeps personal data out of the scaffold.
---

# Contributing to Salt-e PA

Salt-e PA is a scaffold: reusable machinery that anyone can fill with their own
life. Contributions extend the machinery. They never carry a life. This guide
states that boundary, the scan you run before every commit, and the two-tier
gate model that enforces it.

New to the project? Read [`README.md`](README.md) for the clone-to-standup path
and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how the pieces fit
together before you change anything.

## The machinery / data boundary

Contributions go to **reusable machinery only**: scripts, structure, templates,
docs, and tests. The machinery is the part every user shares. The content a
user pours into it is theirs alone and must never enter this repository.

Contribute:

- Scripts and script fixes under `scripts/` and `modules/`.
- Structural templates: placeholder-only memory files, config templates, the
  settings template, cron templates.
- Documentation, schema contracts, and tests.

Never commit personal data, whether yours or anyone else's:

- Real memory content - filled `BRIEFING.md`, `MAP.md`, handoffs, workstream
  notes, people profiles, or any narrative about actual work or people.
- A filled `memory/workstream_config.yml`, a filled `CLAUDE.md` charter, or any
  file where `{{PLACEHOLDER}}` / `<PLACEHOLDER>` slots have been replaced with
  real values.
- Identifiers of any kind: names, emails, phone numbers, handles, absolute home
  paths, tenant or domain names, real remote names, API tokens, or credentials.

New or changed memory-shaped files must stay empty or placeholder-only. The
template linter proves it, and this is the single canonical command - the same
one both NG-0 gates run:

```sh
python scripts/ng0/template_lint.py memory/
```

On a clean checkout this exits `0`. The linter has a built-in allowlist for the
deliberately-generic prose rule files (`memory/rules/regular_dashes_only.md`,
`memory/rules/readable_tokens_over_shorthand.md`,
`memory/rules/external_output_results_only.md`), which ship filled on purpose and
are gated by the secret/PII scan plus human review rather than the template lint.
Pass `--no-allowlist` to lint every file including those.

Keep secrets out of the tree entirely. `.env` and `*.local.*` files are
git-ignored; provide credentials through environment variables, never a
committed file.

## Run the leak scan before every commit

Before you commit, run the NG-0 leak scanner over the paths you touched. It
checks for secrets and PII using generic patterns only, with no literal
identifiers loaded, so it is safe to run anywhere.

The scanner's `--tree` flag takes exactly **one** path. Scan each changed file
or directory in its own invocation - do not pass several paths to a single
`--tree`.

```sh
# Scan a single changed file.
python scripts/ng0/secret_pii_scan.py --tree path/to/changed_file --patterns-only

# Scan a changed directory (walks it recursively).
python scripts/ng0/secret_pii_scan.py --tree scripts/ --patterns-only
```

If you changed several unrelated paths, loop the command once per path. Exit
codes: `0` clean, `1` findings, `2` a usage or runtime error. A `1` means the
scanner matched something that looks like a secret or an identifier - clear it
before committing. Run from the repository root so reported paths read as
repo-relative. See `scripts/ng0/README.md` for scan modes, the exact rule set,
and the documented v1 non-goals.

To scan history rather than the working tree (a secret deleted from disk can
still live in a past commit), use git-history mode:

```sh
python scripts/ng0/secret_pii_scan.py --git-history HEAD --patterns-only
```

## The two-tier gate model

NG-0 enforcement is deliberately split into two tiers so the public checks never
depend on secrets and the strict checks never leak the denylist that powers
them.

**Public tier - patterns only, no secrets.** Every public pull request,
including one from a fork, must pass the scanner in `--patterns-only` mode. This
mode loads only the committed generic rules in `patterns.yml` and no literal
identifiers, so it runs correctly in an untrusted CI context that never receives
repository secrets. This is the same command you run locally before committing.
Because it needs no secret, it works identically on your machine, on a fork's
CI, and on a maintainer's machine.

**Maintainer tier - full literal denylist release gate.** Before a release, the
maintainer runs the scanner in its strict mode, which additionally loads literal
identifiers from a denylist that lives OUTSIDE the scanned tree (default
`$HOME/.salt-e-pa/denylist.local.txt`, or an explicit path via `NG0_DENYLIST`;
CI materializes it under `RUNNER_TEMP`) and scans the full git history across all
refs on the protected default branch. The real denylist never ships and is never exposed to fork PRs;
the strict mode hard-fails if the denylist is missing or empty, so the release
gate can never silently pass with no literals loaded.

```sh
# Public tier - what you and every PR run (no secrets required).
python scripts/ng0/secret_pii_scan.py --tree . --patterns-only

# Maintainer tier - release gate (denylist via a CI secret; not for forks).
python scripts/ng0/secret_pii_scan.py --git-history --all-refs --with-denylist --denylist "$NG0_DENYLIST"
```

### Mandatory release-environment setup

The maintainer release gate reads its denylist from a GitHub **environment
secret** (`NG0_DENYLIST`) on an environment named `release`. That environment is
the authoritative control over which runs may read the secret, and it is a repo
setting the workflow YAML cannot enforce on its own. Before the release gate is
trusted, configure it under **Settings -> Environments -> release**:

- **Deployment branches:** restrict to the default branch (`master`) only.
  GitHub then refuses to expose the secret to any run whose ref is not the
  default branch, no matter what the workflow file says.
- **Required reviewers:** at least one trusted maintainer, so a dispatched run
  pauses for human approval before the secret-bearing job starts.

This matters because `workflow_dispatch` runs a workflow file from the ref it is
dispatched against: a collaborator could dispatch a branch whose copy of the
workflow drops the job-level `if:` guard. The environment protection rule is the
control that still holds in that case; the `if:` guard alone only prevents an
accidental mis-dispatch. Without the environment configured, the denylist secret
is not adequately protected.

### Publish model - where the barrier actually is

Publication is not "push to master." The **authoritative** `prepublish_gate.sh`
run happens on the **clean export / publication-candidate repo** - a single
orphan-root commit - immediately **before** publication, not on the long-lived
dev repo. The Step 16 export is **tracked-content-only**: it is built as an
orphan-root commit / `git archive` of the repo's TRACKED files, never a
filesystem copy that could sweep in git-ignored files. That property is what
makes the model sound - the tree that gets scanned is exactly the tree that gets
published, with nothing ignored riding along - and it is a recorded constraint on
Step 16.

Do not expect the gate to be green on the long-lived dev repo: its
`--git-history --all-refs` tier stays RED by design on a preserved early
development blob (history is never rewritten). The clean orphan-root export has
no such history, so it can go fully green. Run the authoritative gate there.

The release-gate workflow that runs on the published repo is **post-merge
defense-in-depth**, not the primary barrier: the literal-denylist tier is
inherently post-merge because it needs the maintainer secret, which can never be
exposed to an untrusted fork PR. The maintainer denylist must live **outside**
any repo checkout (the gate fails loud if it resolves inside the scanned tree);
the release workflow materializes it under `RUNNER_TEMP`, which is outside the
checked-out workspace.

The **policy** above is what every contributor follows now. The **enforced CI
wiring** that runs these tiers automatically ships with the repository: both NG-0
workflows are present under `.github/workflows/` - `ng0-pr-gate.yml` (public-tier
scan on every pull request) and `ng0-release-gate.yml` (maintainer release gate).
Still run the public-tier scan yourself before every commit; it is the same check
the gate will run.
