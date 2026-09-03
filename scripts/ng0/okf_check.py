#!/usr/bin/env python3
"""NG-0 OKF frontmatter conformance gate for Salt-e PA.

Asserts that every shipped memory and documentation `.md` advertises a machine-
readable identity: a parseable YAML frontmatter block whose top-level `type:`
field is non-empty, plus a one-line human summary. This is the scaffold's
PARTIAL conformance to the Open Knowledge Format (OKF v0.2): frontmatter +
`type` on every non-reserved `.md`, with the runtime-verbatim exec markdown
exempted (see below). It is orthogonal to the other NG-0 gates:

  - secret_pii_scan.py  catches secrets and PII *values*.
  - template_lint.py    catches *filled* template lines on memory files.
  - okf_check.py        catches *missing* frontmatter identity (this file).

======================================================================
OKF v0.2 conformance, as this tree applies it
======================================================================

The Open Knowledge Format (OKF v0.2) defines three conformance rules:

  1. Every non-reserved `.md` carries a parseable YAML frontmatter block.
  2. Every frontmatter block has a non-empty `type` field. `type` is the ONLY
     required field; all other fields are optional.
  3. Reserved filenames (`index.md`, `log.md`) follow their defined structure
     when present.

This tree advertises PARTIAL OKF conformance and states it honestly:

  - It enforces rules (1) and (2) over every in-scope file: frontmatter must be
    present and parseable, and `type` must be non-empty. `type` is the HARD,
    unconditional requirement of this gate.
  - It ALSO requires a one-line human summary alongside `type`. OKF's
    standardized optional field for that is `description`; this tree standardizes
    on `purpose` for the memory templates and prose docs it authored, and keeps
    the existing `description` on the three output-discipline rule files. So this
    gate accepts EITHER `purpose:` OR `description:` as the summary, and requires
    `type:` unconditionally.
  - Reserved names (rule 3): this tree does NOT ship `index.md` or `log.md`.
    The harness auto-loads the top index by the name MEMORY.md, so the tree
    keeps that name and marks it `type: index` rather than renaming or
    duplicating to `index.md`; this gate asserts MEMORY.md carries `type: index`.
    Likewise it uses MIGRATIONS.md as its single structural-change log instead
    of `log.md`; MIGRATIONS.md just needs a `type:` + summary like any other
    file (it is marked `type: log`).
  - It EXEMPTS executable/prompt/command/agent/skill markdown that a runtime
    reads VERBATIM. Injecting a `type:` line into those would change what the
    runtime consumes, so they carry no OKF frontmatter and are skipped here
    (see EXEMPT). The exemption is documented in the conformance claim, not
    hidden.

The OKF permissive-consumer rule (consumers must not reject a bundle for an
unknown `type` value or an unrecognized key) means this gate never validates the
VALUE of `type` against a closed vocabulary or rejects extra keys - it only
checks that `type` is present and non-empty and that a summary exists.

======================================================================
EXEMPT - runtime-verbatim markdown, never given OKF frontmatter
======================================================================

These files are read verbatim at runtime (as a prompt, command, agent
definition, or skill). Adding a `type:` line would alter what the runtime
ingests, so they are skipped by this gate and ship without OKF frontmatter.

  1. scripts/has/has-subagent-prompt.md  - HAS subagent prompt, read verbatim.
  2. .claude/commands/wrap.md            - slash-command body, read verbatim.
  3. .claude/commands/init.md            - slash-command body, read verbatim.
  4. .claude/agents/has-handoff.md       - agent definition; its frontmatter is
                                           the agent contract (name/model/tools),
                                           not OKF metadata.
  5. skills/unslop/SKILL.md              - vendored third-party MIT skill
                                           (unslop, (c) 2026 Lauren Tan). It is
                                           (a) read verbatim at runtime as a
                                           skill, matching the runtime-verbatim
                                           rationale above, and (b) upstream
                                           third-party content that must not be
                                           modified. It already carries its own
                                           name/description skill frontmatter; a
                                           `type:` MUST NOT be added. This is a
                                           PA-authorized extension of the plan's
                                           four-file exemption list.

Exit codes: 0 = all in-scope files conformant, 1 = one or more violations,
2 = usage / runtime error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write("ERROR: PyYAML is required (pip install pyyaml).\n")
    sys.exit(2)


HERE = os.path.dirname(os.path.abspath(__file__))
# scripts/ng0/ -> scripts/ -> repo root.
DEFAULT_ROOT = os.path.dirname(os.path.dirname(HERE))

MD_EXTENSIONS = (".md", ".markdown")

# Runtime-verbatim exemptions (repo-relative posix paths, matched by suffix so
# the skip holds whether discovery yields repo-relative or absolute paths). See
# the module docstring for the per-file rationale. This list is the SINGLE
# source of truth for the OKF exemption set.
EXEMPT = (
    "scripts/has/has-subagent-prompt.md",
    ".claude/commands/wrap.md",
    ".claude/commands/init.md",
    ".claude/agents/has-handoff.md",
    "skills/unslop/SKILL.md",
)

# Reserved-name substitutes: files this tree keeps under a harness/tool name
# instead of OKF's reserved filename, with the `type` value they must declare.
# MEMORY.md stands in for OKF's reserved `index.md` (the harness auto-loads it by
# that name). MIGRATIONS.md stands in for `log.md` but needs no special type
# beyond a non-empty one, so it is not pinned here.
RESERVED_TYPE = {
    "memory/MEMORY.md": "index",
}

# Accepted one-line summary keys. OKF's standardized field is `description`;
# `purpose` is this tree's convention for the memory templates and prose docs.
SUMMARY_KEYS = ("purpose", "description")


def _rel_suffix_match(path, entry):
    """True if repo-relative `entry` is a path-suffix of `path` (posix, / bound)."""
    p = path.replace(os.sep, "/")
    e = entry.replace(os.sep, "/").strip("/")
    return bool(e) and (p == e or p.endswith("/" + e))


def is_exempt(rel):
    return any(_rel_suffix_match(rel, e) for e in EXEMPT)


def reserved_type_for(rel):
    """Return the required `type` value if `rel` is a reserved-name substitute."""
    for entry, expected in RESERVED_TYPE.items():
        if _rel_suffix_match(rel, entry):
            return expected
    return None


def discover_markdown(root):
    """Return tracked `.md`/`.markdown` files under `root`, repo-relative posix.

    Discovery uses `git ls-files` (like secret_pii_scan.py's history mode uses
    git) so only VERSIONED files are checked - generated caches such as
    .pytest_cache/README.md are never tracked and so never gate. Falls back to a
    filesystem walk (skipping .git) when `root` is not a git repo, so the gate
    still runs on an extracted tarball.
    """
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "*.md", "*.markdown"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, ValueError):
        out = None
    if out is not None and out.returncode == 0:
        listed = out.stdout.decode("utf-8", "replace").splitlines()
        return sorted(p.replace(os.sep, "/") for p in listed if p.strip())

    # Fallback: filesystem walk, .git pruned, paths relative to root.
    found = []
    root_abs = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in MD_EXTENSIONS:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root_abs).replace(os.sep, "/")
                found.append(rel)
    return sorted(found)


def extract_frontmatter(text):
    """Return (data_dict, error). A YAML frontmatter block is `---` ... `---` at
    the very top of the file. Returns ({}, reason) on any structural failure."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, "no YAML frontmatter block (file does not start with '---')"
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            block = "\n".join(lines[1:i])
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError as exc:
                return {}, "unparseable YAML frontmatter: {0}".format(
                    str(exc).replace("\n", " "))
            if data is None:
                return {}, "empty frontmatter block"
            if not isinstance(data, dict):
                return {}, "frontmatter is not a key/value mapping"
            return data, None
    return {}, "unterminated frontmatter block (no closing '---')"


