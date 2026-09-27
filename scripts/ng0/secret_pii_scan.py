#!/usr/bin/env python3
"""NG-0 secret + PII scanner for Salt-e PA.

Enforces the project's single hardest non-goal (NG-0): the published tree must
ship ZERO of the author's personal data. This scanner is the automated safety
net that proves it.

Two scan targets:
  --tree PATH          scan files on disk under PATH (default ".")
  --git-history [REF]  scan every committed version of every file reachable
                       from REF (default HEAD). A secret deleted from the
                       working tree but left in history STILL leaks; this mode
                       catches it. Add --all-refs to cover ALL local branches
                       and tags (the release-gating mode).

Two rule tiers (additive):
  --patterns-only      load only the committed generic patterns.yml (regex +
                       entropy + PII patterns; ZERO literal personal strings).
                       Safe to run on forked PRs that carry no secrets.
  --with-denylist      ALSO load literal personal identifiers from a git-ignored
                       denylist file (default denylist.local.txt, override with
                       --denylist). Maintainer-only; the denylist never ships.
                       HARD-FAILS if the denylist loads zero terms (fail closed).

Exit codes:
  0  clean (no findings)
  1  one or more findings
  2  usage / runtime error (includes a misconfigured/empty --with-denylist)

Dependencies: Python stdlib + PyYAML (pyyaml) for patterns.yml.
"""

from __future__ import annotations

import argparse
import fnmatch
import math
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "ERROR: PyYAML is required (pip install pyyaml). It parses patterns.yml.\n"
    )
    sys.exit(2)


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATTERNS = os.path.join(HERE, "patterns.yml")
DEFAULT_DENYLIST = "denylist.local.txt"

# Encoding-union scan set (H1 + CR-A + CR-B). Every non-ignored file's RAW BYTES
# are decoded under every candidate encoding below and the findings are UNIONed,
# so a secret / PII / denylist term is caught under at least one decode no matter
# how the other decodes mangle it. There is no encoding-guessing heuristic to
# defeat: the file's true encoding is always among the candidates.
#
# latin1 (ISO-8859-1) is the PERMANENT BACKSTOP and closes two Critical seams:
#   - CR-B (mixed-validity bytes): latin1 NEVER fails to decode - all 256 byte
#     values are valid - and preserves ASCII 1:1. So an ASCII secret is ALWAYS
#     visible in the latin1 view even if one stray byte breaks UTF-8 and every
#     wide decode lands on mojibake. There is no deeper encoding evasion for an
#     ASCII secret past latin1.
#   - Because latin1 always succeeds, no file is ever "undecodable", so there is
#     no fail-closed / extension-allowlist escape hatch to bypass (CR-A). Every
#     file is scanned; extensions are not trusted (see decode-note in _scan_blob).
#
# Over-reporting (a spurious cross-encoding match, including latin1 mojibake of a
# genuine binary) is the SAFE direction for a leak gate. The real skeleton has no
# binaries, so the false-positive cost is nil in practice; if a downstream tree
# carries binaries, an over-report is a prompt to look, never a silent leak.
_SCAN_ENCODINGS = (
    "latin-1",     # ISO-8859-1: ALWAYS decodes (256/256 bytes valid), preserves
                   # ASCII 1:1. Permanent backstop - every ASCII secret is visible
                   # here regardless of other bytes or failed Unicode decodes.
    "utf-8",       # 7-bit ASCII and UTF-8 (a leading BOM decodes to a harmless
                   # U+FEFF and the body still scans)
    "utf-16-le",   # BOM-less UTF-16LE (Windows Notepad); BOM files decode too
    "utf-16-be",   # BOM-less UTF-16BE; BOM files decode too
    "utf-32-le",   # rarer, but cheap and closes the wide-char class fully
    "utf-32-be",
)

