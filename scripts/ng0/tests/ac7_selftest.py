#!/usr/bin/env python3
"""AC-7 self-test: prove the NG-0 scanner actually catches leaks.

This harness drives secret_pii_scan.py against generated fixtures in every
required combination and asserts the scanner behaved correctly:

  - TREE mode    x  --patterns-only   -> RED, names a secret AND the identifier
  - TREE mode    x  --with-denylist   -> RED, also names the denylist literal
  - HISTORY mode x  --patterns-only   -> RED, names a secret AND the identifier
  - HISTORY mode x  --with-denylist   -> RED, also names the denylist literal
  - a real scan of the tracked scripts/ng0 tree (tests/ INCLUDED) is CLEAN
  - template_lint accepts a valid template and rejects a filled one

FIXTURE MODEL (F2 - no tracked blind spot)
==========================================
Nothing secret-shaped is TRACKED. The planted fixtures are GENERATED at test
time into throwaway temp dirs, and every probe value in this harness is
ASSEMBLED FROM FRAGMENTS at runtime (see the `_a()` helper below), so this file
itself carries NO secret-shaped literal. The whole tracked tree - this harness
included - therefore scans CLEAN under the NG-0 gate with NO fixture exclude, so
the authoritative publish gate has zero blind spots over tracked content. The
scanner no longer default-excludes scripts/ng0/tests/; the only sanctioned
exclude left is .git/ (git internals, never tracked).

Regression scenarios - one per folded review finding (each proves the leak
path that finding named is now CLOSED):

  - C1  --with-denylist fails CLOSED on a missing/empty denylist (non-zero exit)
  - C2  a widened exclude cannot hide data; sanctioned set is locked in code
  - H1  UTF-16 text is decoded + scanned; raw bytes scanned via latin1
  - H2  a secret on a NON-HEAD branch is missed by single-ref, caught by --all-refs
  - H3  base64url, pure-hex, and 2+slash high-entropy tokens are caught
  - H4  unquoted .env / YAML / export secret assignments are caught
  - M2  national/local phone formats are caught by pattern (no denylist)
  - L1  special/unicode filenames parse correctly for reporting AND exclusion

Round-2 residuals:

  - H1-residual  BOM-less UTF-16LE non-ASCII is decoded + scanned via the union
  - H4-residual  JSON object secret assignments ({"client_secret":..}) go RED
  - H3-residual  0x-prefixed hex (bare and JSON-wrapped) goes RED

Round-3/4 scenarios (latin1-always union, no extension trust, invariant, CWD):

  - CR-A  a text leak renamed secret.png / leak.pdf is SCANNED and CAUGHT
  - CR-B  an ASCII secret + one 0x80 junk byte is CAUGHT via the latin1 raw scan
  - INV   scannable_texts() NEVER returns empty for non-empty input
  - CWD   a real scan of scripts/ng0 is clean from a foreign working directory

F2 closure scenario (the previously-excluded path is now scanned):

  - F2  a secret planted under scripts/ng0/tests/ - a non-git subtree AND a real
        git repo - is CAUGHT by a DEFAULT scan (no --no-default-exclude), proving
        the old blind spot over the reserved fixture dir is gone, while .git is
        still pruned.

The history/isolation scenarios run in ISOLATED throwaway git repos (mktemp) so
the staging repo's permanent history stays clean. The scanner it drives exits
NON-ZERO on every leak scenario; this harness exits 0 only when ALL assertions
hold (CI-correct: green means the safety net works), and 1 if any assertion fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("ERROR: PyYAML is required (pip install pyyaml).\n")
    sys.exit(2)

NG0_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(NG0_DIR, "tests")
SCANNER = os.path.join(NG0_DIR, "secret_pii_scan.py")
LINT = os.path.join(NG0_DIR, "template_lint.py")
PATTERNS = os.path.join(NG0_DIR, "patterns.yml")

# The sanctioned exclude set, mirrored from secret_pii_scan.SANCTIONED_EXCLUDES
# (normalized) for the C2 belt assertion. Only .git/ remains sanctioned: the old
# scripts/ng0/tests/ member was removed so the publish gate has no tracked blind
# spot (F2).
SANCTIONED_NORM = {".git"}

SECRET_RULES = (
    "aws-access-key-id", "stripe-secret-key", "generic-secret-assignment",
    "high-entropy-token", "high-entropy-base64url", "high-entropy-hex",
    "high-entropy-base32", "private-key-block", "jwt-token", "slack-token",
    "github-token", "google-api-key",
)
IDENTIFIER_RULE = "email-address"
DENYLIST_RULE = "denylist-literal"

_failures = []


# --------------------------------------------------------------------------- #
# Probe material - ASSEMBLED FROM FRAGMENTS at runtime (F2).
# --------------------------------------------------------------------------- #
def _a(*parts):
    """Join fragments into a probe value at RUNTIME.

    Every secret-shaped probe below is built this way so the TRACKED source of
    this harness carries NO secret-shaped literal - the whole tree scans clean
    under the NG-0 gate with no fixture exclude. The assembled runtime VALUE is
    fully secret-shaped and drives the scanner RED exactly as a real leak would.
    """
    return "".join(parts)


AKIA_KEY = _a("AKIA", "IOSFODNN7", "EXAMPLE")            # AWS example key (fake)
STRIPE_KEY = _a("sk_", "live_", "0123456789abcdefABCDEF")
API_VALUE = _a("Kd9Xm2Qp7Zt4Rw8", "Bn3Lc6Vh1Fj5Gy0", "Ns8Ma2Pq")
B64URL_TOK = _a("Xk7-pQ2_rL9vN4wB8mZ3", "cH6yT1sD5gF0jU-eA_qW7bR")
HEX40_TOK = _a("9f3a7c1e8b06d24f5a9c", "3e70b18d6f42a7c0e9b5")
B64SLASH_TOK = _a("Pq4/Rs7/Tv0Wx3Yz6Ab9", "Cd2Ef5Gh8Ij1Kl4Mn7Op0")
CLIENT_SEC_VAL = _a("aB3xY9zK2m", "Q7pL5wR8tN1vC4")
REFRESH_VAL = _a("ya29aB3xY9zK2m", "Q7pL5wR8tN0")
APIKEY_VAL = _a("Zx8Wq2Lm5R", "p9Tn3Bv6Cy0Hs4")
HEX_0X = _a("0x", HEX40_TOK)
HUNTER = _a("hunter2", "hunter2")
HUNTER_SEC = _a("hunter2", "hunter2", "secret")
EMAIL_JANE = _a("jane.doe", "@", "example.com")
EMAIL_SELF = _a("selftest", "@", "example.invalid")
PHONE_DASH = _a("415", "-555-", "1212")
PHONE_PAREN = _a("(415) ", "555-", "1212")
PHONE_DOT = _a("415", ".555.", "1212")
# A fake literal identifier that matches NO generic pattern (only the denylist
# catches it). Safe to keep as a plain literal - no fragment split needed.
DENY_NAME = "Zaphod Beeblebrox"


# --------------------------------------------------------------------------- #
# Fixture generation - written to throwaway temp dirs at test time.
# --------------------------------------------------------------------------- #
# Fixture bodies are written as TEMPLATES whose secret slots are SHORT non-
# matching placeholder tokens (under the 12-char assignment-value floor, no @,
# no phone shape), then the real fragment-assembled values are substituted at
# runtime. This keeps the tracked source of this harness free of any secret-
# shaped literal even where a secret KEY sits at a line boundary (F2).
def _fill(template, **subs):
    for token, value in subs.items():
        template = template.replace(token, value)
    return template


def _leaky_fixture():
    tmpl = (
        "# FIXTURE - fake leak data for the NG-0 scanner self-test (AC-7)\n\n"
        "Every value below is FAKE and generated at test time.\n\n"
        "Contact email: __EMAIL__\n"
        "Owner: __NAME__\n"
        "AWS access key: __AKIA__\n"
        "Service token: __STRIPE__\n"
        'api_key = "__APIV__"\n'
    )
    return _fill(tmpl, __EMAIL__=EMAIL_JANE, __NAME__=DENY_NAME, __AKIA__=AKIA_KEY,
                 __STRIPE__=STRIPE_KEY, __APIV__=API_VALUE)


def _entropy_fixture():
    tmpl = (
        "# FIXTURE - fake high-entropy tokens (H3)\n\n"
        "base64url token (uses - and _):\n__B64URL__\n\n"
        "pure-hex token (40+ hex chars):\n__HEX__\n\n"
        "standard-base64 token with 2+ slashes:\n__B64SLASH__\n"
    )
    return _fill(tmpl, __B64URL__=B64URL_TOK, __HEX__=HEX40_TOK, __B64SLASH__=B64SLASH_TOK)


def _unquoted_fixture():
    tmpl = (
        "# FIXTURE - fake UNQUOTED secret assignments (H4)\n\n"
        "CLIENT_SECRET=__CSEC__\n"
        "refresh_token: __RTOK__\n"
        "export API_KEY=__APIK__\n"
    )
    return _fill(tmpl, __CSEC__=CLIENT_SEC_VAL, __RTOK__=REFRESH_VAL, __APIK__=APIKEY_VAL)


def _phone_fixture():
    tmpl = (
        "# FIXTURE - fake national/local phone numbers (M2)\n\n"
        "Dashed:        __P1__\n"
        "Parenthesized: __P2__\n"
        "Dotted:        __P3__\n"
    )
    return _fill(tmpl, __P1__=PHONE_DASH, __P2__=PHONE_PAREN, __P3__=PHONE_DOT)


def _write_planted(dest):
    """Write the four planted fixtures into `dest`. Returns a name->path dict."""
    files = {
        "leaky_notes.md": _leaky_fixture(),
        "entropy_tokens.md": _entropy_fixture(),
        "unquoted_assignments.txt": _unquoted_fixture(),
        "phone_numbers.md": _phone_fixture(),
    }
    paths = {}
    for name, content in files.items():
        p = os.path.join(dest, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        paths[name] = p
    return paths


def run(args, cwd=None):
    # Force UTF-8 on the child's stdio so unicode filenames (L1) round-trip
    # identically no matter the host console codepage.
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable] + args, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _scan_content_isolated(filename, content, extra=None):
    """Write one generated fixture into a fresh temp dir and scan it."""
    tmp = tempfile.mkdtemp(prefix="ng0-iso-")
    try:
        dest = os.path.join(tmp, filename)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
        args = [SCANNER, "--tree", tmp, "--patterns-only"]
        if extra:
            args += extra
        return run(args)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _git_out(repo, args):
    proc = subprocess.run(["git", "-C", repo] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("git {0} failed: {1}".format(
            " ".join(args), proc.stderr.decode("utf-8", "replace")))
    return proc.stdout.decode("utf-8", "replace")


def _write_denylist(terms):
    """Write a throwaway denylist in its OWN temp dir (never inside a scanned tree).

    Returns (path, dir). The caller removes `dir` when done. Keeping it out of the
    scanned tree prevents the denylist file itself from producing a match.
    """
    d = tempfile.mkdtemp(prefix="ng0-deny-")
    path = os.path.join(d, "denylist.local.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# throwaway self-test denylist, FAKE terms only\n")
        for term in terms:
            fh.write(term + "\n")
    return path, d


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print("  [{0}] {1}{2}".format(status, label, ("  -- " + detail) if detail and not condition else ""))
    if not condition:
        _failures.append(label)


def names_secret(out):
    return any(rule in out for rule in SECRET_RULES)


def scenario_tree():
    print("Scenario 1: TREE mode, --patterns-only, generated fixtures")
    tmp = tempfile.mkdtemp(prefix="ng0-tree-")
    denylist, denydir = _write_denylist([DENY_NAME])
    try:
        _write_planted(tmp)
        rc, out = run([SCANNER, "--tree", tmp, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("tree/patterns-only exits non-zero", rc == 1, "rc={0}".format(rc))
        check("tree/patterns-only names a SECRET", names_secret(out))
        check("tree/patterns-only names the IDENTIFIER (email)", IDENTIFIER_RULE in out)

        print("Scenario 2: TREE mode, --with-denylist, generated fixtures")
        rc, out = run([SCANNER, "--tree", tmp, "--with-denylist", "--denylist", denylist])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("tree/with-denylist exits non-zero", rc == 1, "rc={0}".format(rc))
        check("tree/with-denylist names a SECRET", names_secret(out))
        check("tree/with-denylist names the IDENTIFIER (email)", IDENTIFIER_RULE in out)
        check("tree/with-denylist names the DENYLIST literal", DENYLIST_RULE in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(denydir, ignore_errors=True)


def scenario_history():
    tmp = tempfile.mkdtemp(prefix="ng0-hist-")
    denylist, denydir = _write_denylist([DENY_NAME])
    try:
        _git(tmp, ["init", "-q"])
        _git(tmp, ["config", "user.email", EMAIL_SELF])
        _git(tmp, ["config", "user.name", "NG0 Selftest"])
        # Commit the planted content, then delete it from the working tree,
        # proving the git-history scan still finds it.
        dest = os.path.join(tmp, "notes.md")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(_leaky_fixture())
        _git(tmp, ["add", "notes.md"])
        _git(tmp, ["commit", "-q", "-m", "plant fake leak"])
        os.remove(dest)  # gone from working tree; still in history
        _git(tmp, ["add", "-A"])
        _git(tmp, ["commit", "-q", "-m", "remove leak from working tree"])

        print("Scenario 3: HISTORY mode, --patterns-only (isolated temp repo, file deleted from tree)")
        rc, out = run([SCANNER, "--git-history", "HEAD", "--patterns-only", "--repo", tmp])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("history/patterns-only exits non-zero", rc == 1, "rc={0}".format(rc))
        check("history/patterns-only names a SECRET", names_secret(out))
        check("history/patterns-only names the IDENTIFIER (email)", IDENTIFIER_RULE in out)

        print("Scenario 4: HISTORY mode, --with-denylist (isolated temp repo)")
        rc, out = run([SCANNER, "--git-history", "HEAD", "--with-denylist",
                       "--denylist", denylist, "--repo", tmp])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("history/with-denylist exits non-zero", rc == 1, "rc={0}".format(rc))
        check("history/with-denylist names a SECRET", names_secret(out))
        check("history/with-denylist names the IDENTIFIER (email)", IDENTIFIER_RULE in out)
        check("history/with-denylist names the DENYLIST literal", DENYLIST_RULE in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(denydir, ignore_errors=True)


def scenario_real_tree_clean():
    print("Scenario 5: a real scan of scripts/ng0 (tests/ INCLUDED) is CLEAN")
    # tests/ is no longer excluded. Because nothing under it is secret-shaped
    # (fixtures are generated at test time; this harness assembles its probes
    # from fragments), a DEFAULT scan of the whole scripts/ng0 tree - the self-
    # test dir included - comes back clean. This is the F2 property: the gate has
    # no tracked blind spot AND the real tree still passes.
    rc, out = run([SCANNER, "--tree", NG0_DIR, "--patterns-only"])
    print(_indent(out))
    print("  scanner exit code = {0}".format(rc))
    check("default scan of scripts/ng0 (tests/ included) is clean", rc == 0, "rc={0}".format(rc))


def scenario_template_lint():
    print("Scenario 6: template_lint accepts a valid template, rejects a filled one")
    tmp = tempfile.mkdtemp(prefix="ng0-tmpl-")
    try:
        good = os.path.join(tmp, "good.md")
        bad = os.path.join(tmp, "bad.md")
        with open(good, "w", encoding="utf-8") as fh:
            fh.write(
                "---\n"
                "# Profile\n\n"
                "## Identity\n"
                "- Name: {{NAME}}\n"
                "- Role: {{ROLE}}\n"
                "- Timezone: <TIMEZONE>\n\n"
                "| Field | Value |\n"
                "| --- | --- |\n"
                "| City | {{CITY}} |\n"
                "<!-- author real values only in a local, git-ignored copy -->\n"
            )
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(
                "# Profile\n\n"
                "- Name: " + DENY_NAME + "\n"        # filled prose value
                "- Email: " + EMAIL_JANE + "\n"      # filled + PII pattern
            )
        rc_good, out_good = run([LINT, good])
        print(_indent(out_good))
        check("template_lint accepts a valid template", rc_good == 0, "rc={0}".format(rc_good))
        rc_bad, out_bad = run([LINT, bad])
        print(_indent(out_bad))
        check("template_lint rejects a filled template", rc_bad == 1, "rc={0}".format(rc_bad))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Regression scenarios - one per folded review finding.
# --------------------------------------------------------------------------- #
def scenario_c1_fail_closed():
    print("Scenario 7 (C1): --with-denylist fails CLOSED on a missing/empty denylist")
    fixdir = tempfile.mkdtemp(prefix="ng0-c1fix-")
    _write_planted(fixdir)
    try:
        # (a) Missing denylist file.
        missing = os.path.join(tempfile.gettempdir(), "ng0-nonexistent-denylist-zzz.txt")
        if os.path.exists(missing):
            os.remove(missing)
        rc, out = run([SCANNER, "--tree", fixdir, "--with-denylist", "--denylist", missing])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("C1 missing denylist -> hard-fail exit 2 (not warn+exit-0)", rc == 2, "rc={0}".format(rc))
        # (b) Present-but-empty denylist (comments/blank lines only).
        tmp = tempfile.mkdtemp(prefix="ng0-c1-")
        try:
            empty = os.path.join(tmp, "empty_denylist.txt")
            with open(empty, "w", encoding="utf-8") as fh:
                fh.write("# comment only, zero real terms\n\n")
            rc2, out2 = run([SCANNER, "--tree", fixdir, "--with-denylist", "--denylist", empty])
            print(_indent(out2))
            print("  scanner exit code = {0}".format(rc2))
            check("C1 empty denylist -> hard-fail exit 2 (fail closed)", rc2 == 2, "rc={0}".format(rc2))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        shutil.rmtree(fixdir, ignore_errors=True)


def scenario_c2_exclude_bypass():
    print("Scenario 8 (C2): a widened exclude cannot hide data; sanctioned set is locked")
    tmp = tempfile.mkdtemp(prefix="ng0-c2-")
    try:
        memdir = os.path.join(tmp, "memory")
        os.makedirs(memdir)
        with open(os.path.join(memdir, "leak.md"), "w", encoding="utf-8") as fh:
            fh.write("Contact: " + EMAIL_JANE + "\n")
        # A contributor tries to hide memory/ by adding it to the exclude set.
        rc, out = run([SCANNER, "--tree", tmp, "--patterns-only", "--exclude", "memory/"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("C2 data under a would-be-excluded memory/ is STILL flagged",
              rc == 1 and IDENTIFIER_RULE in out, "rc={0}".format(rc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # Belt: the committed patterns.yml exclude list must EXACTLY equal the
    # sanctioned set, so an exclude expansion cannot pass review silently.
    with open(PATTERNS, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    committed = set(
        e.replace("\\", "/").strip().rstrip("/") for e in (raw.get("exclude") or [])
    )
    check("C2 committed patterns.yml exclude == sanctioned set",
          committed == SANCTIONED_NORM, "committed={0}".format(sorted(committed)))


def scenario_h1_utf16():
    print("Scenario 9 (H1 + round-4 semantics): UTF-16 text is scanned; every file's raw bytes "
          "are scanned via latin1 (no extension trust, no fail-closed 'unscannable' path)")
    tmp = tempfile.mkdtemp(prefix="ng0-h1-")
    try:
        secret_line = "AWS key " + AKIA_KEY + " here\n"
        with open(os.path.join(tmp, "notepad_bom.md"), "wb") as fh:
            fh.write(b"\xff\xfe" + secret_line.encode("utf-16-le"))       # UTF-16LE + BOM
        with open(os.path.join(tmp, "notepad_nobom.txt"), "wb") as fh:
            fh.write(secret_line.encode("utf-16-le"))                     # UTF-16LE, no BOM
        # A genuine binary blob that carries NO secret pattern under any encoding.
        # Under the latin1-always union it is scanned and passes clean.
        junk = bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]) + \
            bytes([0xff, 0x00, 0xc3, 0x28, 0x00, 0xde, 0xad, 0x00, 0xbe]) * 200
        with open(os.path.join(tmp, "blob.dat"), "wb") as fh:
            fh.write(junk)                                                # not name-trusted
        with open(os.path.join(tmp, "image.png"), "wb") as fh:
            fh.write(junk)                                                # not name-trusted
        rc, out = run([SCANNER, "--tree", tmp, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("H1 UTF-16LE+BOM secret caught (scanned under the union)",
              "notepad_bom.md" in out and names_secret(out), "rc={0}".format(rc))
        check("H1 UTF-16LE no-BOM secret caught", "notepad_nobom.txt" in out)
        check("round-4 secret-free binary blob.dat is SCANNED + passes clean "
              "(no 'unscannable' flag, not skipped)",
              "blob.dat" not in out and "unscannable" not in out)
        check("round-4 secret-free image.png is SCANNED + passes clean "
              "(clean because no secret, NOT because skipped by extension)",
              "image.png" not in out)
        check("round-4 the 'unscannable-binary' rule is gone entirely",
              "unscannable-binary" not in out)
        check("H1 overall exit non-zero (from the two UTF-16 secrets)", rc == 1, "rc={0}".format(rc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_h2_all_refs():
    print("Scenario 10 (H2): side-branch secret missed by single-ref, caught by --all-refs")
    tmp = tempfile.mkdtemp(prefix="ng0-h2-")
    try:
        _git(tmp, ["init", "-q"])
        _git(tmp, ["config", "user.email", EMAIL_SELF])
        _git(tmp, ["config", "user.name", "NG0 Selftest"])
        with open(os.path.join(tmp, "clean.md"), "w", encoding="utf-8") as fh:
            fh.write("nothing to see here\n")
        _git(tmp, ["add", "-A"])
        _git(tmp, ["commit", "-q", "-m", "clean base"])
        base = _git_out(tmp, ["rev-parse", "HEAD"]).strip()
        _git(tmp, ["checkout", "-q", "-b", "leak-branch"])
        with open(os.path.join(tmp, "secret.md"), "w", encoding="utf-8") as fh:
            fh.write("AWS key " + AKIA_KEY + "\n")
        _git(tmp, ["add", "-A"])
        _git(tmp, ["commit", "-q", "-m", "leak on side branch"])
        _git(tmp, ["checkout", "-q", base])  # HEAD back to clean base (detached)
        print("  HEAD is clean; secret lives only on leak-branch")
        rc_single, out_single = run([SCANNER, "--git-history", "HEAD", "--patterns-only", "--repo", tmp])
        print(_indent(out_single))
        print("  single-ref exit = {0}".format(rc_single))
        check("H2 single-ref HEAD scan MISSES the side-branch secret (rc=0)",
              rc_single == 0, "rc={0}".format(rc_single))
        rc_all, out_all = run([SCANNER, "--git-history", "HEAD", "--all-refs",
                               "--patterns-only", "--repo", tmp])
        print(_indent(out_all))
        print("  all-refs exit = {0}".format(rc_all))
        check("H2 --all-refs CATCHES the side-branch secret (rc=1)",
              rc_all == 1 and names_secret(out_all), "rc={0}".format(rc_all))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_h3_entropy():
    print("Scenario 11 (H3): base64url, pure-hex, and 2+slash high-entropy tokens caught")
    rc, out = _scan_content_isolated("entropy_tokens.md", _entropy_fixture())
    print(_indent(out))
    print("  scanner exit code = {0}".format(rc))
    check("H3 exits non-zero", rc == 1, "rc={0}".format(rc))
    check("H3 base64url / 2+slash token caught (high-entropy-base64url)",
          "high-entropy-base64url" in out)
    check("H3 pure-hex token caught (high-entropy-hex)", "high-entropy-hex" in out)


def scenario_h4_unquoted():
    print("Scenario 12 (H4): unquoted .env / YAML / export secret assignments caught")
    rc, out = _scan_content_isolated("unquoted_assignments.txt", _unquoted_fixture())
    print(_indent(out))
    print("  scanner exit code = {0}".format(rc))
    check("H4 exits non-zero", rc == 1, "rc={0}".format(rc))
    check("H4 generic-secret-assignment fires on unquoted forms",
          "generic-secret-assignment" in out)
    check("H4 catches all three unquoted forms",
          out.count("generic-secret-assignment") >= 3,
          "count={0}".format(out.count("generic-secret-assignment")))


def scenario_m2_phone():
    print("Scenario 13 (M2): national/local phone formats caught by pattern (no denylist)")
    rc, out = _scan_content_isolated("phone_numbers.md", _phone_fixture())
    print(_indent(out))
    print("  scanner exit code = {0}".format(rc))
    check("M2 exits non-zero", rc == 1, "rc={0}".format(rc))
    check("M2 phone-number-na fires (patterns-only, no denylist)", "phone-number-na" in out)


def scenario_l1_special_paths():
    print("Scenario 14 (L1): special/unicode filenames parse for reporting AND exclusion")
    # (a) Reporting: a special-named file at repo root holds a secret. Correct
    # `git ls-tree -z` parsing must report it under its true unicode path.
    tmp = tempfile.mkdtemp(prefix="ng0-l1-")
    try:
        _git(tmp, ["init", "-q"])
        _git(tmp, ["config", "user.email", EMAIL_SELF])
        _git(tmp, ["config", "user.name", "NG0 Selftest"])
        with open(os.path.join(tmp, "léaky nóte.md"), "w", encoding="utf-8") as fh:
            fh.write("AWS key " + AKIA_KEY + "\n")
        _git(tmp, ["add", "-A"])
        _git(tmp, ["commit", "-q", "-m", "special-named file"])
        rc, out = run([SCANNER, "--git-history", "HEAD", "--patterns-only", "--repo", tmp])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("L1 special-named file reported with correct unicode path",
              rc == 1 and "léaky nóte.md" in out, "rc={0}".format(rc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # (b) Exclusion: a special-named file inside a .git-named directory must be
    # PRUNED by the single-segment sanctioned exclude, with the unicode name
    # routed correctly. A sibling special-named file outside .git is reported.
    tmp2 = tempfile.mkdtemp(prefix="ng0-l1x-")
    try:
        gitdir = os.path.join(tmp2, ".git")
        os.makedirs(gitdir)
        with open(os.path.join(gitdir, "héld báck.md"), "w", encoding="utf-8") as fh:
            fh.write("AWS key " + AKIA_KEY + "\n")
        with open(os.path.join(tmp2, "shôwn.md"), "w", encoding="utf-8") as fh:
            fh.write("AWS key " + AKIA_KEY + "\n")
        rc2, out2 = run([SCANNER, "--tree", tmp2, "--patterns-only"])
        print(_indent(out2))
        print("  scanner exit code = {0}".format(rc2))
        check("L1 special-named file under .git is PRUNED (not reported)",
              "héld báck.md" not in out2, "rc={0}".format(rc2))
        check("L1 special-named sibling outside .git IS reported",
              rc2 == 1 and "shôwn.md" in out2, "rc={0}".format(rc2))
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Round-2 regression scenarios - one per residual leak path review confirmed.
# --------------------------------------------------------------------------- #
def scenario_h1_residual_widechar():
    print("Scenario 15 (H1-residual): BOM-less UTF-16LE non-ASCII decoded + scanned under the encoding union")
    tmp = tempfile.mkdtemp(prefix="ng0-h1r-")
    try:
        # A real-looking identifier in Cyrillic saved UTF-16LE with NO BOM. Every
        # high byte is 0x04 (never 0x00), so there is NO NUL in the sniff window.
        # The multi-encoding scan-union decodes it under utf-16-le, so with the
        # name in the denylist it is DETECTED.
        with open(os.path.join(tmp, "name.txt"), "wb") as fh:
            fh.write("Иван".encode("utf-16-le"))
        denylist, denydir = _write_denylist(["Иван"])
        try:
            rc, out = run([SCANNER, "--tree", tmp, "--with-denylist", "--denylist", denylist])
        finally:
            shutil.rmtree(denydir, ignore_errors=True)
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("H1-residual BOM-less UTF-16LE Cyrillic name is CAUGHT via the encoding union",
              rc == 1 and "name.txt" in out and DENYLIST_RULE in out, "rc={0}".format(rc))
        check("H1-residual overall exit non-zero", rc == 1, "rc={0}".format(rc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_h4_residual_json():
    print("Scenario 16 (H4-residual): JSON object secret assignments caught")
    tmp = tempfile.mkdtemp(prefix="ng0-h4r-")
    try:
        # Compact JSON objects whose key sits right after `{`. FAKE values
        # (>= 12 chars) so the quoted value class fires.
        with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as fh:
            fh.write(_fill('{"client_secret":"__H__"}\n', __H__=HUNTER))
            fh.write(_fill('{"token":"__H__"}\n', __H__=HUNTER))
        rc, out = run([SCANNER, "--tree", tmp, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("H4-residual JSON object assignments exit non-zero", rc == 1, "rc={0}".format(rc))
        check("H4-residual generic-secret-assignment fires on JSON forms",
              "generic-secret-assignment" in out)
        check("H4-residual catches both JSON object forms",
              out.count("generic-secret-assignment") >= 2,
              "count={0}".format(out.count("generic-secret-assignment")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_h3_residual_0xhex():
    print("Scenario 17 (H3-residual): 0x-prefixed hex caught (bare and JSON-wrapped)")
    tmp = tempfile.mkdtemp(prefix="ng0-h3r-")
    try:
        # 0x + 40 hex, bare and JSON-wrapped. FAKE value.
        with open(os.path.join(tmp, "bare.txt"), "w", encoding="utf-8") as fh:
            fh.write(HEX_0X + "\n")
        with open(os.path.join(tmp, "wrapped.json"), "w", encoding="utf-8") as fh:
            fh.write('{"key":"' + HEX_0X + '"}\n')
        rc, out = run([SCANNER, "--tree", tmp, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("H3-residual 0x-hex exits non-zero", rc == 1, "rc={0}".format(rc))
        check("H3-residual high-entropy-hex fires on 0x-prefixed hex",
              "high-entropy-hex" in out)
        check("H3-residual catches both bare and JSON-wrapped 0x hex",
              out.count("high-entropy-hex") >= 2,
              "count={0}".format(out.count("high-entropy-hex")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Round-3 scenarios - the BOM-less UTF-16 class closed structurally (encoding
# union), the coupled ANSI-log false positive, and the YAML list-item boundary.
# --------------------------------------------------------------------------- #
def scenario_h1_class_encoding_union():
    print("Scenario 18 (H1-class): multi-encoding scan-union catches CJK/secret UTF-16 (LE+BE); ANSI-log UTF-8 NOT over-flagged")
    tmp = tempfile.mkdtemp(prefix="ng0-h1class-")
    try:
        # A CJK name in BOM-less UTF-16LE == 20 5f 70 67. It has no NUL and
        # mis-decodes as printable ASCII ' _pg'. The name lives ONLY in the
        # denylist, so only a correct utf-16 decode can catch it. The encoding
        # union always scans utf-16-le (LE file) / utf-16-be (BE file).
        name = "张杰"
        le_bytes = name.encode("utf-16-le")
        be_bytes = name.encode("utf-16-be")
        assert le_bytes == bytes([0x20, 0x5f, 0x70, 0x67]), le_bytes
        with open(os.path.join(tmp, "zhang_le.txt"), "wb") as fh:
            fh.write(le_bytes)
        with open(os.path.join(tmp, "zhang_be.txt"), "wb") as fh:
            fh.write(be_bytes)
        denylist, denydir = _write_denylist([name])
        try:
            rc, out = run([SCANNER, "--tree", tmp, "--with-denylist", "--denylist", denylist])
        finally:
            shutil.rmtree(denydir, ignore_errors=True)
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("H1-class CJK BOM-less UTF-16LE (20 5f 70 67) is CAUGHT",
              rc == 1 and "zhang_le.txt" in out and DENYLIST_RULE in out, "rc={0}".format(rc))
        check("H1-class CJK BOM-less UTF-16BE is CAUGHT",
              "zhang_be.txt" in out, "rc={0}".format(rc))

        # A planted SECRET in UTF-16LE must be caught BY PATTERN (no denylist).
        tmp2 = tempfile.mkdtemp(prefix="ng0-h1class-sec-")
        try:
            with open(os.path.join(tmp2, "secret_u16.txt"), "wb") as fh:
                fh.write(("AWS key " + AKIA_KEY + " here\n").encode("utf-16-le"))
            rc2, out2 = run([SCANNER, "--tree", tmp2, "--patterns-only"])
            print(_indent(out2))
            print("  scanner exit code = {0}".format(rc2))
            check("H1-class secret in UTF-16LE caught by pattern (patterns-only)",
                  rc2 == 1 and "secret_u16.txt" in out2 and names_secret(out2), "rc={0}".format(rc2))
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # FP guard: a legit UTF-8 log with dense ANSI color escape codes must
        # scan normally - NOT be flagged unscannable.
        tmp3 = tempfile.mkdtemp(prefix="ng0-h1class-ansi-")
        try:
            ansi = "\x1b[31mERR\x1b[0m \x1b[32mOK\x1b[0m \x1b[33mWARN\x1b[0m\n" * 40
            with open(os.path.join(tmp3, "build.log"), "wb") as fh:
                fh.write(ansi.encode("utf-8"))
            rc3, out3 = run([SCANNER, "--tree", tmp3, "--patterns-only"])
            print(_indent(out3))
            print("  scanner exit code = {0}".format(rc3))
            check("H1-class ANSI-colored UTF-8 log is NOT flagged unscannable (FP fixed)",
                  rc3 == 0 and "unscannable" not in out3, "rc={0}".format(rc3))
        finally:
            shutil.rmtree(tmp3, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_h4_yaml_listitem():
    print("Scenario 19 (H4-residual-2): YAML sequence-item secret assignments caught")
    tmp = tempfile.mkdtemp(prefix="ng0-h4yaml-")
    try:
        # YAML list items whose key sits after the sequence prefix '- '. FAKE values.
        with open(os.path.join(tmp, "creds.yml"), "w", encoding="utf-8") as fh:
            fh.write("oauth:\n")
            fh.write(_fill("  - client_secret: __H__\n", __H__=HUNTER))
            fh.write(_fill('  - token: "__H__"\n', __H__=HUNTER))
        rc, out = run([SCANNER, "--tree", tmp, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("H4-YAML list-item assignments exit non-zero", rc == 1, "rc={0}".format(rc))
        check("H4-YAML generic-secret-assignment fires on '- key: value' forms",
              "generic-secret-assignment" in out)
        check("H4-YAML catches both list-item forms (unquoted + quoted)",
              out.count("generic-secret-assignment") >= 2,
              "count={0}".format(out.count("generic-secret-assignment")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Round-4 scenarios - the two delta-3 Criticals from review, the core invariant, and
# the CWD-independence of a real scan.
# --------------------------------------------------------------------------- #
def scenario_cra_extension_bypass():
    print("Scenario 20 (CR-A): a text leak renamed to an allowlisted extension is SCANNED + CAUGHT")
    tmp = tempfile.mkdtemp(prefix="ng0-cra-")
    try:
        # Plaintext email + AWS key in a file named secret.png. Extensions are
        # not trusted: the raw bytes are scanned like any file. A .pdf proves it
        # is not .png-only.
        with open(os.path.join(tmp, "secret.png"), "w", encoding="utf-8") as fh:
            fh.write("Contact " + EMAIL_JANE + "\nAWS key " + AKIA_KEY + "\n")
        with open(os.path.join(tmp, "leak.pdf"), "w", encoding="utf-8") as fh:
            fh.write(_fill("token: __H__\n", __H__=HUNTER_SEC))
        rc, out = run([SCANNER, "--tree", tmp, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("CR-A secret.png (renamed text leak) is CAUGHT, not skipped by extension",
              rc == 1 and "secret.png" in out and names_secret(out), "rc={0}".format(rc))
        check("CR-A email PII inside secret.png is reported", IDENTIFIER_RULE in out)
        check("CR-A a second allowlisted ext (leak.pdf) is also scanned + caught",
              "leak.pdf" in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_crb_mixed_validity():
    print("Scenario 21 (CR-B): an ASCII secret + one 0x80 byte is CAUGHT via the latin1 raw scan")
    tmp = tempfile.mkdtemp(prefix="ng0-crb-")
    try:
        # (a) ASCII AWS key + trailing 00 80. UTF-8 strict decode fails on 0x80;
        # the wide decodes land on mojibake. latin1 decodes the whole blob and
        # preserves the ASCII key 1:1 -> caught.
        with open(os.path.join(tmp, "probe.txt"), "wb") as fh:
            fh.write(AKIA_KEY.encode("ascii") + bytes([0x00, 0x80]))
        # (b) UTF-8 fails mid-content AND the byte count is odd, so EVERY wide
        # decode also fails - latin1 is the SOLE surviving scanner.
        with open(os.path.join(tmp, "midfail.bin"), "wb") as fh:
            fh.write(EMAIL_JANE.encode("ascii") + bytes([0x80]))
        rc, out = run([SCANNER, "--tree", tmp, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("CR-B ASCII key + 00 80 is CAUGHT via latin1 (was a clean pass)",
              rc == 1 and "probe.txt" in out and names_secret(out), "rc={0}".format(rc))
        check("CR-B odd-length blob where only latin1 survives still catches the email PII",
              "midfail.bin" in out and IDENTIFIER_RULE in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scenario_invariant_raw_scan():
    print("Scenario 22 (INV): scannable_texts() never returns empty for non-empty input "
          "(latin1 always contributes) - no file reaches a clean result unscanned")
    import importlib.util
    spec = importlib.util.spec_from_file_location("secret_pii_scan_inv", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    adversarial = [
        bytes([0x80]),                                   # lone UTF-8-invalid byte
        bytes([0x00, 0x80]),                             # NUL + invalid byte
        AKIA_KEY.encode("ascii") + bytes([0x00, 0x80]),  # CR-B exact probe shape
        bytes([0xff]) * 10,                              # all high bytes
        bytes(range(256)),                               # every byte value once
        os.urandom(64),                                  # random binary
    ]
    all_nonempty = True
    latin1_present = True
    for data in adversarial:
        texts = mod.scannable_texts(data)
        if not texts:
            all_nonempty = False
        if data.decode("latin-1") not in texts:
            latin1_present = False
    print("  probed {0} adversarial byte inputs".format(len(adversarial)))
    check("INV scannable_texts() is non-empty for every non-empty input", all_nonempty)
    check("INV the raw latin1 decode is always among the scanned candidates", latin1_present)
    check("INV empty input yields a single empty candidate", mod.scannable_texts(b"") == [""])


def scenario_cwd_independent():
    print("Scenario 23 (CWD): a real scan of scripts/ng0 is clean from a DIFFERENT working dir")
    # The repo-anchoring for exclusion is discovered from the SCAN ROOT, not the
    # process CWD, so a real scan of scripts/ng0 stays clean from any CWD.
    other_cwd = tempfile.mkdtemp(prefix="ng0-cwd-")
    try:
        rc, out = run([SCANNER, "--tree", NG0_DIR, "--patterns-only"], cwd=other_cwd)
        print(_indent(out))
        print("  scanner exit code = {0} (cwd={1})".format(rc, other_cwd))
        check("CWD scan of scripts/ng0 from a foreign CWD is clean",
              rc == 0, "rc={0}".format(rc))
    finally:
        shutil.rmtree(other_cwd, ignore_errors=True)


def scenario_f2_tests_no_longer_excluded():
    print("Scenario 24 (F2): a secret under scripts/ng0/tests/ is now CAUGHT - the old "
          "reserved-fixture blind spot is closed")
    # (a) Non-git subtree: a decoy at scripts/ng0/tests/leak.md under a DEFAULT
    # scan (no --no-default-exclude) is CAUGHT.
    sub = tempfile.mkdtemp(prefix="ng0-f2-nogit-")
    try:
        leakdir = os.path.join(sub, "scripts", "ng0", "tests")
        os.makedirs(leakdir)
        with open(os.path.join(leakdir, "leak.md"), "w", encoding="utf-8") as fh:
            fh.write("Contact " + EMAIL_JANE + "\nAWS key " + AKIA_KEY + "\n")
        rc, out = run([SCANNER, "--tree", sub, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("F2 (a) non-git decoy at scripts/ng0/tests/leak.md is CAUGHT",
              rc == 1 and "leak.md" in out and names_secret(out), "rc={0}".format(rc))
    finally:
        shutil.rmtree(sub, ignore_errors=True)

    # (b) Real git repo: a secret committed at repo-relative scripts/ng0/tests/
    # (the exact path the scanner USED to exclude) is CAUGHT by a default scan
    # from the repo root, while .git is still pruned.
    repo = tempfile.mkdtemp(prefix="ng0-f2-git-")
    try:
        _git(repo, ["init", "-q"])
        _git(repo, ["config", "user.email", EMAIL_SELF])
        _git(repo, ["config", "user.name", "NG0 Selftest"])
        tdir = os.path.join(repo, "scripts", "ng0", "tests")
        os.makedirs(tdir)
        with open(os.path.join(tdir, "leak.md"), "w", encoding="utf-8") as fh:
            fh.write("AWS key " + AKIA_KEY + "\n")
        _git(repo, ["add", "-A"])
        _git(repo, ["commit", "-q", "-m", "plant secret under tests/"])
        rc, out = run([SCANNER, "--tree", repo, "--patterns-only"])
        print(_indent(out))
        print("  scanner exit code = {0}".format(rc))
        check("F2 (b) secret at repo-relative scripts/ng0/tests/leak.md is CAUGHT from repo root",
              rc == 1 and "scripts/ng0/tests/leak.md" in out, "rc={0}".format(rc))
        check("F2 (b) .git is still pruned (no .git path in findings)", ".git" not in out)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _git(repo, args):
    proc = subprocess.run(["git", "-C", repo] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("git {0} failed: {1}".format(
            " ".join(args), proc.stderr.decode("utf-8", "replace")))


def _indent(text):
    return "\n".join("    " + ln for ln in text.rstrip("\n").split("\n"))


def main():
    print("=" * 70)
    print("AC-7 NG-0 scanner self-test")
    print("=" * 70)
    scenario_tree()
    scenario_history()
    scenario_real_tree_clean()
    scenario_template_lint()
    print("=" * 70)
    print("Regression scenarios (one per folded review finding)")
    print("=" * 70)
    scenario_c1_fail_closed()
    scenario_c2_exclude_bypass()
    scenario_h1_utf16()
    scenario_h2_all_refs()
    scenario_h3_entropy()
    scenario_h4_unquoted()
    scenario_m2_phone()
    scenario_l1_special_paths()
    print("=" * 70)
    print("Round-2 residual scenarios (one per delta-review leak path)")
    print("=" * 70)
    scenario_h1_residual_widechar()
    scenario_h4_residual_json()
    scenario_h3_residual_0xhex()
    print("=" * 70)
    print("Round-3 scenarios (encoding-union H1 closure, ANSI FP guard, YAML boundary)")
    print("=" * 70)
    scenario_h1_class_encoding_union()
    scenario_h4_yaml_listitem()
    print("=" * 70)
    print("Round-4 scenarios (CR-A extension bypass, CR-B mixed-validity, invariant, CWD)")
    print("=" * 70)
    scenario_cra_extension_bypass()
    scenario_crb_mixed_validity()
    scenario_invariant_raw_scan()
    scenario_cwd_independent()
    print("=" * 70)
    print("F2 scenario (previously-excluded scripts/ng0/tests/ is now scanned)")
    print("=" * 70)
    scenario_f2_tests_no_longer_excluded()
    print("-" * 70)
    if _failures:
        print("AC-7 SELF-TEST: FAIL ({0} assertion(s) failed): {1}".format(
            len(_failures), ", ".join(_failures)))
        return 1
    print("AC-7 SELF-TEST: PASS (scanner went RED on every planted leak; "
          "clean where expected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
