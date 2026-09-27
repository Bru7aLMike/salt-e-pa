#!/usr/bin/env python3
"""NG-0 template lint for Salt-e PA memory files.

Asserts that every shipped memory file is EMPTY or contains only template
scaffolding: markdown structure plus placeholder tokens, never filled-in
personal values. This is orthogonal to secret_pii_scan.py:

  - secret_pii_scan.py  catches secrets and PII *values* (patterns + denylist).
  - template_lint.py     catches *filled* template lines (structural discipline),
                         and additionally rejects any line that trips a PII
                         pattern, as a belt-and-suspenders second gate.

Together they enforce NG-0 on the memory tree.

======================================================================
PLACEHOLDER-TOKEN GRAMMAR (v1)  --  Step 3 authors memory files to this
======================================================================

A placeholder token is one of:

  {{UPPER_SNAKE}}     double-curly form   e.g. {{NAME}}, {{USER_EMAIL}}, {{WS_1}}
  <UPPER_SNAKE>       angle form          e.g. <TIMEZONE>, <ROLE>

  UPPER_SNAKE = one or more of A-Z, 0-9, underscore.

A memory file PASSES the lint when it is empty/whitespace-only, OR every
non-blank line is one of:

  1. STRUCTURE  - a markdown structural line carrying no enforceable data
                  payload (see the LABEL-POSITION boundary note below):
       - heading            (#, ##, ... ######)      heading text is free
       - frontmatter fence  (--- on its own line)
       - horizontal rule    (***, ---, ___)
       - table separator    (| --- | --- |)
       - table row          (| ... | ... |)          cells are label positions
       - markdown comment    (<!-- ... -->)
       - blockquote / list marker with empty or placeholder-only content

  2. FIELD      - "LABEL: VALUE" (LABEL may carry list/blockquote/emphasis
                  prefixes; the label text is free) where VALUE is EMPTY or a
                  PLACEHOLDER EXPRESSION.

  3. PLACEHOLDER EXPRESSION line - after stripping any list/blockquote prefix,
                  the whole line is a placeholder expression.

A PLACEHOLDER EXPRESSION is: one or more placeholder tokens joined only by
whitespace and the connective punctuation set  , . ; : / | ( ) [ ] -
It must contain NO bare words, digits, or other letters outside tokens. This is
what stops a real value (a name, a date, an email) from passing: concrete
values are not placeholder tokens.

ADDITIONAL GATE: any line matching a PII pattern from patterns.yml
(email, phone, social handle) is a VIOLATION regardless of the above.

LABEL-POSITION boundary (important): headings, list labels, and table cells are
"label positions." template_lint cannot positionally distinguish a legitimate
column/field label from a filled-in value there (the first table column is
usually a field name, not data), so it treats those positions as structure and
does NOT reject bare words in them. It DOES still run the PII belt on the whole
line. Literal personal *names* sitting in a label position (which match no
generic pattern) are caught by secret_pii_scan.py --with-denylist, not here. The
two tools together enforce NG-0; neither alone is sufficient.

Exit codes: 0 = all files clean, 1 = one or more violations, 2 = usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("ERROR: PyYAML is required (pip install pyyaml).\n")
    sys.exit(2)


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATTERNS = os.path.join(HERE, "patterns.yml")

LINT_EXTENSIONS = (".md", ".markdown", ".yml", ".yaml", ".txt")

# Deliberately-generic prose rule files: shipped FILLED (not templates), so
# template_lint would red-fail on their intentional prose. They are gated by
# secret_pii_scan.py (patterns + denylist) plus human review instead. This
# allowlist is the SINGLE source of truth for that scoping - the documented
# canonical command `template_lint.py memory/` and BOTH NG-0 gates
# (prepublish_gate.sh, .github/workflows/ng0-pr-gate.yml) rely on it, so there is
# one list to maintain, not three. Entries are repo-relative posix paths matched
# by suffix, so the skip holds whether you point at memory/ or the repo root. To
# add a new generic-prose rule, add its EXACT path here. Pass --no-allowlist to
# lint everything (nothing skipped).
DEFAULT_ALLOWLIST = (
    "memory/system/rules/regular_dashes_only.md",
    "memory/system/rules/readable_tokens_over_shorthand.md",
    "memory/system/rules/external_output_results_only.md",
)


def is_allowlisted(path, allowlist):
    """True if `path` is one of the allowlisted generic-prose files (by suffix)."""
    p = os.path.abspath(path).replace(os.sep, "/")
    for entry in allowlist:
        e = entry.replace(os.sep, "/").strip("/")
        if e and (p == e or p.endswith("/" + e)):
            return True
    return False

PLACEHOLDER_TOKEN = re.compile(r"\{\{[A-Z0-9_]+\}\}|<[A-Z0-9_]+>")
# Connective punctuation allowed to glue placeholder tokens together.
CONNECTIVE = set(" \t,.;:/|()[]-")

# OKF frontmatter keys (see scripts/ng0/okf_check.py). An empty-template memory
# file may open with a minimal YAML frontmatter block carrying a top-level
# `type:` plus a one-line human summary (`purpose:`, or the rule files'
# `description:`). Those values are real short scalars, not placeholder tokens,
# so they would otherwise trip the "non-placeholder content" rule. This narrow
# allowlist accepts exactly these keys, and ONLY inside the opening frontmatter
# block (see lint_file). It is a targeted grammar extension: no other key and no
# body line is relaxed, and the PII belt still runs first so a key cannot smuggle
# an identifier.
FRONTMATTER_ALLOWED_KEY = re.compile(r"^(?:type|purpose|description):\s*\S")

HEADING = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
FRONTMATTER_FENCE = re.compile(r"^---\s*$")
HR = re.compile(r"^\s*([*_-])(\s*\1){2,}\s*$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{0,}:?\s*\|?\s*$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
COMMENT_ONLY = re.compile(r"^\s*<!--.*?-->\s*$")
# Leading list bullet or blockquote markers we strip before checking content.
LIST_OR_QUOTE_PREFIX = re.compile(r"^\s*(?:[-*+]\s+|>\s?|\d+\.\s+)+")


def _load_pii_regexes(patterns_path):
    if not os.path.isfile(patterns_path):
        sys.stderr.write("ERROR: patterns file not found: {0}\n".format(patterns_path))
        sys.exit(2)
    with open(patterns_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    out = []
    for item in raw.get("pii_patterns", []) or []:
        name, pattern = item.get("name"), item.get("regex")
        if name and pattern is not None:
            out.append((name, re.compile(pattern)))
    return out


def is_placeholder_expression(text):
    """True if text is only placeholder tokens + connective punctuation."""
    stripped = text.strip()
    if not stripped:
        return True
    remainder = PLACEHOLDER_TOKEN.sub("", stripped)
    if remainder == stripped:
        # No token was present at all -> not a placeholder expression.
        return False
    return all(ch in CONNECTIVE for ch in remainder)


def classify_line(raw_line, pii_regexes, in_frontmatter=False):
    """Return None if the line is allowed, else a short violation reason."""
    line = raw_line.rstrip("\n")
    if not line.strip():
        return None

    # Belt-and-suspenders: any PII pattern anywhere on the line is a violation.
    for name, rx in pii_regexes:
        if rx.search(line):
            return "matches PII pattern '{0}'".format(name)

    # Inside the opening frontmatter block only, allow the OKF conformance keys
    # (type / purpose / description) to carry a real one-line scalar value. The
    # PII belt above has already run, so this cannot pass an identifier.
    if in_frontmatter and FRONTMATTER_ALLOWED_KEY.match(line.strip()):
        return None

    # 1. STRUCTURE lines with no enforceable data payload. Table cells and
    # headings are label positions: template_lint cannot positionally tell a
    # column label from a filled cell, so (like headings) it treats them as
    # structure and leans on the PII belt above plus the denylist scanner for
    # literal identifiers. See the module docstring's boundary note.
    if (HEADING.match(line) or FRONTMATTER_FENCE.match(line) or HR.match(line)
            or TABLE_SEPARATOR.match(line) or TABLE_ROW.match(line)
            or COMMENT_ONLY.match(line)):
        return None

    # Strip a leading list bullet / blockquote / ordered-list prefix.
    body = LIST_OR_QUOTE_PREFIX.sub("", line).strip()
    if not body:
        return None  # bare bullet / blockquote marker

    # 2. FIELD line: LABEL: VALUE  (split on first colon).
    if ":" in body:
        label, _, value = body.partition(":")
        # Guard: a bare word before the colon is a field label; that is fine.
        if is_placeholder_expression(value):
            return None

    # 3. PLACEHOLDER EXPRESSION line (whole body is tokens + connective punct).
    if is_placeholder_expression(body):
        return None

    return "non-placeholder content (filled value or prose)"


def lint_file(path, pii_regexes):
    """Return a list of (line_number, reason) violations for one file."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    if not content.strip():
        return []  # empty / whitespace-only file is always clean
    lines = content.split("\n")
    # Track the opening YAML frontmatter block (if the file starts with a `---`
    # fence) so classify_line can allow the OKF frontmatter keys there and only
    # there. Files without an opening fence never enter frontmatter mode, so
    # behavior is unchanged for them.
    opens_with_fm = bool(lines) and FRONTMATTER_FENCE.match(lines[0].rstrip("\n"))
    in_frontmatter = False
    frontmatter_closed = False
    violations = []
    for i, raw in enumerate(lines, start=1):
        if opens_with_fm and not frontmatter_closed:
            if i == 1:
                in_frontmatter = True
                continue  # opening --- fence: structure, nothing to classify
            if FRONTMATTER_FENCE.match(raw.rstrip("\n")):
                in_frontmatter = False
                frontmatter_closed = True
                continue  # closing --- fence
        reason = classify_line(raw, pii_regexes, in_frontmatter=in_frontmatter)
        if reason:
            violations.append((i, reason, raw.strip()))
    return violations


