---
type: doc
purpose: Overview of the NG-0 secret and personal-data enforcement tooling.
---

# NG-0 enforcement tooling

NG-0 is Salt-e PA's single hardest non-goal: **the published tree ships ZERO of
the author's personal data.** This directory is the automated safety net that
proves it. It is designed two-tier so the same tooling runs as a public PR gate
and as a maintainer-only release gate (see the project plan, Step 12).

## Scope and non-goals (v1)

NG-0 has a finite, declared responsibility. Drawing the boundary explicitly gives
review a fixed bar to test against - a leak scanner otherwise has no natural
finish line.

### In scope

- Scanning **text**, including text under the common Unicode encodings: UTF-8,
  UTF-16 LE/BE, and UTF-32 LE/BE.
- Scanning **raw single-byte / ASCII content** via ISO-8859-1 (latin1), which
  decodes all 256 byte values and preserves ASCII 1:1. latin1 is scanned for
  **every** file, so any ASCII secret or PII is visible no matter what the other
  bytes are or whether the Unicode decodes succeed. This is the permanent
  backstop and the reason no file is skipped by extension.
- Secret detection (named regexes + multi-alphabet Shannon-entropy heuristics),
  PII detection (email, phone, handle), and literal denylist matching.
- Working-tree mode and full git-history mode (`--git-history`, plus `--all-refs`
  for every local branch and tag).

### Explicit non-goals (v1)

These are **documented limitations, not bugs**. A secret hidden by one of the
techniques below is out of scope for v1 detection; callers are responsible for
not committing such artifacts.

- **Compressed archives.** NG-0 does not decompress or unpack gzip, zip, tar,
  bz2, xz, or 7z containers to scan their contents. It scans the archive's raw
  bytes as-is.
- **Recursively-decoded nested payloads.** NG-0 does not decode base64, hex, or
  other encoded blobs embedded inside a file and re-scan the decoded result. (The
  encoded *text* is still scanned as bytes - a long base64 blob may trip an
  entropy heuristic - but the *decoded* payload is not recovered and re-scanned.)
  A secret deliberately base64- or hex-encoded inside a committed file is not
  detected by content.
- **Steganography.** Secrets hidden in image/audio pixel or sample data are out
  of scope.
- **Binary container internals.** NG-0 does not parse format-specific internal
  structures (PDF object streams, database pages, media metadata atoms, etc.). It
  scans the raw bytes of such files - latin1 always applies - but does not
  interpret their internal format.

Everything a file exposes as ASCII or as one of the handled text encodings is in
scope; deeper layers (compression, nested encoding, steganography) are the
caller's responsibility for v1.

## Contents

| File | Purpose |
| --- | --- |
| `secret_pii_scan.py` | Secret + PII scanner. Working-tree and git-history modes; `--patterns-only` and `--with-denylist` tiers. |
| `patterns.yml` | Committed, generic rules (regex + entropy + PII patterns) and the `exclude:` list. Contains ZERO literal personal strings. |
| `template_lint.py` | Asserts memory files are empty or placeholder-only. Defines the placeholder-token grammar. |
| `tests/ac7_selftest.py` | Self-test: GENERATES its secret-shaped fixtures into throwaway temp dirs at test time and drives the scanner against them, asserting it goes RED, plus one regression scenario per folded review finding. Nothing secret-shaped is tracked (see the fixture model below). |

## Dependencies

Python 3 standard library plus **PyYAML** (`pip install pyyaml`), used only to
parse `patterns.yml`. No other third-party dependency. Kept minimal on purpose:
this is a public tool a contributor should be able to run with one pip install.

## Usage

Run from the **repository root** so reported tree paths read as repo-relative
(`scripts/ng0/...`), matching the frame git-history mode uses. The only default
exclude is `.git/` (git internals, never tracked). It is a single-segment entry,
so it prunes any `.git` directory encountered, in any frame - a tree scan, a
non-git foreign tree, or git-history mode - and its behaviour does **not** depend
on the working directory. The repo-anchoring machinery (repo-relative matching of
any multi-segment or glob exclude, discovered from the scan root via `git
rev-parse --show-toplevel`) is retained for defence in depth, but no multi-segment
path is sanctioned by default: nothing under the tracked tree is hidden.

