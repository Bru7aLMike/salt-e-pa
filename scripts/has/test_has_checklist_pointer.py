#!/usr/bin/env python3
"""Checklist pointer + tick-mismatch assert script for the HAS handoff writer.

The HAS subagent prompt (`has-subagent-prompt.md`, Phase 3.3) turns every open
task checklist the session touched into exactly ONE `next:` pointer of the form

    continue <task-slug> CHECKLIST.md at item N (<title of item N>)

and never copies checklist item text anywhere else. It also compares the
checklist ticks against transcript evidence and lists every mismatch under a
`## Checklist mismatches` body section, with the count in its return. It never
edits the checklist. The existing Track A/B carry-forward (Phase 3.2) must keep
working next to it.

The HAS prompt is run by an LLM, so this script does not run HAS. It checks a
handoff that HAS produced against the fixture in
`tests/fixtures/checklist/`:

    memory/MAP.md, memory/SCHEMAS.md          fixture memory dir
    memory/content/work/demo-ws/README.md     workstream with Active handoffs
    memory/content/work/demo-ws/hand-offs/    previous handoff: next: holds
                                              'PROJ-12: add an export button'
                                              and '[carry:1] Tidy the fixture
                                              README'; open_items: holds
                                              '[stale?:2] Settle on a home
                                              for the long-term archive
                                              copies'
    tasks/demo-ws/import-run/CHECKLIST.md     filled from the shipped
                                              CHECKLIST template: 6 items,
                                              1-3 ticked, next unchecked = 4
    transcripts/transcript_A.txt              evidence for items 1-3
    transcripts/transcript_B.txt              evidence for items 1 and 3 only
                                              (item 2 ticked without evidence)

Neither transcript addresses the previous handoff's items, so all three must
carry forward (none dropped).

C4 allows one checklist item title to appear once in the handoff body in
total (summary prose naturally names where it stopped); every other copy of
item text fails. C7 expects `[unverified]` as a prefix after the carry tag:
`[stale?:N] [unverified] text`.

Usage:
    python scripts/has/test_has_checklist_pointer.py \\
        --handoff <produced handoff.md> \\
        --checklist <the CHECKLIST.md copy HAS could see> \\
        --checklist-before <sha256 of that file before the HAS run> \\
        --expect-mismatch 0|1 \\
        --return <file holding the HAS return text>

    python scripts/has/test_has_checklist_pointer.py --self-test

Use --expect-mismatch 0 for transcript A and 1 for transcript B. Exit 0 when
every check passes, 1 otherwise. --self-test checks the fixture itself, then
proves the checks pass a hand-written correct handoff and fail hand-written
wrong ones. Under pytest, `test_self_test` runs the same self-test.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent            # scripts/has/
REPO_ROOT = HERE.parent.parent                    # repo root
FIXTURE_DIR = HERE / "tests" / "fixtures" / "checklist"
FIXTURE_CHECKLIST = FIXTURE_DIR / "tasks" / "demo-ws" / "import-run" / "CHECKLIST.md"
FIXTURE_PREV_HANDOFF = (
    FIXTURE_DIR / "memory" / "content" / "work" / "demo-ws" / "hand-offs"
    / "2026-09-20_01_import-prep.md"
)
FIXTURE_TRANSCRIPTS = FIXTURE_DIR / "transcripts"
CHECKLIST_TEMPLATE = REPO_ROOT / "memory" / "system" / "workspace" / "templates" / "CHECKLIST.md"

# Expected pointer target for the fixture checklist.
EXPECT_ITEM = 4
# Item ticked without evidence in transcript B.
MISMATCH_ITEM = 2

# Carry-forward expectations (HAS Phase 3.2 applied to the fixture previous
# handoff with no resolving evidence in either transcript).
PREV_TICKET = "PROJ-12: add an export button"
PREV_NEXT_CARRY = "[carry:1] Tidy the fixture README"
PREV_OPEN_CARRY = "[stale?:2] Settle on a home for the long-term archive copies"
EXPECT_TICKET = PREV_TICKET                                      # Track A: verbatim, no tags
EXPECT_NEXT_CARRY = "[stale?:2] [unverified] Tidy the fixture README"   # Track B, UNCHECKABLE
EXPECT_OPEN_CARRY = "[stale?:3] [unverified] Settle on a home for the long-term archive copies"  # Track B, UNCHECKABLE
CARRY_TOPICS = ("PROJ-12", "Tidy the fixture README", "Settle on a home for the long-term archive copies")

POINTER_RE = re.compile(r"^continue (\S+) CHECKLIST\.md at item (\d+)(?: \((.+)\))?$")
TOP_ITEM_RE = re.compile(r"^- \[( |x|X)\] (.+?)\s*$")
SUB_ITEM_RE = re.compile(r"^\s+- \[( |x|X)\] (.+?)\s*$")
TAG_TOKENS = ("[carry:", "[stale?:", "[unverified]")
# One checklist item title may appear once in the handoff body (V3-fix decision).
C4_BODY_TITLE_BUDGET = 1


# --- parsing helpers ---------------------------------------------------------

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def split_frontmatter(text: str):
    """Return (frontmatter dict, body text). Raises ValueError when missing."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("no frontmatter block")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            data = yaml.safe_load("\n".join(lines[1:i])) or {}
            if not isinstance(data, dict):
                raise ValueError("frontmatter is not a mapping")
            return data, "\n".join(lines[i + 1:])
    raise ValueError("unterminated frontmatter block")