def collect_targets(root, allowlist=()):
    if os.path.isfile(root):
        return [] if is_allowlisted(root, allowlist) else [root]
    targets = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            if is_allowlisted(full, allowlist):
                continue
            _, ext = os.path.splitext(name)
            if ext.lower() in LINT_EXTENSIONS:
                targets.append(full)
            else:
                # Include otherwise-unknown files only if empty (e.g. .gitkeep)
                # so a stray non-empty unknown file is not silently ignored.
                try:
                    if os.path.getsize(full) > 0:
                        targets.append(full)
                except OSError:
                    pass
    return sorted(targets)


def build_parser():
    p = argparse.ArgumentParser(description="NG-0 memory template lint (Salt-e PA).")
    p.add_argument("path", help="file or directory to lint (e.g. memory/)")
    p.add_argument("--patterns", default=DEFAULT_PATTERNS,
                  help="patterns.yml providing PII rules (default: alongside this script)")
    p.add_argument("--no-allowlist", action="store_true",
                  help="lint EVERY file, including the deliberately-generic prose "
                       "rule files that are allowlisted by default")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not os.path.exists(args.path):
        sys.stderr.write("ERROR: path not found: {0}\n".format(args.path))
        return 2
    pii_regexes = _load_pii_regexes(args.patterns)

    allowlist = () if args.no_allowlist else DEFAULT_ALLOWLIST
    targets = collect_targets(args.path, allowlist)
    total_violations = 0
    for path in targets:
        try:
            rel = os.path.relpath(path).replace(os.sep, "/")
        except ValueError:  # cross-drive on Windows
            rel = os.path.abspath(path).replace(os.sep, "/")
        for line_no, reason, snippet in lint_file(path, pii_regexes):
            total_violations += 1
            print("TEMPLATE-LINT {0}:{1} {2} :: {3}".format(rel, line_no, reason, snippet[:80]))

    scanned = len(targets)
    if total_violations:
        print("TEMPLATE-LINT FAIL: {0} violation(s) across {1} file(s)".format(
            total_violations, scanned))
        return 1
    print("TEMPLATE-LINT OK: {0} file(s) clean".format(scanned))
    return 0


if __name__ == "__main__":
    sys.exit(main())