```sh
# Public tier - generic patterns only. Safe on forked PRs (no literals loaded).
python scripts/ng0/secret_pii_scan.py --tree . --patterns-only
python scripts/ng0/secret_pii_scan.py --git-history HEAD --patterns-only

# Release-gate history mode: scan ALL local refs + tags, not just one ref.
python scripts/ng0/secret_pii_scan.py --git-history --all-refs --patterns-only

# Maintainer tier - also loads literal identifiers from a git-ignored denylist.
python scripts/ng0/secret_pii_scan.py --tree . --with-denylist --denylist denylist.local.txt
python scripts/ng0/secret_pii_scan.py --git-history main --with-denylist --denylist "$NG0_DENYLIST"

# Memory template discipline (canonical command; exits 0 on a clean checkout).
# The deliberately-generic prose rule files under memory/rules/ are allowlisted
# by default (gated by the PII scan + human review instead); --no-allowlist
# lints everything.
python scripts/ng0/template_lint.py memory/

# Prove the safety net works.
python scripts/ng0/tests/ac7_selftest.py
```

Exit codes: `0` clean, `1` findings/violations, `2` usage or runtime error.
`--with-denylist` **hard-fails with exit 2** if the denylist path is missing or
loads zero terms - a silently-empty denylist must never green-light a run.

### Scan modes

- `--tree PATH` scans files on disk. **Every** file's raw bytes are decoded under
  **every** candidate encoding - ISO-8859-1 (latin1) plus UTF-8, UTF-16 LE/BE,
  and UTF-32 LE/BE - and the findings from all decodes are **unioned**, so a
  secret saved in a UTF-16 `.md`/`.txt` (Windows Notepad, BOM or BOM-less, ASCII
  or CJK/Cyrillic) is scanned under its true encoding regardless of how the other
  decodes mangle it. **latin1 is always in the set** and never fails to decode
  (all 256 byte values are valid) and preserves ASCII 1:1, so any ASCII secret or
  PII is always visible even when a file is mostly binary or a stray byte breaks
  the UTF-8 decode. Files are **not** trusted or skipped by extension: a text leak
  renamed `secret.png` is decoded and scanned like any other file. There is no
  encoding-guessing heuristic to defeat and no "undecodable" escape hatch - the
  file's real encoding is always among the candidates, and latin1 backstops
  everything else. Over-scanning a genuine binary (latin1 mojibake) is the safe
  direction for a leak gate; an over-report prompts a look, it never hides a leak.
- `--git-history [REF]` scans every committed version of every non-excluded path
  reachable from `REF` (default `HEAD`). A secret deleted from the working tree
  but left in history still leaks; this mode catches it (PRD AC-2). Add
  `--all-refs` to cover **all local branches and tags** (`rev-list --all
  --tags`) - a secret on a side branch or tag would otherwise pass a HEAD-only
  scan and leak when that ref is pushed; this is the mode the release gate calls.
  Path exclusion is applied **before** de-duplicating by blob, so an identical
  blob at a non-excluded path is still scanned. History paths are read via
  `git ls-tree -r -z` (NUL-delimited), so filenames with spaces, unicode, or
  quotes parse correctly for both exclusion and reporting.

### Rule tiers

- `--patterns-only` (default) loads only `patterns.yml`: named secret regexes,
  Shannon-entropy heuristics across multiple alphabets (base64url, long hex,
  base32), and generic PII patterns (email, international + national/local
  phone, at-sign handle). The generic-secret-assignment rule catches quoted and
  unquoted secret assignments whose value is 12+ characters, when the key sits at
  one of these boundaries: the start of a line (optionally indented, optionally
  prefixed by `export ` or a YAML sequence-item marker `- `), or immediately
  after a JSON object `{` or a `,` separator. This covers `.env` lines, `KEY:
  value` YAML including list items (`- client_secret: ...`, `- token: "..."`),
  compact JSON objects (`{"token":"..."}`), and pretty-printed JSON where the key
  is on its own indented line. It does **not** claim full YAML/JSON parsing:
  values under 12 characters, values made only of `{ } < >`, and multi-line or
  block-scalar values are out of scope by design (the value classes stop at
  `{ } < >` so `{{PLACEHOLDER}}`/`<SECRET>` templates are not mistaken for filled
  secrets). No literal personal strings, so it is safe to run in untrusted CI
  (forked PRs).