# C2: the ONLY excludes the scanner will honor. Locked in code so a contributor
# cannot widen `patterns.yml` `exclude:` (or pass --exclude) to hide personal
# data under, say, memory/ or docs/. Anything outside this set is dropped (with
# a warning) and therefore STILL scanned. Normalized (posix, no trailing slash)
# for comparison in _norm_exclude().
#
# F2: scripts/ng0/tests/ is NO LONGER sanctioned. It was excluded because the
# self-test fixtures are secret-shaped by design, but excluding a TRACKED subtree
# is a publish-gate blind spot: a real secret committed there would ride through.
# The self-test now GENERATES its fixtures at test time and assembles its own
# probe values from fragments, so nothing under tests/ is secret-shaped and the
# whole tracked tree can be scanned with no fixture exclude. Only .git/ (git
# internals, never tracked) remains sanctioned.
SANCTIONED_EXCLUDES = (".git/",)


class Finding:
    """One detected leak: which file, which line, which rule, redacted match."""

    def __init__(self, source, path, line, rule, match):
        self.source = source  # "tree" or "history"
        self.path = path
        self.line = line
        self.rule = rule
        self.match = match

    def format(self):
        loc = self.path if self.line is None else "{0}:{1}".format(self.path, self.line)
        return "FINDING [{0}] {1} rule={2} match={3}".format(
            self.source, loc, self.rule, redact(self.match)
        )


def redact(value):
    """Redact the middle of a matched string so output never prints a full secret."""
    value = value.replace("\n", "\\n").strip()
    if len(value) <= 8:
        head = value[:2]
        return head + "*" * (len(value) - len(head))
    return "{0}...{1} (len={2})".format(value[:4], value[-2:], len(value))


def shannon_entropy(token):
    """Shannon entropy in bits per character."""
    if not token:
        return 0.0
    counts = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(token)
    entropy = 0.0
    for c in counts.values():
        p = c / n
        entropy -= p * math.log2(p)
    return entropy


def _norm_exclude(entry):
    """Normalize an exclude entry to a posix path with no trailing slash."""
    return entry.replace(os.sep, "/").strip().rstrip("/")


_SANCTIONED_NORM = frozenset(_norm_exclude(e) for e in SANCTIONED_EXCLUDES)


