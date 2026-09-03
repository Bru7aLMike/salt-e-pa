#!/usr/bin/env bash
# HAS SessionStart hook - records a per-session scratch file holding the
# transcript path so the /wrap pipeline can locate the transcript at session
# end. Also clears the previous session's filter output.
#
# Claude Code invokes this on session start and pipes a JSON object on stdin:
#   {"session_id": "...", "transcript_path": "..."}
#
# Config resolution (per key, first match wins - same 3-level pattern as the
# scanners):
#   1. environment variable (the HAS_* name below)
#   2. the has: section of the workstream config YAML
#   3. a repo-relative default
#
# Keys:
#   HAS_WRAP_STATE_DIR  has.wrap_state_dir  default: $HOME/.claude/wrap-state
#   HAS_SCRATCH_DIR     has.scratch_dir     default: <repo>/scratch
#   HAS_CONFIG_FILE     (config file path)  default: <repo>/memory/workstream_config.yml
#
# No personal paths are baked in: everything resolves from config or a
# repo-relative default, so the hook runs unedited in a fresh checkout.

set -euo pipefail

# Repo root = two levels up from scripts/has/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CONFIG_FILE="${HAS_CONFIG_FILE:-$REPO_ROOT/memory/workstream_config.yml}"

# Resolve one has.<key> from the config YAML. Prints nothing (empty) if the key
# is unset, a placeholder token like <HAS_SCRATCH_DIR>, or the file/parser is
# unavailable - the caller then falls through to its default. Never fails the
# hook (best-effort, || true at the call site).
_cfg() {
    python - "$CONFIG_FILE" "$1" <<'PY' 2>/dev/null || true
import re, sys
try:
    import yaml
except Exception:
    sys.exit(0)
cfg_path, key = sys.argv[1], sys.argv[2]
try:
    with open(cfg_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
except Exception:
    sys.exit(0)
section = data.get("has") if isinstance(data.get("has"), dict) else {}
val = section.get(key)
if isinstance(val, str) and val.strip() and not re.match(r"^<[A-Z0-9_]+>$", val.strip()):
    print(val.strip())
PY
}

# WRAP_STATE_DIR: env -> config -> default.
WRAP_STATE_DIR="${HAS_WRAP_STATE_DIR:-}"
if [ -z "$WRAP_STATE_DIR" ]; then WRAP_STATE_DIR="$(_cfg wrap_state_dir)"; fi
if [ -z "$WRAP_STATE_DIR" ]; then WRAP_STATE_DIR="$HOME/.claude/wrap-state"; fi

# SCRATCH_DIR: env -> config -> default.
SCRATCH_DIR="${HAS_SCRATCH_DIR:-}"
if [ -z "$SCRATCH_DIR" ]; then SCRATCH_DIR="$(_cfg scratch_dir)"; fi
if [ -z "$SCRATCH_DIR" ]; then SCRATCH_DIR="$REPO_ROOT/scratch"; fi

mkdir -p "$WRAP_STATE_DIR" "$SCRATCH_DIR"

# Clean up the previous session's filter output (unique per session ID, would
# accumulate otherwise).
rm -f "$SCRATCH_DIR"/filtered_*.txt

INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | python -c "import json,sys; print(json.loads(sys.stdin.read()).get('session_id',''))" 2>/dev/null || true)
TRANSCRIPT_PATH=$(echo "$INPUT" | python -c "import json,sys; print(json.loads(sys.stdin.read()).get('transcript_path',''))" 2>/dev/null || true)
SESSION_STARTED=$(date -Iseconds)

if [ -z "$SESSION_ID" ]; then
    echo '{"error": "no session_id in stdin"}' >&2
    exit 0
fi

cat > "$WRAP_STATE_DIR/$SESSION_ID.json" <<SCRATCH
{"session_id": "$SESSION_ID", "transcript_path": "$TRANSCRIPT_PATH", "session_started": "$SESSION_STARTED"}
SCRATCH

exit 0