def check_file(path, rel):
    """Return a list of violation strings for one in-scope file (empty = clean)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (IOError, OSError) as exc:
        return ["could not read file: {0}".format(exc)]

    data, err = extract_frontmatter(text)
    if err:
        return [err]

    violations = []
    type_val = data.get("type")
    if type_val is None or str(type_val).strip() == "":
        violations.append("missing or empty top-level 'type' field")
    else:
        expected = reserved_type_for(rel)
        if expected is not None and str(type_val).strip() != expected:
            violations.append(
                "reserved-name file must declare type: {0} (found '{1}')".format(
                    expected, str(type_val).strip()))

    # The summary must be a genuine ONE-LINE value. A non-empty scalar string
    # with no embedded newline satisfies it. A YAML block scalar (`purpose: |`)
    # parses to a multiline str, and a list/mapping parses to a non-str; both
    # bypass the stated one-line requirement, so both are rejected here. An empty
    # value for one key falls through to the next accepted key, preserving the
    # "EITHER purpose OR description" semantics.
    summary_ok = False
    summary_violation = None
    for key in SUMMARY_KEYS:
        if key not in data:
            continue
        val = data.get(key)
        if val is None or str(val).strip() == "":
            continue
        if not isinstance(val, str):
            summary_violation = (
                "summary '{0}' must be a one-line scalar string "
                "(found {1})".format(key, type(val).__name__))
            continue
        if "\n" in val or "\r" in val:
            summary_violation = (
                "summary '{0}' must be a single line "
                "(embedded newline / block scalar not allowed)".format(key))
            continue
        summary_ok = True
        break
    if not summary_ok:
        if summary_violation is not None:
            violations.append(summary_violation)
        else:
            violations.append(
                "missing one-line summary (need non-empty '{0}')".format(
                    "' or '".join(SUMMARY_KEYS)))
    return violations


def build_parser():
    p = argparse.ArgumentParser(
        description="NG-0 OKF frontmatter conformance gate (Salt-e PA).")
    p.add_argument("root", nargs="?", default=DEFAULT_ROOT,
                  help="repo root to scan (default: the repo this script lives in)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        sys.stderr.write("ERROR: root not found: {0}\n".format(args.root))
        return 2

    md_files = discover_markdown(args.root)
    checked = 0
    skipped = 0
    total_violations = 0

    for rel in md_files:
        if is_exempt(rel):
            skipped += 1
            print("OKF-CHECK SKIP {0} (runtime-verbatim exempt)".format(rel))
            continue
        full = os.path.join(args.root, rel)
        checked += 1
        for reason in check_file(full, rel):
            total_violations += 1
            print("OKF-CHECK {0}: {1}".format(rel, reason))

    if total_violations:
        print("OKF-CHECK FAIL: {0} violation(s) across {1} checked file(s), "
              "{2} exempt file(s) skipped".format(
                  total_violations, checked, skipped))
        return 1
    print("OKF-CHECK OK: {0} file(s) conformant, {1} exempt file(s) skipped".format(
        checked, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
