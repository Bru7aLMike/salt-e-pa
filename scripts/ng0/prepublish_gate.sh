#!/usr/bin/env bash
#
# NG-0 pre-publish gate (Salt-e PA).
#
# The maintainer runs this before publication, and the maintainer release
# workflow (.github/workflows/ng0-release-gate.yml) runs the exact same script
# in CI. It enforces NG-0: the published tree ships ZERO of the author's
# personal data.
#
# ---------------------------------------------------------------------------
# PUBLISH MODEL (defines WHERE the authoritative gate run happens)
# ---------------------------------------------------------------------------
# The AUTHORITATIVE gate run happens on the CLEAN EXPORT / publication-candidate
# repo - a single orphan-root commit - immediately before publication, NOT on
# the long-lived dev repo. Step 16 builds that export from TRACKED CONTENT ONLY
# (an orphan-root commit / `git archive` of tracked files, NOT a filesystem copy
# that could sweep in git-ignored files). Tracked-content-only is the property
# that makes this model sound: the thing scanned is exactly the thing published,
# with no ignored files riding along. This gate must be green on that export.
#
# Do NOT expect a green run on the long-lived DEV repo. The git-history tier
# (--git-history --all-refs) stays RED there BY DESIGN: an early development blob
# is preserved in the dev repo's history, and history is never rewritten. That is
# not a failure of the export - the clean orphan-root export has no such history,
# so its history tier is green. "Run it on the dev repo until green" is the wrong
# mental model; run it on the export.
#
# The release-gate workflow runs this same script in CI as POST-MERGE
# defense-in-depth on the published repo. The literal-denylist tier is inherently
# post-merge: it needs the maintainer denylist secret, which can never be exposed
# to an untrusted fork PR, so it cannot run pre-merge on fork contributions.
#
# It runs four sub-checks and is FAIL-LOUD: every sub-check runs even if an
# earlier one fails (so you see the full picture in one pass), a clear per-check
# and overall PASS/FAIL summary is printed, and the script exits 0 ONLY when
# every sub-check passes. Any failure -> non-zero exit.
#
#   1. Full-tree secret + PII scan, maintainer tier (--with-denylist).
#   2. Git-history secret + PII scan across ALL refs + tags, maintainer tier.
#   3. Memory template-lint, correctly scoped (see TEMPLATE-LINT SCOPING below).
#   4. OKF frontmatter conformance (okf_check.py): every tracked, non-exempt
#      Markdown file carries a non-empty top-level `type:` plus a one-line
#      summary. Tree-level only - needs no denylist or secret, so the PR gate
#      runs it too (see .github/workflows/ng0-pr-gate.yml).
#
# ---------------------------------------------------------------------------
# DENYLIST (maintainer tier) - MUST LIVE OUTSIDE THE SCANNED TREE
# ---------------------------------------------------------------------------
# The full scan loads literal personal identifiers from a denylist. That file
# holds the author's REAL identifiers, so it must NEVER sit inside the tree this
# gate scans: if it did, its own terms would trip denylist-literal on the file
# itself, and - worse - excluding it to work around that would leave the single
# most sensitive file unscanned. This gate FAILS LOUD (exit 2) if the resolved
# denylist path is inside the repo tree (see the outside-tree guard below).
# Resolution order:
#   1. $NG0_DENYLIST  - explicit path to the denylist. The release workflow sets
#                       this to a file under RUNNER_TEMP, which is OUTSIDE the
#                       checked-out workspace.
#   2. Default: $HOME/.salt-e-pa/denylist.local.txt - a per-user location OUTSIDE
#                       any repo checkout (cross-platform: $HOME resolves on Linux,
#                       macOS, and Git Bash on Windows). Keep the local denylist
#                       here, never in the working tree.
# The scanner HARD-FAILS (exit 2) if the denylist is missing or loads zero terms,
# so a silently-empty denylist can never green-light a release. That exit 2 is
# surfaced here as a failed check, never swallowed.
#
# ---------------------------------------------------------------------------
# TEMPLATE-LINT SCOPING (load-bearing - do not "simplify" this away)
# ---------------------------------------------------------------------------
# template_lint.py asserts memory files are empty or placeholder-only. But the
# scaffold DELIBERATELY ships some real generic prose that is NOT a template:
#   - CLAUDE.md (repo root) - generic agent instructions, not under memory/.
#   - memory/rules/*.md      - generic, reusable behavioral rules in plain prose.
# Those files are intentional and are gated by the PII scan (checks 1 and 2) plus
# a human read-review, NOT by template_lint. Running template_lint over them
# would red-fail on intentional prose. template_lint.py owns that allowlist now:
# its built-in DEFAULT_ALLOWLIST skips exactly those generic-prose files, so the
# gate runs the SINGLE canonical command `template_lint.py memory/` (identical to
# CONTRIBUTING.md and the PR-gate workflow). One allowlist to maintain, in the
# tool - not a shell case-list duplicated across gates. To add a generic-prose
# rule, add its EXACT path to DEFAULT_ALLOWLIST in template_lint.py. This does
# NOT weaken template_lint: everything else under memory/ is still linted.
#
set -u

