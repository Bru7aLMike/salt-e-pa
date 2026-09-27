#!/usr/bin/env bash
# restore-backups.sh - Rehydrate your PA memory tree + working tree on a new
# machine by pulling from two PRIVATE GitHub backup repos.
#
# This is the mirror-IN counterpart of scripts/push-backups.sh (the mirror-OUT
# script). push-backups.sh sends your filled memory and working trees UP to two
# private repos; this script brings them back DOWN into a fresh clone so the
# assistant rehydrates from your own backed-up content. See docs/SELF-RESTORE.md
# for the full restore walkthrough.
#
# NOTHING is hard-coded. Both target paths and both backup remotes resolve with
# the same precedence used across the scaffold. The two remotes have NO default:
# until you set them this script NO-OPS and pulls nothing, so a fresh clone can
# never pull from someone else's account by accident.
#
# Config precedence for each value (first non-empty wins):
#   target paths (env -> tracked config -> repo-relative default):
#     paths.memory_dir    env PA_MEMORY_DIR   default <repo>/memory
#     paths.working_dir   env PA_WORKING_DIR  default <repo root>
#   backup remotes (env -> LOCAL git-ignored config -> none):
#     backup.memory_remote   env PA_MEMORY_BACKUP_REMOTE   required
#     backup.working_remote  env PA_WORKING_BACKUP_REMOTE  required
#
# The backup remotes are NEVER read from the tracked workstream_config.yml: real
# backup URLs must not live in a tracked file. The LOCAL git-ignored config is
# the only file source for your REAL private backup URLs. Default location:
#   memory/backup-remotes.local.yml   (override with PA_BACKUP_CONFIG_FILE)
# It is git-ignored (see .gitignore) and holds a single 'backup:' section:
#   backup:
#     memory_remote: your-user/your-memory-backup
#     working_remote: your-user/your-working-backup
#
# A remote may be given as owner/name (resolved to https://github.com/owner/name.git),
# a full clone URL (https or ssh), or a local filesystem path (used for testing
# the restore mechanism against local stand-in repos).
#
# PRE-RESTORE TASK: clean throwaway credentials out of old handoffs INSIDE the
# backup repos before you rely on a restore. See docs/SELF-RESTORE.md.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="${PA_CONFIG_FILE:-$REPO_ROOT/memory/workstream_config.yml}"
LOCAL_CONFIG_FILE="${PA_BACKUP_CONFIG_FILE:-$REPO_ROOT/memory/backup-remotes.local.yml}"

# Resolve one <section>.<key> from a given YAML config file. Prints nothing
# (empty) if the file, key, or parser is unavailable, or the value is still a
# placeholder token like <MEMORY_BACKUP_REMOTE> - the caller then falls through.
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

# --- Resolve target paths: env -> tracked config -> repo-relative default -----
MEMORY_DIR="${PA_MEMORY_DIR:-}"
[ -z "$MEMORY_DIR" ] && MEMORY_DIR="$(_cfg "$CONFIG_FILE" paths memory_dir)"
[ -z "$MEMORY_DIR" ] && MEMORY_DIR="$REPO_ROOT/memory"

WORKING_DIR="${PA_WORKING_DIR:-}"
[ -z "$WORKING_DIR" ] && WORKING_DIR="$(_cfg "$CONFIG_FILE" paths working_dir)"
[ -z "$WORKING_DIR" ] && WORKING_DIR="$REPO_ROOT"

# --- Resolve backup remotes: env -> LOCAL git-ignored config -> none ----------
# NEVER read from the tracked CONFIG_FILE: real backup URLs must not live in a
# tracked file. Only env vars and the git-ignored LOCAL_CONFIG_FILE are honored.
MEMORY_REMOTE="${PA_MEMORY_BACKUP_REMOTE:-}"
[ -z "$MEMORY_REMOTE" ] && MEMORY_REMOTE="$(_cfg "$LOCAL_CONFIG_FILE" backup memory_remote)"

WORKING_REMOTE="${PA_WORKING_BACKUP_REMOTE:-}"
[ -z "$WORKING_REMOTE" ] && WORKING_REMOTE="$(_cfg "$LOCAL_CONFIG_FILE" backup working_remote)"

