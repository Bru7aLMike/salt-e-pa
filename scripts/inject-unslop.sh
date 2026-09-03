#!/usr/bin/env bash
# SessionStart hook - inject the unslop writing-discipline catalog into context
# so the generation-time "unslop" guidance is always active without a manual
# skill load. With no matcher on the settings.json side it fires on startup,
# resume, and compact, so the catalog is re-injected after a compaction too.
#
# Config resolution (first match wins - same layered pattern as the scanners
# and the HAS hook):
#   1. environment variable  PA_UNSLOP_SKILL
#   2. the unslop.skill_file key in the workstream config YAML
#   3. the bundled skill:  REPO_ROOT/skills/unslop/SKILL.md  (zero-config default)
#   4. a final fallback:   $HOME/.claude/skills/unslop/SKILL.md
#
# The bundled default (3) means a fresh clone injects the shipped writing
# catalog with no env, config, or $HOME setup. An explicit override (1 or 2)
# still wins, and (4) keeps working for anyone whose skill lives under $HOME.
#
# The config file itself is located by PA_CONFIG_FILE, else the repo-relative
# default memory/workstream_config.yml.
#
# No personal paths are baked in. If no skill file resolves the hook exits 0
# silently, so a checkout with the bundle removed never blocks session start.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_FILE="${PA_CONFIG_FILE:-$REPO_ROOT/memory/workstream_config.yml}"

# Resolve one <section>.<key> from the config YAML. Prints nothing (empty) if
# the key is unset, a placeholder token like <PA_UNSLOP_SKILL>, or the file or
# parser is unavailable - the caller then falls through to its default.
_cfg() {
    python - "$CONFIG_FILE" "$1" "$2" <<'PY' 2>/dev/null || true
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

# Skill file path: env -> config -> bundled repo default -> $HOME fallback.
UNSLOP_SKILL="${PA_UNSLOP_SKILL:-}"
if [ -z "$UNSLOP_SKILL" ]; then UNSLOP_SKILL="$(_cfg unslop skill_file)"; fi
if [ -z "$UNSLOP_SKILL" ]; then UNSLOP_SKILL="$REPO_ROOT/skills/unslop/SKILL.md"; fi
if [ -z "$UNSLOP_SKILL" ]; then UNSLOP_SKILL="$HOME/.claude/skills/unslop/SKILL.md"; fi

# Drain the session JSON on stdin; this hook does not need it.
cat >/dev/null 2>&1 || true

# Skill missing -> fail silent, never block session start.
[ -f "$UNSLOP_SKILL" ] || exit 0

python - "$UNSLOP_SKILL" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    body = f.read()
framing = (
    '# Unslop skill (always-active - injected at session start)\n\n'
    'Apply this at generation time, as you write, not as a post-hoc cleanup '
    'pass. The first draft should already avoid these AI tells and carry a '
    'voice. Reach for the /unslop skill only for a deliberate deep audit of an '
    'existing piece.\n\n'
    '---\n\n'
)
ctx = framing + body
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': ctx,
    }
}))
PY
exit 0