def enforce_sanctioned_excludes(excludes):
    """C2: keep only sanctioned excludes; drop (and warn about) anything else.

    An unsanctioned exclude cannot hide data: it is stripped here, so the path
    it named is STILL scanned. Returns the filtered list.
    """
    kept = []
    for entry in excludes:
        if not entry:
            continue
        if _norm_exclude(entry) in _SANCTIONED_NORM:
            kept.append(entry)
        else:
            sys.stderr.write(
                "WARNING: ignoring unsanctioned exclude {0!r}; that path is "
                "STILL scanned (sanctioned excludes: {1}).\n".format(
                    entry, ", ".join(sorted(_SANCTIONED_NORM)))
            )
    return kept


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #
class RuleSet:
    """Compiled rules: named regexes, entropy heuristics, excludes, denylist."""

    def __init__(self, patterns_path, denylist_terms, default_excludes):
        raw = _load_yaml(patterns_path)
        self.named = []  # list of (rule_name, compiled_regex)
        for group in ("secret_patterns", "pii_patterns"):
            for item in raw.get(group, []) or []:
                name = item.get("name")
                pattern = item.get("regex")
                if not name or pattern is None:
                    _fail("patterns.yml: every {0} entry needs name + regex".format(group))
                try:
                    self.named.append((name, re.compile(pattern)))
                except re.error as exc:
                    _fail("patterns.yml: bad regex for rule {0}: {1}".format(name, exc))

        # H3: multi-alphabet entropy heuristics. Each: (name, regex, min_len,
        # threshold). Supports the `heuristics:` list form; falls back to the
        # legacy single-config form for backward compatibility.
        ent = raw.get("entropy", {}) or {}
        self.entropy_enabled = bool(ent.get("enabled", False))
        self.entropy_rules = []
        if self.entropy_enabled:
            heuristics = ent.get("heuristics")
            if heuristics:
                for h in heuristics:
                    self.entropy_rules.append(self._compile_entropy(h, ent))
            else:  # legacy single-heuristic form
                self.entropy_rules.append(self._compile_entropy({
                    "name": "high-entropy-token",
                    "charset_regex": ent.get("charset_regex", r"[A-Za-z0-9+/=]{24,}"),
                    "min_length": ent.get("min_length", 24),
                    "threshold": ent.get("threshold", 4.5),
                }, ent))

        # Excludes: from patterns.yml plus any injected by the caller. The
        # sanctioned-set lock (C2) is applied by the caller in main() AFTER any
        # --exclude entries are appended.
        self.excludes = list(raw.get("exclude", []) or [])
        self.excludes.extend(default_excludes or [])

        # Literal denylist terms (personal identifiers). Never from patterns.yml.
        self.denylist = list(denylist_terms or [])

    def _compile_entropy(self, h, ent):
        name = h.get("name", "high-entropy-token")
        charset = h.get("charset_regex", r"[A-Za-z0-9+/=]{24,}")
        min_len = int(h.get("min_length", ent.get("min_length", 24)))
        threshold = float(h.get("threshold", ent.get("threshold", 4.5)))
        # H3-residual: optional framing prefixes (e.g. "0x"/"0X" on hex secrets)
        # matched by the charset but stripped before length + entropy are
        # measured, so the heuristic scores the payload, not the marker.
        strip_prefixes = tuple(h.get("strip_prefixes", []) or [])
        try:
            rx = re.compile(charset)
        except re.error as exc:
            _fail("patterns.yml: bad entropy charset_regex for {0}: {1}".format(name, exc))
        return (name, rx, min_len, threshold, strip_prefixes)

    def is_excluded(self, relpath, repo_anchored=True):
        """Decide exclusion for a path expressed in the frame the caller anchors
        to: repo-relative for git-history scans and for tree scans whose scan
        root sits inside a git repo; scan-root-relative in the non-git fallback.

        Two match classes, derived purely from the sanctioned entry's SHAPE (the
        sanctioned SET is never widened here - only HOW each entry matches):

          - Single-segment entries (e.g. `.git`) are universal internal-dir
            excludes: they match ANY path segment, in ANY frame, so git internals
            are skipped wherever they sit (including a nested `.git`) and never
            scanned. These apply even in the non-git fallback.
          - Multi-segment path entries (e.g. `scripts/ng0/tests`) are
            repo-anchored: they match only as a prefix of the repo-relative path,
            so the scanner's OWN fixture dir is excluded at its true location -
            NOT a same-named dir that merely happens to sit under a deeper scan
            root (the delta-4 collision). With no repo frame (`repo_anchored`
            False) there is no basis to call a foreign file "the scanner's
            fixtures", so these do NOT apply - which closes the fallback hiding
            vector rather than reopening it.

        Glob entries keep fnmatch semantics and are likewise repo-anchored.
        """
        rel = relpath.replace(os.sep, "/")
        segs = rel.split("/")
        for entry in self.excludes:
            e = entry.replace(os.sep, "/").strip().rstrip("/")
            if not e:
                continue
            if any(ch in e for ch in "*?[]"):
                if repo_anchored and (
                    fnmatch.fnmatch(rel, e) or fnmatch.fnmatch(rel, e + "/*")
                ):
                    return True
            elif "/" in e:
                if repo_anchored and (rel == e or rel.startswith(e + "/")):
                    return True
            else:
                if e in segs:
                    return True
        return False

    def scan_text(self, source, path, text):
        """Return a list of Finding for one file's text content."""
        findings = []
        lines = text.split("\n")

        for name, rx in self.named:
            for m in rx.finditer(text):
                findings.append(
                    Finding(source, path, _line_of(text, m.start(), lines), name, m.group(0))
                )

        for term in self.denylist:
            if not term:
                continue
            start = 0
            low_text = text.lower()
            low_term = term.lower()
            while True:
                idx = low_text.find(low_term, start)
                if idx == -1:
                    break
                findings.append(
                    Finding(source, path, _line_of(text, idx, lines), "denylist-literal", term)
                )
                start = idx + len(low_term)

        # H3: entropy heuristics across multiple alphabets. No blunt "2+ slash"
        # skip - a secret can legitimately contain slashes (std base64) or be
        # pure hex. De-dupe identical (offset, token) hits so overlapping
        # alphabets do not double-report the same token.
        if self.entropy_enabled:
            seen = set()
            for name, rx, min_len, threshold, strip_prefixes in self.entropy_rules:
                for m in rx.finditer(text):
                    token = m.group(0)
                    # H3-residual: measure the payload, not any framing prefix
                    # (e.g. a leading 0x on a hex secret), so min_length and the
                    # entropy threshold apply to the secret itself.
                    payload = token
                    for pfx in strip_prefixes:
                        if payload.startswith(pfx):
                            payload = payload[len(pfx):]
                            break
                    if len(payload) < min_len:
                        continue
                    if shannon_entropy(payload) >= threshold:
                        key = (m.start(), token)
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append(
                            Finding(
                                source, path, _line_of(text, m.start(), lines),
                                name, token,
                            )
                        )
        return findings


