#!/usr/bin/env python3
"""OKF-check self-test: prove the frontmatter conformance gate enforces its
stated rules, including the one-line-summary requirement.

okf_check.py asserts every tracked, non-exempt Markdown file carries a non-empty
top-level `type:` PLUS a one-line human summary (`purpose:` or `description:`).
The M1 review finding was that a YAML BLOCK SCALAR (`purpose: |` spanning lines)
parsed to a truthy multiline string and slipped past the "one-line" claim. These
scenarios drive okf_check.py over generated fixtures and assert:

  - a valid one-line `purpose:` (and one-line `description:`) passes
  - a BLOCK-SCALAR multiline `purpose:` is REJECTED (the M1 fix)
  - a multiline `description:` is REJECTED
  - a non-scalar summary (a YAML list) is REJECTED
  - an EMPTY `purpose:` falls through to a valid `description:` and passes
  - a missing summary is REJECTED
  - a missing/empty top-level `type:` is REJECTED

Fixtures are GENERATED at test time into throwaway temp dirs (never tracked), so
this harness carries no tracked frontmatter that could gate. okf_check discovers
Markdown via `git ls-files`, falling back to a filesystem walk when the root is
not a git repo (the temp dirs here), so each scenario scans exactly its fixtures.

Run:  python scripts/ng0/tests/okf_check_selftest.py
Exit: 0 only when ALL assertions hold, 1 otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

NG0_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OKF = os.path.join(NG0_DIR, "okf_check.py")

_failures = []


def run(root):
    proc = subprocess.run(
        [sys.executable, OKF, root],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    suffix = ("  -- " + detail) if detail and not condition else ""
    print("  [{0}] {1}{2}".format(status, label, suffix))
    if not condition:
        _failures.append(label)


def _scan_one(filename, body):
    """Write one fixture into a fresh temp dir and scan that dir."""
    tmp = tempfile.mkdtemp(prefix="okf-")
    try:
        with open(os.path.join(tmp, filename), "w", encoding="utf-8") as fh:
            fh.write(body)
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _indent(text):
    return "\n".join("    " + ln for ln in text.rstrip("\n").split("\n"))


def scenario_valid_oneline():
    print("Scenario 1: a valid one-line purpose / description passes")
    rc, out = _scan_one(
        "doc.md",
        "---\ntype: doc\npurpose: A single clear line.\n---\n\nbody\n",
    )
    print(_indent(out))
    check("valid one-line purpose is conformant (exit 0)", rc == 0, "rc={0}".format(rc))

    rc2, out2 = _scan_one(
        "doc.md",
        "---\ntype: doc\ndescription: A single clear line.\n---\n\nbody\n",
    )
    print(_indent(out2))
    check("valid one-line description is conformant (exit 0)", rc2 == 0, "rc={0}".format(rc2))


def scenario_block_scalar_rejected():
    print("Scenario 2 (M1): a multiline BLOCK-SCALAR purpose is REJECTED")
    body = (
        "---\n"
        "type: doc\n"
        "purpose: |\n"
        "  first line\n"
        "  second line\n"
        "---\n\nbody\n"
    )
    rc, out = _scan_one("doc.md", body)
    print(_indent(out))
    check("block-scalar purpose exits non-zero", rc == 1, "rc={0}".format(rc))
    check("block-scalar purpose named as a single-line violation",
          "single line" in out or "one-line" in out,
          "out did not name the one-line violation")


def scenario_multiline_description_rejected():
    print("Scenario 3 (M1): a multiline BLOCK-SCALAR description is REJECTED")
    body = (
        "---\n"
        "type: doc\n"
        "description: >\n"
        "  folded first\n"
        "  folded second\n"
        "\n"
        "  new paragraph\n"
        "---\n\nbody\n"
    )
    # A folded '>' scalar with a blank line yields an embedded newline -> reject.
    rc, out = _scan_one("doc.md", body)
    print(_indent(out))
    check("multiline folded description exits non-zero", rc == 1, "rc={0}".format(rc))
    check("multiline folded description named as a single-line violation",
          "single line" in out or "one-line" in out,
          "out did not name the one-line violation")


def scenario_nonscalar_summary_rejected():
    print("Scenario 4: a non-scalar (list) summary is REJECTED")
    body = (
        "---\n"
        "type: doc\n"
        "purpose:\n"
        "  - one\n"
        "  - two\n"
        "---\n\nbody\n"
    )
    rc, out = _scan_one("doc.md", body)
    print(_indent(out))
    check("list-valued summary exits non-zero", rc == 1, "rc={0}".format(rc))
    check("list-valued summary named as a scalar violation",
          "scalar" in out or "one-line" in out,
          "out did not name the scalar violation")


def scenario_empty_purpose_falls_through():
    print("Scenario 5: an empty purpose falls through to a valid description")
    body = (
        "---\n"
        "type: doc\n"
        'purpose: ""\n'
        "description: A single clear line.\n"
        "---\n\nbody\n"
    )
    rc, out = _scan_one("doc.md", body)
    print(_indent(out))
    check("empty purpose + valid description passes (exit 0)", rc == 0, "rc={0}".format(rc))


def scenario_missing_summary_rejected():
    print("Scenario 6: a missing summary is REJECTED")
    rc, out = _scan_one("doc.md", "---\ntype: doc\n---\n\nbody\n")
    print(_indent(out))
    check("missing summary exits non-zero", rc == 1, "rc={0}".format(rc))
    check("missing summary named", "one-line summary" in out,
          "out did not name the missing summary")


def scenario_missing_type_rejected():
    print("Scenario 7: a missing/empty top-level type is REJECTED")
    rc, out = _scan_one(
        "doc.md", "---\npurpose: A single clear line.\n---\n\nbody\n")
    print(_indent(out))
    check("missing type exits non-zero", rc == 1, "rc={0}".format(rc))
    check("missing type named", "'type'" in out or "type" in out,
          "out did not name the missing type")


def main():
    print("=" * 70)
    print("OKF-check self-test (frontmatter conformance, incl. M1 one-line)")
    print("=" * 70)
    scenario_valid_oneline()
    scenario_block_scalar_rejected()
    scenario_multiline_description_rejected()
    scenario_nonscalar_summary_rejected()
    scenario_empty_purpose_falls_through()
    scenario_missing_summary_rejected()
    scenario_missing_type_rejected()
    print("-" * 70)
    if _failures:
        print("OKF-CHECK SELF-TEST: FAIL ({0} assertion(s)): {1}".format(
            len(_failures), ", ".join(_failures)))
        return 1
    print("OKF-CHECK SELF-TEST: PASS (one-line summary enforced; block scalars "
          "and non-scalars rejected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
