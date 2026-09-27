#!/usr/bin/env bash
# push-backups.sh - Commit and push your PA memory tree + working tree to two
# PRIVATE GitHub backup repos.
#
# WHY YOU RUN THIS (not the agent): Claude Code's safety classifier blocks the
# agent from bulk-pushing personal data to external repos. Running it yourself
# keeps the export under your control. Safe to re-run anytime (the first run
# creates each repo via `gh`; later runs just push the new commits).
#
# NOTHING is hard-coded. Repo paths and backup remotes resolve by precedence,
# but the two SOURCES DIFFER on purpose:
#   - repo paths     : environment variable, then the tracked config YAML, then a
#                      repo-relative default.
#   - backup remotes : environment variable, then a LOCAL git-ignored config file,
#                      then NO default. The real backup URLs are secret-ish and
#                      MUST NOT sit in a tracked file, so this script NEVER reads
#                      them from the tracked workstream_config.yml. Until you set
#                      BOTH remotes this script NO-OPS and pushes nowhere, so a
#                      fresh clone can never back up to someone else's account.
#
# Repo paths (tracked memory/workstream_config.yml, or the matching env vars):
#   paths.memory_dir       env PA_MEMORY_DIR            default <repo>/memory
#   paths.working_dir      env PA_WORKING_DIR           default <repo root>
#
# Backup remotes (env vars, or a LOCAL git-ignored config file - NEVER tracked):
#   backup.memory_remote   env PA_MEMORY_BACKUP_REMOTE  required, e.g. your-user/your-memory-backup
#   backup.working_remote  env PA_WORKING_BACKUP_REMOTE required, e.g. your-user/your-working-backup
# The local config defaults to memory/backup-remotes.local.yml (git-ignored;
# override with PA_BACKUP_CONFIG_FILE) and holds a single 'backup:' section.
#
# A pre-push secret guard runs before any push. Primary guard: the scaffold's
# NG-0 scanner (scripts/ng0/secret_pii_scan.py) over each repo's committed
# history. If the scanner cannot run, a built-in git-grep fallback guard runs
# instead, so the guard is never silently skipped. Either guard ABORTS the push
# on a match. Commits are made before the guard, so an abort leaves a local-only
# commit and pushes nothing.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCANNER="$SCRIPT_DIR/ng0/secret_pii_scan.py"
CONFIG_FILE="${PA_CONFIG_FILE:-$REPO_ROOT/memory/workstream_config.yml}"
LOCAL_CONFIG_FILE="${PA_BACKUP_CONFIG_FILE:-$REPO_ROOT/memory/backup-remotes.local.yml}"