def _line_of(text, offset, lines):
    return text.count("\n", 0, offset) + 1


def _load_yaml(path):
    if not os.path.isfile(path):
        _fail("patterns file not found: {0}".format(path))
    with open(path, "r", encoding="utf-8") as fh:
        try:
            return yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            _fail("could not parse {0}: {1}".format(path, exc))


def load_denylist(path):
    """Load literal identifiers from a denylist file (one term per line).

    Blank lines and lines starting with '#' are ignored. Missing file -> [].
    Callers that pass --with-denylist HARD-FAIL on an empty result (see main);
    this loader stays pure and just reports what it found.
    """
    terms = []
    if not path or not os.path.isfile(path):
        return terms
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            term = raw.strip()
            if term and not term.startswith("#"):
                terms.append(term)
    return terms


# --------------------------------------------------------------------------- #
# Decode / binary handling (H1)
# --------------------------------------------------------------------------- #
def scannable_texts(data):
    """Return the list of candidate decodes to scan for one blob.

    Every file's RAW BYTES are decoded under every candidate encoding (see
    _SCAN_ENCODINGS) and the caller unions the findings, instead of trying to
    pick the "right" encoding from the decode output (which any mojibake pattern
    defeats). A candidate is kept if the raw bytes decode under it - STRICT for
    the Unicode encodings (a strict success means "these bytes ARE valid text in
    this encoding"), and ALWAYS for latin1 (256/256 bytes valid):
      - 张杰 as BOM-less UTF-16LE (20 5f 70 67) decodes under utf-8 as " _pg"
        AND under utf-16-le as "张杰"; the union scans both, so the name is found.
      - An ASCII secret saved UTF-16LE decodes under utf-8 as NUL-interleaved
        noise (no match) AND under utf-16-le as the real text (regex matches).
      - An ASCII secret followed by one 0x80 byte fails UTF-8 and reads as
        mojibake under every wide decode, but latin1 decodes the whole blob and
        preserves the ASCII secret 1:1, so it is always caught (CR-B).

    INVARIANT: for any non-empty input this NEVER returns an empty list - latin1
    always contributes a candidate - so a caller can never reach a clean result
    without the raw bytes having been scanned. Empty input yields one empty
    candidate. Duplicate decodes (pure ASCII is identical under several
    encodings) are de-duplicated so the same text is not scanned twice.
    """
    if not data:
        return [""]
    texts = []
    seen = set()
    for enc in _SCAN_ENCODINGS:
        try:
            text = data.decode(enc)  # STRICT for Unicode; latin1 never raises
        except (UnicodeDecodeError, ValueError):
            continue
        if text not in seen:
            seen.add(text)
            texts.append(text)
    return texts