- `--with-denylist` additionally loads literal identifiers from a denylist file
  (default `denylist.local.txt`, git-ignored; or inject a secret path via
  `--denylist`). Maintainer-only. The real denylist never ships. Hard-fails on
  an empty/missing denylist (see above).

## Exclude mechanism: `default_exclude` (sanctioned set, locked in code)

Default scans skip the paths listed under `exclude:` in `patterns.yml`. This is
called **`default_exclude`**. The **only** exclude the scanner honors is locked
in code as a sanctioned set of exactly one entry:

```yaml
exclude:
  - ".git/"
```

Only `.git/` is excluded because git internals are never tracked, so excluding
them is not a publish-gate blind spot. This is a security boundary. A contributor
cannot widen exclusion to hide personal data: any exclude beyond the sanctioned
set - whether added to `patterns.yml` `exclude:` or passed via `--exclude GLOB` -
is **ignored with a warning, and that path is STILL scanned**. The AC-7 self-test
additionally asserts that the committed `patterns.yml` `exclude:` list exactly
equals the sanctioned set, so an exclude expansion cannot pass review silently.

### Fixture model (F2 - no tracked blind spot)

The self-test fixtures are secret-shaped by design, so an earlier version kept
them under a tracked `scripts/ng0/tests/` directory that `default_exclude`
skipped. Excluding a **tracked** subtree is a blind spot: a real secret committed
there would ride through `--tree .` and the publish gate. That is now closed.
`scripts/ng0/tests/ac7_selftest.py` **generates** its secret-shaped fixtures into
throwaway temp dirs at test time and assembles every probe value from runtime
fragments, so **nothing under `tests/` is secret-shaped** and the whole tracked
tree is scanned with no fixture exclude. The publish gate therefore has zero
blind spots over tracked content, and a real scan of `scripts/ng0` (the self-test
included) still comes back clean.

Matching is by the entry's **shape**: a single-segment entry (`.git`) is a
universal internal-dir exclude and matches that name as **any** path segment, in
every frame (so a nested `.git` is pruned too). The repo-anchoring machinery for
multi-segment and glob entries (repo-relative prefix matching) is retained for
defence in depth but has no sanctioned member by default. To change the
sanctioned set, edit `SANCTIONED_EXCLUDES` in `secret_pii_scan.py` **and** the
`exclude:` list in `patterns.yml` together (the self-test enforces parity).

## Placeholder-token grammar (v1)

`template_lint.py` enforces that memory files are empty or contain only template
scaffolding. Step 3 authors memory files to this grammar.

A **placeholder token** is:

- `{{UPPER_SNAKE}}` - double-curly form, e.g. `{{NAME}}`, `{{USER_EMAIL}}`, `{{WS_1}}`
- `<UPPER_SNAKE>` - angle form, e.g. `<TIMEZONE>`, `<ROLE>`

where `UPPER_SNAKE` is one or more of `A-Z`, `0-9`, `_`.

A file **passes** when it is empty/whitespace-only, or every non-blank line is:

1. **Structure** - heading, frontmatter fence (`---`), horizontal rule, table
   separator, table row, markdown comment, or a bare list/blockquote marker.
2. **Field** - `LABEL: VALUE` where `VALUE` is empty or a placeholder expression.
3. **Placeholder expression** - after stripping any list/blockquote prefix, the
   whole line is placeholder tokens joined only by whitespace and the connective
   punctuation `, . ; : / | ( ) [ ] -` (no bare words, digits, or other letters).

Plus a **belt-and-suspenders** gate: any line matching a PII pattern
(email/phone/handle) fails, regardless of the above.

### Label-position boundary (read this)

Headings, list labels, and table cells are **label positions**. `template_lint`
cannot positionally distinguish a legitimate label (a field name, a column
header) from a filled-in value there, so it treats those positions as structure
and does not reject bare words in them - it still runs the PII belt on the whole
line. Literal personal **names** sitting in a label position (which match no
generic pattern) are caught by `secret_pii_scan.py --with-denylist`, not here.

**The two tools together enforce NG-0. Neither alone is sufficient.**