# --- Resolve repo root and cd there so tree paths read repo-relative ----------
if REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  :
else
  # Fallback: two levels up from this script (scripts/ng0/ -> repo root).
  REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fi
cd "$REPO_ROOT" || { echo "prepublish_gate: cannot cd to repo root '$REPO_ROOT'" >&2; exit 2; }

NG0_DIR="scripts/ng0"
SCAN="$NG0_DIR/secret_pii_scan.py"
LINT="$NG0_DIR/template_lint.py"
OKF="$NG0_DIR/okf_check.py"

# --- Pick a Python interpreter (python3 in CI, python on Windows) -------------
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "prepublish_gate: no python3/python interpreter found" >&2
  exit 2
fi

# --- Resolve the denylist (default is OUTSIDE any repo tree) -------------------
DENYLIST="${NG0_DENYLIST:-$HOME/.salt-e-pa/denylist.local.txt}"

# --- FAIL-LOUD guard: the denylist MUST live outside the scanned tree ---------
# The scanned tree is the repo root. If the denylist resolves to a path inside
# it, the tree scan would either flag the author's real identifiers on the file
# itself or (the old, reverted bug) exclude the most sensitive file from the
# scan. Refuse to run in that case - this makes the self-scan hazard structurally
# impossible regardless of how NG0_DENYLIST is set.
#
# Containment is decided by CANONICAL PATH SEMANTICS, not by comparing path
# STRINGS. The old textual containment test compared `pwd`-derived strings with a
# case-SENSITIVE prefix check, which a differently-cased in-tree absolute path
# (e.g. f:/... vs F:/...) slipped past on Windows - misclassifying an in-tree
# denylist as OUTSIDE and NOT refusing. We now resolve BOTH the scan root and the
# denylist through physical realpath (os.path.realpath, which also resolves
# symlinks/junctions so a symlink-into-tree alias is caught) and Windows
# case-folding (os.path.normcase), then test containment with os.path.commonpath.
# If the denylist is INSIDE the tree the checker exits 2 -> we REFUSE. Any error
# resolving the path fails CLOSED (we refuse rather than silently downgrade to a
# scan). commonpath raises on different drives (e.g. denylist under $HOME on C:,
# tree on F:) - that is a genuine OUTSIDE case and is treated as outside.
"$PY" -c '
import os, sys
try:
    root = os.path.normcase(os.path.realpath(sys.argv[1]))
    deny = os.path.normcase(os.path.realpath(sys.argv[2]))
    try:
        inside = (os.path.commonpath([root, deny]) == root)
    except ValueError:
        # No common path (e.g. different Windows drives) -> outside the tree.
        inside = False
    sys.exit(2 if inside else 0)
except Exception as exc:  # fail CLOSED - never downgrade to running the scan
    sys.stderr.write("prepublish_gate: denylist path resolution failed: %s\n" % exc)
    sys.exit(3)
' "$REPO_ROOT" "$DENYLIST"
guard_rc=$?
if [ "$guard_rc" -eq 2 ]; then
  echo "prepublish_gate: REFUSING TO RUN - the denylist is INSIDE the scanned tree." >&2
  echo "  denylist : $DENYLIST" >&2
  echo "  scan root: $REPO_ROOT" >&2
  echo "  The denylist holds the author's real identifiers and must live OUTSIDE" >&2
  echo "  the repo. Move it out and point NG0_DENYLIST at it (default location:" >&2
  echo "  \$HOME/.salt-e-pa/denylist.local.txt)." >&2
  exit 2
elif [ "$guard_rc" -ne 0 ]; then
  echo "prepublish_gate: REFUSING TO RUN - could not verify the denylist is outside" >&2
  echo "  the scanned tree (path resolution failed). Failing closed." >&2
  echo "  denylist : $DENYLIST" >&2
  echo "  scan root: $REPO_ROOT" >&2
  exit 2
fi