# --------------------------------------------------------------------------- #
# Scan targets
# --------------------------------------------------------------------------- #
def _relto(full, base):
    """Path of `full` relative to `base`, posix-normalized. Cross-drive safe."""
    try:
        return os.path.relpath(full, base).replace(os.sep, "/")
    except ValueError:
        return os.path.abspath(full).replace(os.sep, "/")


def _scan_blob(rules, source, rel, data):
    """Decode one blob under the encoding union and scan every candidate.

    Every file's raw bytes are scanned; extensions are NOT trusted (CR-A) - a
    text leak renamed `secret.png` is decoded and scanned like any other file.
    latin1 is always in the union and never fails, so there is no "undecodable"
    escape hatch and no fail-closed branch: the raw-byte (latin1) scan is the
    backstop for everything, ASCII secrets included (CR-B).

    Findings from all candidate decodes are UNIONed and de-duplicated on
    (line, rule, redacted match) so an identical hit that appears under more than
    one decode (e.g. an ASCII secret that reads the same under several encodings)
    is reported once.
    """
    findings = []
    seen = set()
    for text in scannable_texts(data):
        for f in rules.scan_text(source, rel, text):
            key = (f.line, f.rule, redact(f.match))
            if key in seen:
                continue
            seen.add(key)
            findings.append(f)
    return findings


def _git_repo_root(path):
    """Absolute repo root (git worktree top-level) that CONTAINS `path`, or None
    when `path` is not inside a git worktree.

    Run against the SCAN ROOT, so the answer does not depend on the process CWD
    (that CWD-dependence was the Scenario-5 fragility). Never raises and never
    leaks git's stderr into scanner output: a non-repo scan root is a normal
    case (scanning a foreign extracted tree), signalled by a None return - not
    an error - so the caller can fall back cleanly.
    """
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, ValueError):  # git missing, or path not a directory
        return None
    if out.returncode != 0:
        return None
    root = out.stdout.decode("utf-8", "replace").strip()
    return root or None