# Resolve one <section>.<key> from a given YAML config file. Prints nothing
# (empty) if the key is unset, a placeholder token like <MEMORY_BACKUP_REMOTE>,
# or the file or parser is unavailable - the caller then falls through.
_cfg() {   # $1 = config file, $2 = section, $3 = key
  python - "$1" "$2" "$3" <<'PY' 2>/dev/null || true
import re, sys
try:
    import yaml
except Exception:
    sys.exit(0)
cfg_path, section_key, key = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(cfg_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
except Exception:
    sys.exit(0)
section = data.get(section_key) if isinstance(data.get(section_key), dict) else {}
val = section.get(key)
if isinstance(val, str) and val.strip() and not re.match(r"^<[A-Z0-9_]+>$", val.strip()):
    print(val.strip())
PY
}

# --- Resolve repo paths: env -> tracked config -> repo-relative default -------
MEMORY_DIR="${PA_MEMORY_DIR:-}"
[ -z "$MEMORY_DIR" ] && MEMORY_DIR="$(_cfg "$CONFIG_FILE" paths memory_dir)"
[ -z "$MEMORY_DIR" ] && MEMORY_DIR="$REPO_ROOT/memory"

WORKING_DIR="${PA_WORKING_DIR:-}"
[ -z "$WORKING_DIR" ] && WORKING_DIR="$(_cfg "$CONFIG_FILE" paths working_dir)"
[ -z "$WORKING_DIR" ] && WORKING_DIR="$REPO_ROOT"

# --- Resolve backup remotes: env -> LOCAL git-ignored config -> (no default) ---
# NEVER read from the tracked CONFIG_FILE: real backup URLs must not live in a
# tracked file. Only env vars and the git-ignored LOCAL_CONFIG_FILE are honored.
MEMORY_REMOTE="${PA_MEMORY_BACKUP_REMOTE:-}"
[ -z "$MEMORY_REMOTE" ] && MEMORY_REMOTE="$(_cfg "$LOCAL_CONFIG_FILE" backup memory_remote)"

WORKING_REMOTE="${PA_WORKING_BACKUP_REMOTE:-}"
[ -z "$WORKING_REMOTE" ] && WORKING_REMOTE="$(_cfg "$LOCAL_CONFIG_FILE" backup working_remote)"

# --- Guard: remotes not configured -> do nothing, push nowhere ----------------
if [ -z "$MEMORY_REMOTE" ] || [ -z "$WORKING_REMOTE" ]; then
  echo "push-backups: backup remotes are not configured - nothing pushed."
  echo
  echo "Set BOTH remotes before running, either as environment variables:"
  echo "    export PA_MEMORY_BACKUP_REMOTE='your-user/your-memory-backup'"
  echo "    export PA_WORKING_BACKUP_REMOTE='your-user/your-working-backup'"
  echo "or under a 'backup:' section in a LOCAL git-ignored config file:"
  echo "    $LOCAL_CONFIG_FILE"
  echo "        backup:"
  echo "          memory_remote: your-user/your-memory-backup"
  echo "          working_remote: your-user/your-working-backup"
  echo "The real backup URLs must NEVER go in the tracked workstream_config.yml."
  echo
  echo "Both must point at PRIVATE repos you own. Exiting 0 without pushing."
  exit 0
fi

stamp="$(date +%F)"

commit_if_changed () {   # $1 = repo path, $2 = label
  git -C "$1" add -A
  if git -C "$1" diff --cached --quiet; then
    echo "  ($2: nothing new to commit)"
  else
    git -C "$1" commit -q -m "backup snapshot $stamp"
    echo "  ($2: committed $(git -C "$1" rev-parse --short HEAD))"
  fi
}

# Pre-push secret guard for one repo. Primary: the NG-0 scanner over committed
# history (exit 0 clean, exit 1 findings -> abort, any other exit -> treat the
# scanner as unavailable and fall back). Fallback: inline git-grep over tracked
# content for well-known secret markers. Returns non-zero so the caller aborts
# before pushing.
secret_guard () {        # $1 = repo path, $2 = label
  local rc
  if [ -f "$SCANNER" ] && command -v python >/dev/null 2>&1; then
    if python "$SCANNER" --git-history HEAD --patterns-only --repo "$1"; then
      echo "  ($2: NG-0 history scan clean)"
      return 0
    fi
    rc=$?
    if [ "$rc" -eq 1 ]; then
      echo "!! $2: NG-0 scanner found a secret/PII match in committed history - ABORTING before push." >&2
      return 1
    fi
    echo "  ($2: NG-0 scanner unavailable (exit $rc) - using fallback git-grep guard)" >&2
  fi
  # Fallback: scan tracked content for common secret markers. Structured token
  # prefixes only; no personal identifiers.
  if git -C "$1" grep --cached -lIE 'ATATT[A-Za-z0-9]|sbp_[A-Za-z0-9]{10}|BEGIN [A-Z]+ PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{20}|eyJ[A-Za-z0-9_-]{30,}|re_[A-Za-z0-9]{20}'; then
    echo "!! $2: fallback guard detected a secret in tracked content - ABORTING before push." >&2
    return 1
  fi
  echo "  ($2: fallback git-grep guard clean)"
  return 0
}

push_or_create () {      # $1 = repo path, $2 = owner/name
  if git -C "$1" remote get-url origin >/dev/null 2>&1; then
    git -C "$1" push origin HEAD
  else
    gh repo create "$2" --private --source "$1" --remote origin --push
  fi
}

echo "== Committing =="
commit_if_changed "$MEMORY_DIR"  memory
commit_if_changed "$WORKING_DIR" workspace

echo "== Pre-push secret guard =="
secret_guard "$MEMORY_DIR"  memory    || exit 1
secret_guard "$WORKING_DIR" workspace || exit 1

echo "== Pushing memory    -> $MEMORY_REMOTE (private) =="
push_or_create "$MEMORY_DIR" "$MEMORY_REMOTE"
echo "== Pushing workspace -> $WORKING_REMOTE (private) =="
push_or_create "$WORKING_DIR" "$WORKING_REMOTE"

echo "== Done. Both private backups updated. =="