# --- Guard: remotes not configured -> do nothing, pull nowhere ----------------
if [ -z "$MEMORY_REMOTE" ] || [ -z "$WORKING_REMOTE" ]; then
  echo "restore-backups: backup remotes are not configured - nothing pulled."
  echo
  echo "Set BOTH remotes before running, either as environment variables:"
  echo "    export PA_MEMORY_BACKUP_REMOTE='your-user/your-memory-backup'"
  echo "    export PA_WORKING_BACKUP_REMOTE='your-user/your-working-backup'"
  echo "or under a 'backup:' section in a LOCAL git-ignored config file:"
  echo "    $LOCAL_CONFIG_FILE"
  echo "        backup:"
  echo "          memory_remote: your-user/your-memory-backup"
  echo "          working_remote: your-user/your-working-backup"
  echo
  echo "Both must point at PRIVATE repos you own. Exiting 0 without pulling."
  exit 0
fi

# Turn a configured remote into a clonable URL. Accepts a local filesystem path
# (used as-is, for testing against stand-in repos), a full clone URL (https/ssh,
# used as-is), or bare owner/name (resolved to a GitHub https URL).
to_url () {   # $1 = remote
  local r="$1"
  if [ -e "$r" ]; then
    echo "$r"
  elif printf '%s' "$r" | grep -qE '://|@[^/]+:'; then
    echo "$r"
  elif printf '%s' "$r" | grep -qE '^[^/[:space:]]+/[^/[:space:]]+$'; then
    echo "https://github.com/$r.git"
  else
    echo "$r"
  fi
}

# Default branch a remote publishes (from its HEAD symref). Falls back to main.
default_branch () {   # $1 = url
  local br
  br="$(git ls-remote --symref "$1" HEAD 2>/dev/null \
        | awk '/^ref:/ {sub("refs/heads/","",$2); print $2; exit}')"
  [ -n "$br" ] && { echo "$br"; return 0; }
  echo "main"
}

# True when a directory is absent or holds no entries.
dir_empty () {   # $1 = path
  [ ! -e "$1" ] && return 0
  [ -z "$(ls -A "$1" 2>/dev/null)" ] && return 0
  return 1
}

# Pull one backup repo into one target directory.
#   - target empty/absent -> full `git clone` (the new-machine path).
#   - target already populated -> add a 'pa-backup' remote, fetch, and check the
#     backup's default-branch content out over the tree (the in-place path used
#     when rehydrating on top of a fresh scaffold clone).
restore_one () {   # $1 = target dir, $2 = configured remote, $3 = label
  local dir="$1" remote="$2" label="$3" url br
  url="$(to_url "$remote")"
  echo "== Restoring $label <- $remote =="
  if dir_empty "$dir"; then
    rm -rf "$dir" 2>/dev/null
    git clone "$url" "$dir" || { echo "!! $label: clone failed." >&2; return 1; }
    echo "  ($label: cloned into $dir)"
    return 0
  fi
  if [ ! -d "$dir/.git" ]; then
    git -C "$dir" init -q || { echo "!! $label: git init failed." >&2; return 1; }
  fi
  git -C "$dir" remote remove pa-backup >/dev/null 2>&1 || true
  git -C "$dir" remote add pa-backup "$url" || { echo "!! $label: could not add remote." >&2; return 1; }
  br="$(default_branch "$url")"
  git -C "$dir" fetch -q pa-backup "$br" || { echo "!! $label: fetch failed." >&2; return 1; }
  git -C "$dir" checkout "pa-backup/$br" -- . || { echo "!! $label: checkout failed." >&2; return 1; }
  echo "  ($label: pulled pa-backup/$br into $dir)"
  return 0
}

echo "== PA restore =="
echo "  memory  dir : $MEMORY_DIR"
echo "  working dir : $WORKING_DIR"
echo

restore_one "$MEMORY_DIR"  "$MEMORY_REMOTE"  memory    || exit 1
restore_one "$WORKING_DIR" "$WORKING_REMOTE" workspace || exit 1

echo
echo "== Content restored. Regenerate the orientation files: =="
echo "    python scripts/deadline_scanner.py"
echo "    python scripts/workspace_scanner.py"
echo "== Done. =="