def as_list(value):
    """Normalize a handoff list field: missing / 'none' -> [], scalar -> [str]."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if v is not None]
    text = str(value).strip()
    if text.lower() == "none" or not text:
        return []
    return [text]


def parse_checklist(path: Path):
    """Return (slug, items, sub_texts). items = [(number, ticked, title)]."""
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    items, subs = [], []
    for line in body.split("\n"):
        m = TOP_ITEM_RE.match(line)
        if m:
            items.append((len(items) + 1, m.group(1).lower() == "x", m.group(2)))
            continue
        m = SUB_ITEM_RE.match(line)
        if m:
            subs.append(m.group(2))
    return path.parent.name, items, subs


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def section(body: str, heading: str):
    """Return the lines of every `## <heading>` section (list of line lists)."""
    out, current = [], None
    for line in body.split("\n"):
        if re.match(r"^#{1,2} ", line):
            if current is not None:
                out.append(current)
                current = None
            if line.strip() == "## " + heading:
                current = []
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        out.append(current)
    return out


# --- the checks --------------------------------------------------------------

def check_handoff(handoff_text: str, checklist_path: Path, checklist_before: str,
                  expect_mismatch: int, return_text: str):
    """Run every V3 check. Returns a list of (check_id, ok, detail)."""
    results = []

    def record(cid, ok, detail):
        results.append((cid, bool(ok), detail))

    # C1: checklist byte-identical.
    after = sha256_file(checklist_path)
    record("C1-checklist-unchanged", after == checklist_before.strip().lower(),
           f"sha256 before={checklist_before.strip()} after={after}")

    # C2: frontmatter parses.
    try:
        fm, body = split_frontmatter(handoff_text)
    except (ValueError, yaml.YAMLError) as exc:
        record("C2-frontmatter", False, f"handoff frontmatter unparseable: {exc}")
        return results
    next_items = as_list(fm.get("next"))
    open_items = as_list(fm.get("open_items"))
    record("C2-frontmatter", True, f"next={len(next_items)} open_items={len(open_items)}")

    slug, items, subs = parse_checklist(checklist_path)
    unchecked = [n for n, ticked, _ in items if not ticked]
    first_unchecked = unchecked[0] if unchecked else None
    titles = {n: t for n, _, t in items}

    # C3: exactly one pointer, in next:, at the first unchecked item, untagged.
    ptr_next = [i for i in next_items if "CHECKLIST.md at item" in i]
    ptr_open = [i for i in open_items if "CHECKLIST.md at item" in i]
    pointer_title = None
    if len(ptr_next) != 1 or ptr_open:
        record("C3-pointer", False,
               f"expected exactly 1 pointer in next: and 0 in open_items:, "
               f"found next={ptr_next} open_items={ptr_open}")
    else:
        ptr = ptr_next[0]
        m = POINTER_RE.match(ptr)
        problems = []
        if any(tok in ptr for tok in TAG_TOKENS):
            problems.append("pointer carries a carry/stale/unverified tag")
        if not m:
            problems.append("pointer does not match "
                            "'continue <slug> CHECKLIST.md at item N (<title>)'")
        else:
            if m.group(1) != slug:
                problems.append(f"slug {m.group(1)!r} != {slug!r}")
            if int(m.group(2)) != first_unchecked:
                problems.append(f"item {m.group(2)} != first unchecked {first_unchecked}")
            if first_unchecked != EXPECT_ITEM:
                problems.append(f"fixture first unchecked is {first_unchecked}, expected {EXPECT_ITEM}")
            pointer_title = m.group(3)
            if pointer_title is not None and first_unchecked in titles \
                    and pointer_title.strip() != titles[first_unchecked]:
                problems.append(f"pointer title {pointer_title!r} != item "
                                f"{first_unchecked} title {titles.get(first_unchecked)!r}")
        record("C3-pointer", not problems, "; ".join(problems) or ptr)

    # C4: no checklist item text anywhere, with two exceptions. In the
    # frontmatter, only the item N title inside the pointer. In the body, one
    # checklist item title may appear ONCE in total (summary prose naturally
    # names the item it stopped at). Sub-item text is never allowed.
    body_norm = norm(body)
    unix_text = handoff_text.replace("\r\n", "\n")
    fm_norm = norm(unix_text[: len(unix_text) - len(body)])  # body is a suffix of unix_text
    leaks = []
    body_hits = []
    for n, _, title in items:
        allowed = 1 if (n == first_unchecked and pointer_title is not None) else 0
        fm_count = fm_norm.count(norm(title))
        if fm_count != allowed:
            leaks.append(f"item {n} text x{fm_count} in frontmatter (allowed {allowed})")
        body_count = body_norm.count(norm(title))
        body_hits += [n] * body_count
    if len(body_hits) > C4_BODY_TITLE_BUDGET:
        leaks.append(f"item titles in body x{len(body_hits)} (items {body_hits}, "
                     f"allowed {C4_BODY_TITLE_BUDGET} in total)")
    for text in subs:
        count = norm(handoff_text).count(norm(text))
        if count:
            leaks.append(f"sub-item text {text!r} x{count} (allowed 0)")
    record("C4-no-item-text", not leaks, "; ".join(leaks) or
           f"no copied item text (body title mentions: {len(body_hits)})")

    # C5: ## Checklist mismatches entries.
    sections = section(body, "Checklist mismatches")
    entries = []
    for sec in sections:
        for line in sec:
            s = line.strip()
            if re.match(r"^[-*] ", s) and norm(s[2:]).rstrip(".") != "none":
                entries.append(s)
    problems = []
    if len(sections) > 1:
        problems.append(f"{len(sections)} '## Checklist mismatches' sections")
    if expect_mismatch == 0:
        if entries:
            problems.append(f"expected no entries, found {entries}")
    else:
        if not sections:
            problems.append("section '## Checklist mismatches' missing")
        if len(entries) != 1:
            problems.append(f"expected exactly 1 entry, found {entries}")
        else:
            nums = {int(x) for x in re.findall(r"\bitem\s+(\d+)\b", entries[0], re.I)}
            if nums != {MISMATCH_ITEM}:
                problems.append(f"entry names items {sorted(nums)}, expected [{MISMATCH_ITEM}]")
    record("C5-mismatch-section", not problems,
           "; ".join(problems) or f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")

    # C6: return count.
    counts = re.findall(r"^\s*CHECKLIST_MISMATCHES\s*:\s*(\d+)\s*$", return_text, re.I | re.M)
    values = {int(c) for c in counts}
    record("C6-return-count", values == {expect_mismatch},
           f"CHECKLIST_MISMATCHES values in return: {sorted(values) or 'none found'}, "
           f"expected {expect_mismatch}")

    # C7: carry-forward (Track A verbatim, Track B tagged, [unverified] as a prefix after the carry tag).
    problems = []

    def holders(topic):
        return ([("next", i) for i in next_items if topic in i]
                + [("open_items", i) for i in open_items if topic in i])

    for topic, field, expected in (
        ("PROJ-12", "next", EXPECT_TICKET),
        ("Tidy the fixture README", "next", EXPECT_NEXT_CARRY),
        ("Settle on a home for the long-term archive copies", "open_items", EXPECT_OPEN_CARRY),
    ):
        found = holders(topic)
        if found != [(field, expected)]:
            problems.append(f"{topic!r}: expected exactly [{field}: {expected!r}], found {found}")
    for line in handoff_text.split("\n"):
        if "DROPPED" in line and any(t in line for t in CARRY_TOPICS):
            problems.append(f"carry item dropped: {line.strip()!r}")
    record("C7-carry-forward", not problems, "; ".join(problems) or "all three carried as expected")

    return results


def report(results) -> bool:
    ok = all(r[1] for r in results)
    for cid, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'} {cid}: {detail}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


# --- self-test ---------------------------------------------------------------

GOOD_FRONTMATTER = """---
date: 2026-09-21
session_end: 2026-09-21T18:00:00+00:00
workstream_id: demo-ws
status: active
next:
  - 'PROJ-12: add an export button'
  - '[stale?:2] [unverified] Tidy the fixture README'
  - continue import-run CHECKLIST.md at item 4 (Reconcile the staging row count with the source)