def scan_tree(rules, root):
    """Scan every non-excluded file under `root`.

    Exclusion frame (the delta-4 fix): sanctioned multi-segment excludes like
    `scripts/ng0/tests/` name the scanner's OWN fixture dir at its true location
    in the scanner repo, so they are matched against each file's path RELATIVE
    TO THE REPO ROOT - not relative to the scan root. Matching relative to the
    scan root let a deeper decoy collide: under `--tree docs`, a planted
    `docs/scripts/ng0/tests/leak.md` produced the scan-root-relative path
    `scripts/ng0/tests/leak.md`, matched the sanctioned prefix, and was hidden.
    Anchoring to the repo root gives that same decoy the path
    `docs/scripts/ng0/tests/leak.md`, which does NOT match, so it IS scanned.

    The repo root is discovered from the SCAN ROOT (`git rev-parse
    --show-toplevel`), so the result is CWD-independent. When the scan root is
    not inside a git repo (`_git_repo_root` returns None) the path is matched
    scan-root-relative with `repo_anchored=False`: a foreign tree has no basis
    to be called "the scanner's fixtures", so the multi-segment excludes
    correctly do NOT apply (closing the fallback hiding vector rather than
    reopening it), while the universal single-segment `.git` exclude still
    prunes any `.git` directory encountered.

    Reported paths stay relative to the SCAN ROOT (user-facing frame, unchanged);
    only the exclusion MATCH uses the repo-relative frame.

    Robustness: the repo-relative path is built as `sub_prefix + scan-root-
    relative`, where `sub_prefix` is the scan root's position INSIDE the repo,
    computed ONCE via `os.path.realpath` on both the scan root and the repo root.
    realpath resolves 8.3 short names, symlinks, and drive-letter case, so two
    spellings of the same directory (e.g. git's long-form directory name vs the
    abspath 8.3 short name) cannot make the anchoring silently miss and
    over-scan the scanner's own fixtures. Per-file paths stay in the stable scan-root textual
    frame (no per-file realpath), so symlinked dirs keep their logical path.
    """
    findings = []
    root_abs = os.path.abspath(root)
    repo_root = _git_repo_root(root_abs)
    sub_prefix = ""
    if repo_root is not None:
        try:
            sub = os.path.relpath(
                os.path.realpath(root_abs), os.path.realpath(repo_root)
            ).replace(os.sep, "/")
        except ValueError:  # different drive - can't express a relative frame
            sub = ".."
        if sub in (".", ""):
            sub_prefix = ""              # scan root IS the repo root
        elif sub.startswith(".."):
            repo_root = None             # scan root not under the repo -> fallback
        else:
            sub_prefix = sub + "/"
    repo_anchored = repo_root is not None

    def _excluded(full):
        scan_rel = _relto(full, root_abs)
        rel_for_match = (sub_prefix + scan_rel) if repo_anchored else scan_rel
        return rules.is_excluded(rel_for_match, repo_anchored=repo_anchored)

    for dirpath, dirnames, filenames in os.walk(root_abs):
        # Prune excluded directories in place so os.walk does not descend into
        # them (keeps the scanner out of .git internals and the sanctioned
        # fixture dir - never scanned, never reported).
        pruned = []
        for d in dirnames:
            if not _excluded(os.path.join(dirpath, d)):
                pruned.append(d)
        dirnames[:] = pruned

        for fname in filenames:
            full = os.path.join(dirpath, fname)
            if _excluded(full):
                continue
            # User-facing path: relative to the scan root (repo-relative when
            # scanning from the repo root with `--tree .`).
            rel = _relto(full, root_abs)
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except (IOError, OSError):
                continue
            findings.extend(_scan_blob(rules, "tree", rel, data))
    return findings