# The denylist must exist and be readable. A missing/unreadable denylist is a
# hard fail (exit 2), never a silent downgrade to a scan with zero literals. This
# preserves the prior fail-loud behavior for missing parent paths and extends it
# to a missing/unreadable denylist file.
if [ ! -e "$DENYLIST" ]; then
  echo "prepublish_gate: denylist '$DENYLIST' does not exist." >&2
  echo "  Point NG0_DENYLIST at a denylist file OUTSIDE the repo tree, or create" >&2
  echo "  the default location \$HOME/.salt-e-pa/denylist.local.txt." >&2
  exit 2
fi
if [ ! -r "$DENYLIST" ]; then
  echo "prepublish_gate: denylist '$DENYLIST' is not readable." >&2
  exit 2
fi

# --- Template-lint scope: the SINGLE canonical command --------------------------
# `template_lint.py memory/` lints EVERY file under memory/ except the EXACT
# deliberately-generic prose rule files, which template_lint.py's built-in
# DEFAULT_ALLOWLIST skips. That allowlist is the one source of truth (shared with
# CONTRIBUTING.md and .github/workflows/ng0-pr-gate.yml). Everything ELSE under
# memory/ is linted by default - including memory/rules/INDEX.md, a PLACEHOLDER
# template that MUST pass, and any NEW file added under memory/rules/.
TEMPLATE_ROOT="memory"

# --- Run the three checks, aggregating failures -------------------------------
FAILED=0
declare -a RESULTS

record () {  # $1 = label, $2 = rc
  if [ "$2" -eq 0 ]; then
    RESULTS+=("PASS  $1")
  else
    RESULTS+=("FAIL  $1  (exit $2)")
    FAILED=1
  fi
}

echo "=============================================================="
echo "NG-0 pre-publish gate"
echo "repo root : $REPO_ROOT"
echo "python    : $PY"
echo "denylist  : $DENYLIST"
echo "=============================================================="

# 1. Full-tree secret + PII scan (maintainer tier). The denylist lives OUTSIDE
#    the scanned tree (enforced by the outside-tree guard above), so no exclude
#    is needed or honored - the whole tree is scanned, including nothing hidden.
echo
echo "--- [1/4] full-tree secret + PII scan (--with-denylist) -------"
"$PY" "$SCAN" --tree . --with-denylist --denylist "$DENYLIST"
record "tree secret+PII scan (--with-denylist)" "$?"

# 2. Git-history secret + PII scan across all refs + tags (maintainer tier).
echo
echo "--- [2/4] git-history secret + PII scan (--all-refs) ----------"
"$PY" "$SCAN" --git-history --all-refs --with-denylist --denylist "$DENYLIST"
record "git-history secret+PII scan (--all-refs, --with-denylist)" "$?"

# 3. Memory template-lint. The single canonical command: template_lint.py owns
#    the generic-prose allowlist (DEFAULT_ALLOWLIST), so the gate, CONTRIBUTING.md,
#    and the PR-gate workflow all run the exact same invocation.
echo
echo "--- [3/4] memory template-lint (canonical command) -----------"
lint_rc=0
if [ -d "$TEMPLATE_ROOT" ]; then
  "$PY" "$LINT" "$TEMPLATE_ROOT" || lint_rc=$?
else
  echo "prepublish_gate: template root '$TEMPLATE_ROOT' not found" >&2
  lint_rc=2
fi
record "memory template-lint (canonical: template_lint.py memory/)" "$lint_rc"

# 4. OKF frontmatter conformance over the whole tracked tree. Tree-level check
#    with NO denylist/secret dependency, so the fork PR gate runs the identical
#    command. okf_check.py discovers tracked Markdown via `git ls-files`, skips
#    its own runtime-verbatim exemptions, and fails (exit 1) on any file missing
#    a non-empty top-level `type:` plus a one-line summary.
echo
echo "--- [4/4] OKF frontmatter conformance (okf_check.py) ----------"
okf_rc=0
"$PY" "$OKF" . || okf_rc=$?
record "OKF frontmatter conformance (okf_check.py)" "$okf_rc"

# --- Summary ------------------------------------------------------------------
echo
echo "=============================================================="
echo "NG-0 pre-publish gate summary"
echo "--------------------------------------------------------------"
for r in "${RESULTS[@]}"; do
  echo "  $r"
done
echo "--------------------------------------------------------------"
if [ "$FAILED" -eq 0 ]; then
  echo "RESULT: PASS - safe to publish (all NG-0 checks green)."
  echo "=============================================================="
  exit 0
else
  echo "RESULT: FAIL - DO NOT PUBLISH. Resolve the findings above."
  echo "=============================================================="
  exit 1
fi