blockers: none
open_items:
  - '[stale?:3] [unverified] Settle on a home for the long-term archive copies'
---
"""

GOOD_BODY = """
# Handoff 2026-09-21: import run

## What was done

Worked the import-run checklist: items 1-3 ticked. The export and the load both
ran (1200 rows each way).

## Decisions

None.

## What's next

Resume at the checklist pointer in `next:`. The ticketed export button, the
README tidy and the archive-copies question carry forward.

## Git state

No git activity this session.

## Checklist mismatches

{mismatches}
"""

GOOD_A = GOOD_FRONTMATTER + GOOD_BODY.format(mismatches="None.")
GOOD_B = GOOD_FRONTMATTER + GOOD_BODY.format(
    mismatches="- import-run item 2: ticked without evidence")

# The primary wrong handoff: plausible, but it copies checklist item text into
# the body, drops the required [unverified] prefix, and tags the ticketed item.
WRONG_B = """---
date: 2026-09-21
session_end: 2026-09-21T18:00:00+00:00
workstream_id: demo-ws
status: active
next:
  - '[carry:1] PROJ-12: add an export button'
  - '[stale?:2] Tidy the fixture README'
  - continue import-run CHECKLIST.md at item 4 (Reconcile the staging row count with the source)
blockers: none
open_items:
  - '[stale?:3] [unverified] Settle on a home for the long-term archive copies'
