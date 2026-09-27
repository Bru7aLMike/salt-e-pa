---
type: doc
purpose: Guide to rehydrating a filled assistant on a new machine from the private backup repos.
---

# Self-restore: rehydrate your assistant on a new machine

This guide is for one specific person: you, the author who has been filling this
scaffold with your own life and mirroring it to two private backup repos with
`scripts/push-backups.sh`. It is the inverse of that script. Where push-backups
sends your memory and working trees UP to two private repos, this flow brings
them back DOWN onto a fresh machine so your assistant rehydrates from your own
content instead of the empty placeholders.

If you have never run `push-backups.sh`, you have no backups to restore and this
guide does not apply. Set up backups first (see `scripts/push-backups.sh` and the
data-boundary section of `README.md`), then come back here when you need them.

## What restore does

A published Salt-e PA clone ships empty: every memory file is a placeholder and
no path is filled. Restore turns that empty clone into your real assistant by
pulling two private repos into the two directories the assistant reads:

- the memory directory (durable memory: the root index and generated files, the
  `system/` machinery subtree, and the `content/` subtree holding your
  workstreams with their per-workstream handoffs), from your private
  memory-backup repo;
- the working directory (charter, scripts, tasks, scratch), from your private
  working-backup repo.

Then the two scanners regenerate the derived orientation files (`DEADLINES.md`,
`MAP.md`, `INTEGRITY.md`, and the scanner block of `BRIEFING.md`) from the
restored source, and the assistant is back where you left it.

Restore never invents remotes. Exactly like push-backups, the two backup remotes
have no default: until you supply both, `scripts/restore-backups.sh` no-ops and
pulls nothing, so a fresh clone can never pull from someone else's account.

## Pre-restore task: clean throwaway credentials out of the backups

Do this before you rely on a restore, not after.

Old session handoffs inside your backup repos can contain throwaway credentials
you pasted during a working session (short-lived tokens, test API keys, one-off
URLs with an embedded secret). Those handoffs were mirrored up as-is, so the same
strings now sit in the backup repos and will come back down on restore.

Before trusting a restore:

1. Scan each backup repo's tree and full history for secrets. The scaffold ships
   the scanner for exactly this:

   ```sh
   python scripts/ng0/secret_pii_scan.py --tree <path-to-backup-checkout> --with-denylist --denylist <your-denylist>
   python scripts/ng0/secret_pii_scan.py --git-history --all-refs --repo <path-to-backup-checkout> --with-denylist --denylist <your-denylist>
   ```

2. Rotate any credential a scan surfaces (assume anything committed is already
   burned), then remove it from the handoff.

3. Because these are private backup repos you own, you can rewrite their history
   to purge a leaked secret. Do that in the backup repo, re-push, and only then
   restore from it.

Treat this as a standing hygiene task on the backups, not a one-time step.

## Restore steps

Everything below runs from the root of a fresh scaffold clone.

1. Clone the empty scaffold and install its requirements (Python 3.11 or newer
   with PyYAML, plus a Git Bash environment on Windows). This gives you the
   restore helper and the scanners.

2. Point the helper at your two private backup repos. Put the real URLs in a
   local, git-ignored config file so they never land in a tracked file. Create
   `memory/backup-remotes.local.yml` (already git-ignored):

   ```yaml
   backup:
     memory_remote: <MEMORY_BACKUP_REMOTE>
     working_remote: <WORKING_BACKUP_REMOTE>
   ```

   Replace each placeholder with your own private repo, given as `owner/name`, a
   full clone URL (https or ssh), or a local path. As an alternative to the file,
   export the same two values as environment variables:

   ```sh
   export PA_MEMORY_BACKUP_REMOTE='<MEMORY_BACKUP_REMOTE>'
   export PA_WORKING_BACKUP_REMOTE='<WORKING_BACKUP_REMOTE>'
   ```

   The helper resolves each remote in this order: environment variable, then the
   local git-ignored file, then nothing. It NEVER reads the remotes from the
   tracked `memory/workstream_config.yml` - real backup URLs must stay out of any
   tracked file, so env vars and the git-ignored local file are the only sources.

3. Choose where the two trees land. By default the helper restores the memory
   repo into `<repo>/memory` and the working repo into the repo root, matching
   the scaffold layout. To restore into other locations (for example the real
   split where the memory directory lives under your Claude projects folder),
   set `PA_MEMORY_DIR` and `PA_WORKING_DIR` to absolute paths first.

4. Run the restore helper:

   ```sh
   bash scripts/restore-backups.sh
   ```

   For each target it either clones the backup repo (when the target is empty) or
   adds a `pa-backup` remote, fetches, and checks the backup's default branch out
   over the tree (when you are rehydrating on top of a populated clone). When the
   remotes are unset it prints a short "configure your remotes" notice and exits
   without pulling.

5. Regenerate the orientation files from the restored source. Run the deadline
   scan first, then the workspace scan, so the workspace snapshot reflects the
   freshly computed deadlines:

   ```sh
   python scripts/deadline_scanner.py
   python scripts/workspace_scanner.py
   ```

6. Start a session. The harness auto-loads the restored `CLAUDE.md` and
   `memory/MEMORY.md`; the assistant orients from your real memory and reports
   your workstreams, deadlines, and latest handoff.

## Related

- `scripts/push-backups.sh` - the mirror-out side that creates and updates the
  two private backups this guide restores from.
- `scripts/restore-backups.sh` - the helper this guide drives.
- `README.md` - the clone-to-standup path for a fresh, empty assistant.
- `docs/automation.md` - wiring the SessionStart hooks and scheduling the scans.