def _git(repo, args):
    out = subprocess.run(
        ["git", "-C", repo] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if out.returncode != 0:
        _fail("git {0} failed: {1}".format(" ".join(args), out.stderr.decode("utf-8", "replace").strip()))
    return out.stdout


def scan_history(rules, ref, repo=".", all_refs=False):
    """Scan every committed blob of every non-excluded path reachable from REF.

    all_refs=True (H2) scans ALL local refs + tags (`rev-list --all --tags`),
    catching secrets on non-HEAD branches, tags, and orphan refs that a later
    push would leak. Path exclusion is applied BEFORE de-duplicating by blob, so
    an identical blob that also exists at a non-excluded path is still scanned.

    L1: paths come from `git ls-tree -r -z` (NUL-delimited, NO C-quoting), so
    filenames with spaces, unicode, quotes, or backslashes parse correctly for
    both exclusion and reporting.
    """
    repo = os.path.abspath(repo)
    if all_refs:
        commits = _git(repo, ["rev-list", "--all", "--tags"]).decode("utf-8", "replace").split()
    else:
        commits = _git(repo, ["rev-list", ref]).decode("utf-8", "replace").split()
    seen_blobs = set()
    pairs = []  # (path, blobsha)
    for commit in commits:
        raw = _git(repo, ["ls-tree", "-r", "-z", commit])
        listing = raw.decode("utf-8", "surrogateescape")
        for row in listing.split("\0"):
            if not row:
                continue
            meta, _, path = row.partition("\t")
            parts = meta.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue
            blobsha = parts[2]
            # -z output is raw: no surrounding quotes, no escapes to undo.
            # `git ls-tree` paths are already REPO-RELATIVE, which is exactly the
            # frame is_excluded's sanctioned multi-segment excludes anchor to, so
            # repo_anchored=True is correct here (a history scan is always inside
            # the repo whose objects it reads).
            rel = path.replace(os.sep, "/")
            if rules.is_excluded(rel, repo_anchored=True):
                continue
            key = (rel, blobsha)
            if key in seen_blobs:
                continue
            seen_blobs.add(key)
            pairs.append((rel, blobsha))

    findings = []
    for rel, blobsha in pairs:
        data = _git(repo, ["cat-file", "blob", blobsha])
        findings.extend(_scan_blob(rules, "history", rel, data))
    return findings


# --------------------------------------------------------------------------- #
# Errors / CLI
# --------------------------------------------------------------------------- #
def _fail(msg):
    sys.stderr.write("ERROR: {0}\n".format(msg))
    sys.exit(2)


def build_parser():
    p = argparse.ArgumentParser(
        description="NG-0 secret + PII scanner (Salt-e PA)."
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--tree", metavar="PATH", help="scan files on disk under PATH")
    target.add_argument("--git-history", metavar="REF", dest="git_history",
                        nargs="?", const="HEAD", default=None,
                        help="scan every committed blob reachable from REF (default HEAD)")
    p.add_argument("--all-refs", action="store_true",
                  help="with --git-history: scan ALL local refs + tags (release-gate mode)")
    tier = p.add_mutually_exclusive_group()
    tier.add_argument("--patterns-only", action="store_true",
                     help="generic patterns.yml only (default; safe on forks)")
    tier.add_argument("--with-denylist", action="store_true",
                     help="also load literal identifiers from the denylist (maintainer-only)")
    p.add_argument("--patterns", default=DEFAULT_PATTERNS,
                  help="path to patterns.yml (default: alongside this script)")
    p.add_argument("--denylist", default=DEFAULT_DENYLIST,
                  help="path to literal denylist for --with-denylist (default: denylist.local.txt)")
    p.add_argument("--repo", default=".",
                  help="git repo root for --git-history (default: .)")
    p.add_argument("--no-default-exclude", action="store_true",
                  help="do NOT apply patterns.yml `exclude:` (scan even the .git/ internals)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                  help="additional exclude entry (repeatable; sanctioned paths only)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.all_refs and args.git_history is None:
        _fail("--all-refs requires --git-history")

    denylist_terms = []
    if args.with_denylist:
        denylist_terms = load_denylist(args.denylist)
        if not denylist_terms:
            # C1: fail CLOSED. A silently-empty denylist must never green-light
            # a run that was explicitly asked to check literal identifiers.
            _fail(
                "--with-denylist set but ZERO terms loaded from {0}. Refusing to "
                "run: a missing/empty denylist would let literal identifiers "
                "ship undetected. Fix the path (--denylist) or the file.".format(
                    args.denylist)
            )

    default_excludes = [] if args.no_default_exclude else None
    # When --no-default-exclude is set we still honor any explicit --exclude.
    rules = RuleSet(args.patterns, denylist_terms, default_excludes)
    if args.no_default_exclude:
        rules.excludes = list(args.exclude)
    else:
        rules.excludes.extend(args.exclude)
    # C2: lock the effective exclude set to the sanctioned list. Any attempt to
    # widen exclusion (via patterns.yml or --exclude) is stripped here, so the
    # named path is STILL scanned.
    rules.excludes = enforce_sanctioned_excludes(rules.excludes)

    if args.git_history is not None:
        findings = scan_history(rules, args.git_history, repo=args.repo,
                                all_refs=args.all_refs)
        scope = "all-refs" if args.all_refs else args.git_history
        target_desc = "git-history {0}".format(scope)
    else:
        findings = scan_tree(rules, args.tree)
        target_desc = "tree {0}".format(args.tree)

    tier = "with-denylist" if args.with_denylist else "patterns-only"
    if findings:
        for f in findings:
            print(f.format())
        print("NG-0 FAIL: {0} finding(s) over {1} [{2}]".format(
            len(findings), target_desc, tier))
        return 1

    print("NG-0 OK: 0 findings over {0} [{1}]".format(target_desc, tier))
    return 0


if __name__ == "__main__":
    sys.exit(main())