---

# Handoff 2026-09-21: import run

## What was done

- Export the source rows to a CSV file
- Verify the CSV header names match the table
- Load the CSV rows into the staging table

## Checklist mismatches

- import-run item 2: ticked without evidence
"""


def _return(count: int) -> str:
    return ("STATUS: ok\n"
            "HANDOFF_PATH: <tmp>/2026-09-21_01_import-run.md\n"
            "SCRATCH_FILE: <tmp>/scratch/has-output.txt\n"
            f"CHECKLIST_MISMATCHES: {count}\n")


def _fixture_checks():
    """Sanity-check the fixture itself. Returns a list of failure strings."""
    fails = []
    # Checklist is the shipped template filled in (same keys, same comments).
    tpl_fm, tpl_body = split_frontmatter(CHECKLIST_TEMPLATE.read_text(encoding="utf-8"))
    fx_text = FIXTURE_CHECKLIST.read_text(encoding="utf-8")
    fx_fm, fx_body = split_frontmatter(fx_text)
    if list(fx_fm) != list(tpl_fm):
        fails.append(f"checklist frontmatter keys {list(fx_fm)} != template {list(tpl_fm)}")
    for line in tpl_body.split("\n"):
        if line.startswith("<!--") and line not in fx_body.split("\n"):
            fails.append(f"template comment line missing from fixture: {line!r}")
    if "{{" in fx_text or "<" + "TASK_GOAL>" in fx_text:
        fails.append("fixture checklist still holds template placeholders")
    if fx_fm.get("status") != "open" or fx_fm.get("type") != "checklist":
        fails.append("fixture checklist must be type: checklist, status: open")
    slug, items, subs = parse_checklist(FIXTURE_CHECKLIST)
    ticks = [t for _, t, _ in items]
    if slug != "import-run" or len(items) != 6 or ticks != [True, True, True, False, False, False]:
        fails.append(f"checklist shape wrong: slug={slug} ticks={ticks}")
    # Previous handoff holds the three carry-forward sources.
    prev_fm, _ = split_frontmatter(FIXTURE_PREV_HANDOFF.read_text(encoding="utf-8"))
    if as_list(prev_fm.get("next")) != [PREV_TICKET, PREV_NEXT_CARRY]:
        fails.append(f"previous handoff next: {prev_fm.get('next')}")
    if as_list(prev_fm.get("open_items")) != [PREV_OPEN_CARRY]:
        fails.append(f"previous handoff open_items: {prev_fm.get('open_items')}")
    # Transcripts: A has item-2 evidence, B does not; neither touches carry items.
    ta = (FIXTURE_TRANSCRIPTS / "transcript_A.txt").read_text(encoding="utf-8")
    tb = (FIXTURE_TRANSCRIPTS / "transcript_B.txt").read_text(encoding="utf-8")
    for name, text in (("A", ta), ("B", tb)):
        for n in (1, 2, 3):
            if f"new_string: - [x] {items[n - 1][2]}" not in text:
                fails.append(f"transcript {name} lacks the tick edit for item {n}")
        for topic in ("PROJ-12", "README", "archive"):
            if topic.lower() in text.lower():
                fails.append(f"transcript {name} mentions carry topic {topic!r}")
    if "check_headers.py" not in ta or "check_headers.py" in tb:
        fails.append("item-2 evidence must be in transcript A only")
    return fails


def run_self_test() -> int:
    ok = True
    fixture_fails = _fixture_checks()
    for f in fixture_fails:
        print("FAIL fixture:", f)
    print("fixture checks:", "PASS" if not fixture_fails else "FAIL")
    ok &= not fixture_fails

    with tempfile.TemporaryDirectory() as tmp:
        # Keep the task-slug parent folder: the slug is read from it.
        work = Path(tmp) / FIXTURE_CHECKLIST.parent.name / "CHECKLIST.md"
        work.parent.mkdir()

        def fresh():
            shutil.copyfile(FIXTURE_CHECKLIST, work)
            return sha256_file(work)

        def run(label, handoff, ret, expect, want_pass, mutate=None, want_fail_id=None):
            before = fresh()
            if mutate:
                mutate(work)
            results = check_handoff(handoff, work, before, expect, ret)
            passed = all(r[1] for r in results)
            failed_ids = [r[0] for r in results if not r[1]]
            good = passed if want_pass else (not passed and (
                want_fail_id is None or any(i.startswith(want_fail_id) for i in failed_ids)))
            verdict = "pass" if passed else "fail " + ",".join(failed_ids)
            print(f"{'OK ' if good else 'BAD'} [{label}] expected "
                  f"{'pass' if want_pass else 'fail ' + (want_fail_id or '')}, got {verdict}")
            return good

        cases = [
            ("correct handoff, transcript A", GOOD_A, _return(0), 0, True, None, None),
            ("correct handoff, transcript B", GOOD_B, _return(1), 1, True, None, None),
            ("wrong handoff, transcript B", WRONG_B, _return(1), 1, False, None, None),
            ("one item title once in body", GOOD_B.replace(
                "Worked the import-run", "Load the CSV rows into the staging table. Worked the import-run"),
             _return(1), 1, True, None, None),
            ("pointer title once in body", GOOD_B.replace(
                "Resume at the checklist pointer", "Reconcile the staging row count with the source. Resume at the checklist pointer"),
             _return(1), 1, True, None, None),
            ("two item titles in body", GOOD_B.replace(
                "Worked the import-run", "Load the CSV rows into the staging table. Worked the import-run").replace(
                "Resume at the checklist pointer", "Reconcile the staging row count with the source. Resume at the checklist pointer"),
             _return(1), 1, False, None, "C4"),
            ("same item title twice in body", GOOD_B.replace(
                "Worked the import-run", "Load the CSV rows into the staging table. Load the CSV rows into the staging table. Worked the import-run"),
             _return(1), 1, False, None, "C4"),
            ("item title in open_items", GOOD_B.replace(
                "open_items:\n", "open_items:\n  - Load the CSV rows into the staging table\n"),
             _return(1), 1, False, None, "C4"),
            ("sub-item text in body", GOOD_B.replace(
                "Worked the import-run", "Recheck the date columns in the weekly report. Worked the import-run"),
             _return(1), 1, False, None, "C4"),
            ("[unverified] as suffix", GOOD_B.replace(
                "[stale?:2] [unverified] Tidy the fixture README", "[stale?:2] Tidy the fixture README [unverified]"),
             _return(1), 1, False, None, "C7"),
            ("missing [unverified] on open_items carry", GOOD_B.replace(
                "[unverified] Settle on a home for the long-term archive copies", "Settle on a home for the long-term archive copies"),
             _return(1), 1, False, None, "C7"),
            ("ticketed item tagged", GOOD_B.replace(
                "'PROJ-12: add", "'[carry:1] PROJ-12: add"),
             _return(1), 1, False, None, "C7"),
            ("open_items carry moved to next", GOOD_B.replace(
                "  - '[stale?:2] Tidy", "  - '[stale?:3] [unverified] Settle on a home for the long-term archive copies'\n  - '[stale?:2] Tidy").replace(
                "open_items:\n  - '[stale?:3] [unverified] Settle on a home for the long-term archive copies'", "open_items: none"),
             _return(1), 1, False, None, "C7"),
            ("carry item DROPPED", GOOD_B + "\n## Carry-forward verification\n\n"
             "- DROPPED (from open_items:): \"[stale?:2] Settle on a home for the long-term archive copies\" - decided\n",
             _return(1), 1, False, None, "C7"),
            ("pointer at wrong item", GOOD_B.replace(
                "at item 4 (Reconcile", "at item 3 (Reconcile"),
             _return(1), 1, False, None, "C3"),
            ("pointer with wrong title", GOOD_B.replace(
                "(Reconcile the staging row count with the source)", "(Reconcile the counts)"),
             _return(1), 1, False, None, "C3"),
            ("pointer with carry tag", GOOD_B.replace(
                "  - continue import-run", "  - '[carry:1] continue import-run").replace(
                "with the source)\n", "with the source)'\n"),
             _return(1), 1, False, None, "C3"),
            ("two pointers", GOOD_B.replace(
                "blockers: none", "  - continue import-run CHECKLIST.md at item 5\nblockers: none"),
             _return(1), 1, False, None, "C3"),
            ("mismatch section missing", GOOD_B.split("## Checklist mismatches")[0],
             _return(1), 1, False, None, "C5"),
            ("mismatch names the wrong item", GOOD_B.replace("item 2: ticked", "item 3: ticked"),
             _return(1), 1, False, None, "C5"),
            ("mismatch entry when none expected", GOOD_B, _return(0), 0, False, None, "C5"),
            ("return count wrong", GOOD_B, _return(0), 1, False, None, "C6"),
            ("checklist edited by HAS", GOOD_B, _return(1), 1, False,
             lambda p: p.write_bytes(p.read_bytes().replace(
                 b"- [ ] Reconcile", b"- [x] Reconcile")), "C1"),
        ]
        for case in cases:
            ok &= run(*case)

    print("SELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def test_self_test():
    """pytest entry point: the self-test must pass."""
    assert run_self_test() == 0


# --- CLI ---------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--self-test", action="store_true",
                   help="check the fixture and prove the checks pass/fail hand-written handoffs")
    p.add_argument("--handoff", type=Path, help="handoff file produced by HAS")
    p.add_argument("--checklist", type=Path, help="the CHECKLIST.md copy HAS was pointed at")
    p.add_argument("--checklist-before", help="sha256 of --checklist before the HAS run")
    p.add_argument("--expect-mismatch", type=int, choices=(0, 1),
                   help="0 for transcript A, 1 for transcript B")
    p.add_argument("--return", dest="return_file", type=Path,
                   help="file holding the HAS return text (Phase 8 envelope)")
    args = p.parse_args(argv)

    if args.self_test:
        return run_self_test()
    missing = [n for n in ("handoff", "checklist", "checklist_before", "expect_mismatch",
                           "return_file") if getattr(args, n) is None]
    if missing:
        p.error("missing: " + ", ".join("--" + m.replace("_file", "").replace("_", "-")
                                         for m in missing))
    results = check_handoff(
        args.handoff.read_text(encoding="utf-8"),
        args.checklist,
        args.checklist_before,
        args.expect_mismatch,
        args.return_file.read_text(encoding="utf-8"),
    )
    return 0 if report(results) else 1


if __name__ == "__main__":
    sys.exit(main())
