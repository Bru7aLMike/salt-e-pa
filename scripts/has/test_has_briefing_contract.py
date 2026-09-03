#!/usr/bin/env python3
"""scanner -> HAS -> scanner BRIEFING contract test (the F6 fix).

The BRIEFING.md file has two independent producers that must never clobber each
other:

  - The workspace scanner owns the PA_SCAN activity block (everything BETWEEN the
    `<!-- PA_SCAN:start -->` / `<!-- PA_SCAN:end -->` markers). It runs as the
    final step of every HAS /wrap pipeline.
  - The HAS handoff pipeline owns the hand-authored region (everything OUTSIDE
    the marker pair: Session Reminders, Active Handoffs, and any prose).

The contract this file proves:

  1. Round-trip preservation - after the scanner rewrites the PA_SCAN block (the
     concluding step of a HAS pipeline run), every byte OUTSIDE the marker pair
     is identical to before. The hand-authored region is never touched.
  2. Fail-loud - if the marker pair is missing (stripped, or a fresh file with no
     block), the scanner REFUSES to write rather than overwriting or appending.
     A refusal aborts the /wrap pipeline, so HAS cannot silently clobber the
     hand-authored region.

Both are proven twice: once by importing the scanner's real byte-splice function
(`replace_briefing_block`), and once by running the scanner as a subprocess over
a copy of the shipped memory tree (true end-to-end integration).

Run:  python -m pytest scripts/has/test_has_briefing_contract.py -v
  or:  python scripts/has/test_has_briefing_contract.py
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# --- Locate repo + import the scanner's real contract code ------------------
HERE = Path(__file__).resolve().parent          # scripts/has/
SCRIPTS_DIR = HERE.parent                        # scripts/
REPO_ROOT = SCRIPTS_DIR.parent                   # repo root
SCANNER_PY = SCRIPTS_DIR / "workspace_scanner.py"
MEMORY_TEMPLATE = REPO_ROOT / "memory"

sys.path.insert(0, str(SCRIPTS_DIR))
import workspace_scanner as scanner  # noqa: E402

PA_SCAN_START = scanner.PA_SCAN_START
PA_SCAN_END = scanner.PA_SCAN_END
START_TOK = PA_SCAN_START.encode("utf-8")
END_TOK = PA_SCAN_END.encode("utf-8")


def _outside_regions(raw: bytes):
    """Return (before, after): the bytes outside the PA_SCAN marker pair.

    `before` is everything up to the start marker; `after` is everything from
    the end of the end marker. Together they are the hand-authored region the
    scanner must preserve byte-for-byte.
    """
    start = raw.find(START_TOK)
    end = raw.find(END_TOK)
    assert start != -1 and end != -1 and end >= start, "marker pair not found"
    return raw[:start], raw[end + len(END_TOK):]


def _fresh_block(tag: str) -> str:
    """A well-formed replacement block: begins with the start marker, ends with
    the end marker (the contract `replace_briefing_block` documents)."""
    return f"{PA_SCAN_START}\n\nSCANNER-GENERATED {tag}\n\n{PA_SCAN_END}"


# A BRIEFING with a CRLF hand-authored region on BOTH sides of an LF-delimited
# marker block - proves the splice is byte-exact and does not normalize newlines
# across the file (the exact bug byte-splicing prevents).
_HAND_BEFORE = (
    b"# Briefing\r\n\r\n"
    b"## Hand-authored region\r\n\r\n"
    b"### Session Reminders\r\n\r\n- do not touch this byte\r\n\r\n"
    b"### Active Handoffs\r\n\r\n| Workstream | Handoff | Next |\r\n"
    b"| --- | --- | --- |\r\n\r\n"
    b"## Scanner-owned region\r\n\r\n"
)
_HAND_AFTER = b"\r\n\r\n## Trailer kept verbatim\r\n- trailing hand byte\r\n"


def _write_briefing_with_markers(path: Path) -> None:
    body = _HAND_BEFORE + _fresh_block("ORIGINAL").encode("utf-8") + _HAND_AFTER
    path.write_bytes(body)


# ===========================================================================
# Unit level: the scanner's real byte-splice function
# ===========================================================================

def test_unit_roundtrip_hand_region_byte_identical(tmp_path, monkeypatch):
    """replace_briefing_block rewrites ONLY the marker block; the hand-authored
    region is byte-identical afterward, and the block content actually changed."""
    briefing = tmp_path / "BRIEFING.md"
    _write_briefing_with_markers(briefing)
    monkeypatch.setattr(scanner, "BRIEFING_FILE", briefing)

    before_raw = briefing.read_bytes()
    hand_before = _outside_regions(before_raw)

    scanner.replace_briefing_block(_fresh_block("REGENERATED-XYZ"))

    after_raw = briefing.read_bytes()
    hand_after = _outside_regions(after_raw)

    assert hand_after == hand_before, "hand-authored region was mutated"
    assert after_raw != before_raw, "block content did not change"
    assert b"REGENERATED-XYZ" in after_raw
    assert b"ORIGINAL" not in after_raw


def test_unit_fail_loud_missing_markers(tmp_path, monkeypatch):
    """Markers stripped -> BriefingContractError, and the file is left untouched
    (no overwrite, no append)."""
    briefing = tmp_path / "BRIEFING.md"
    briefing.write_bytes(
        b"# Briefing\r\n\r\nhand-authored only, NO markers here\r\n"
    )
    monkeypatch.setattr(scanner, "BRIEFING_FILE", briefing)

    before = briefing.read_bytes()
    with pytest.raises(scanner.BriefingContractError):
        scanner.replace_briefing_block(_fresh_block("SHOULD-NOT-LAND"))
    assert briefing.read_bytes() == before, "file was clobbered on fail-loud path"
    assert b"SHOULD-NOT-LAND" not in briefing.read_bytes()


def test_unit_fail_loud_missing_file(tmp_path, monkeypatch):
    """No BRIEFING.md at all -> BriefingContractError; the scanner refuses to
    create the hand-authored file it does not own."""
    briefing = tmp_path / "does_not_exist_BRIEFING.md"
    monkeypatch.setattr(scanner, "BRIEFING_FILE", briefing)
    with pytest.raises(scanner.BriefingContractError):
        scanner.replace_briefing_block(_fresh_block("NOPE"))
    assert not briefing.exists()


# ===========================================================================
# Integration level: the real scanner run end-to-end over a copied memory tree
# ===========================================================================

def _run_scanner(memory_dir: Path):
    env = dict(os.environ)
    env["PA_MEMORY_DIR"] = str(memory_dir)
    env["PA_WORKING_DIR"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(SCANNER_PY)],
        env=env, capture_output=True, text=True,
    )


def test_integration_scanner_preserves_hand_region(tmp_path):
    """Full scanner subprocess over a copy of the shipped memory tree: exit 0
    and the hand-authored region byte-identical before/after."""
    if not (MEMORY_TEMPLATE / "BRIEFING.md").exists():
        pytest.skip("shipped memory tree not present")
    mem = tmp_path / "memory"
    shutil.copytree(MEMORY_TEMPLATE, mem)
    briefing = mem / "BRIEFING.md"

    hand_before = _outside_regions(briefing.read_bytes())
    result = _run_scanner(mem)
    assert result.returncode == 0, f"scanner failed: {result.stderr}"
    hand_after = _outside_regions(briefing.read_bytes())

    assert hand_after == hand_before, "scanner mutated the hand-authored region"


def test_integration_scanner_fail_loud_on_stripped_markers(tmp_path):
    """Strip the marker pair, run the full scanner: it exits non-zero, names the
    contract error, and leaves BRIEFING.md byte-identical (no clobber)."""
    if not (MEMORY_TEMPLATE / "BRIEFING.md").exists():
        pytest.skip("shipped memory tree not present")
    mem = tmp_path / "memory"
    shutil.copytree(MEMORY_TEMPLATE, mem)
    briefing = mem / "BRIEFING.md"

    raw = briefing.read_bytes()
    stripped = raw.replace(START_TOK, b"").replace(END_TOK, b"")
    briefing.write_bytes(stripped)
    before = briefing.read_bytes()

    result = _run_scanner(mem)
    assert result.returncode != 0, "scanner should fail loud on missing markers"
    combined = result.stderr + result.stdout
    assert "missing the scanner marker pair" in combined
    assert "refusing to overwrite or append" in combined
    assert briefing.read_bytes() == before, "BRIEFING was clobbered on fail-loud"


# ===========================================================================
# Per-workstream handoff-target contract (v1.1)
# ===========================================================================
#
# The HAS writer subagent follows `has-subagent-prompt.md`. v1.1 moves the
# handoff target from a single central `hand-offs/` to the ACTIVE workstream's
# own `<tree>/<workstream>/hand-offs/`, passed in as the `{{HANDOFF_DIR}}`
# template variable by the `/wrap` caller.
#
# The writer is LLM-driven, so these tests pin the DETERMINISTIC half of the
# contract: the write-target expression the prompt prescribes. They extract that
# expression straight from the prompt, render it with the same substitution
# `/wrap` would perform against a synthetic workstream under
# `content/work/<ws>/`, and assert (a) it lands inside that workstream's
# `hand-offs/` and (b) it never resolves to a central location. If the prompt
# ever reverts to `{{MEMORY_DIR}}/hand-offs/...`, rendering routes the file to a
# central dir and both tests fail.

PROMPT_FILE = HERE / "has-subagent-prompt.md"


def _write_target_expr(prompt_text: str) -> str:
    """Extract the Phase 5 write-target path expression (the fenced block right
    after 'Write a new handoff file at:'), verbatim, template vars intact."""
    m = re.search(
        r"Write a new handoff file at:\s*```[^\n]*\n(.+?)\n```",
        prompt_text,
        re.S,
    )
    assert m, "Phase 5 write-target fenced block not found in prompt"
    return m.group(1).strip()


def _render(expr: str, subs: dict) -> str:
    for var, val in subs.items():
        expr = expr.replace("{{" + var + "}}", str(val))
    return expr


def test_prompt_no_central_handoff_path():
    """No-central-write contract at the prompt level: the writer prompt targets
    the per-workstream {{HANDOFF_DIR}} and carries no central
    {{MEMORY_DIR}}/hand-offs/ write path anywhere."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    assert "{{HANDOFF_DIR}}" in text, "prompt no longer references {{HANDOFF_DIR}}"
    assert "{{MEMORY_DIR}}/hand-offs" not in text, (
        "prompt still contains a central {{MEMORY_DIR}}/hand-offs path"
    )
    # Every filesystem command that touches hand-offs must go through
    # {{HANDOFF_DIR}}, never {{MEMORY_DIR}}.
    for line in text.splitlines():
        if "{{MEMORY_DIR}}" in line and "hand-off" in line:
            raise AssertionError(
                f"central handoff path leaked into prompt line: {line!r}"
            )


def test_writer_lands_per_workstream(tmp_path):
    """Per-workstream-append assertion: rendering the prompt's write-target
    expression for a synthetic workstream under content/work/<ws>/ resolves to
    that workstream's hand-offs/ dir, and the simulated write lands there."""
    ws = "synthetic-ws"
    mem = tmp_path / "memory"
    ws_dir = mem / "content" / "work" / ws
    handoff_dir = ws_dir / "hand-offs"
    handoff_dir.mkdir(parents=True)

    filename = "2026-09-01_01_synthetic.md"
    expr = _write_target_expr(PROMPT_FILE.read_text(encoding="utf-8"))
    resolved = Path(
        _render(
            expr,
            {
                "HANDOFF_DIR": handoff_dir,
                "MEMORY_DIR": mem,
                "HANDOFF_FILENAME": filename,
            },
        )
    )

    # The rendered target sits inside the workstream's own hand-offs/ dir.
    assert resolved.parent == handoff_dir, (
        f"write target {resolved} is not the workstream hand-offs dir {handoff_dir}"
    )
    assert ws in resolved.parts, "resolved target is not scoped to the workstream"

    # Simulate the writer's Phase 5 filesystem effect and confirm it lands there.
    resolved.write_text("---\nworkstream_id: synthetic-ws\n---\nbody\n", encoding="utf-8")
    assert resolved.exists()
    assert (handoff_dir / filename).exists()


def test_writer_never_writes_central(tmp_path):
    """No-central-write assertion: after the simulated per-workstream write, no
    central hand-offs/ directory exists at the memory-tree root (nor at any tier
    root above the workstream)."""
    ws = "synthetic-ws"
    mem = tmp_path / "memory"
    ws_dir = mem / "content" / "work" / ws
    handoff_dir = ws_dir / "hand-offs"
    handoff_dir.mkdir(parents=True)

    filename = "2026-09-01_01_synthetic.md"
    expr = _write_target_expr(PROMPT_FILE.read_text(encoding="utf-8"))
    resolved = Path(
        _render(
            expr,
            {
                "HANDOFF_DIR": handoff_dir,
                "MEMORY_DIR": mem,
                "HANDOFF_FILENAME": filename,
            },
        )
    )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text("---\nworkstream_id: synthetic-ws\n---\nbody\n", encoding="utf-8")

    # No central hand-offs dir should have been created anywhere above the
    # workstream: not at the memory root, the content/ tier, or content/work/.
    for central in (mem / "hand-offs",
                    mem / "content" / "hand-offs",
                    mem / "content" / "work" / "hand-offs"):
        assert not central.exists(), f"central handoff dir was created: {central}"


# ===========================================================================
# Scanner-emitted MAP resolution contract (H3) + no-central-handoff (H5)
# ===========================================================================
#
# H3: /wrap Step 4 resolves the active workstream's folder from MAP.md. The
# scanner's build_map() emits per-workstream bullets of the form
#   - **[Display Name](relative/path/)** - `workstream_id` - aliases: ...
# and NOT a `Location`-column table. Once a live scan runs, any table-column
# lookup no longer exists. These tests generate a FRESH MAP with the real
# scanner over a fixture tree and assert the bullet-link resolution wrap.md now
# documents actually finds the workstream folder.
#
# H5: the scanner discovers handoffs ONLY beneath each workstream's own
# hand-offs/. A file left at the (now forbidden) central memory/hand-offs/ must
# be ignored - never selected as a latest handoff, even when it is newer.

WRAP_FILE = REPO_ROOT / ".claude" / "commands" / "wrap.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_ws_fixture(tmp_path, *, central_handoff=False):
    """Copy the shipped memory tree and add one workstream under content/work
    with a per-workstream handoff, wired roots + aliases. Optionally plant a
    NEWER central handoff at memory/hand-offs/. Returns (mem, config, ws_rel, wid).
    """
    mem = tmp_path / "memory"
    shutil.copytree(MEMORY_TEMPLATE, mem)

    wid = "synthetic-ws"
    ws_rel = "content/work/synthetic-ws"
    ws_dir = mem / "content" / "work" / "synthetic-ws"
    (ws_dir / "hand-offs").mkdir(parents=True)

    _write(
        ws_dir / "README.md",
        "---\n"
        f"workstream_id: {wid}\n"
        "display_name: Synthetic WS\n"
        "status: active\n"
        "type: workstream\n"
        "purpose: Fixture workstream for scanner contract tests.\n"
        "---\n\n# Synthetic WS\n",
    )

    # Per-workstream handoff (the ONLY location the scanner may honor).
    _write(
        ws_dir / "hand-offs" / "2026-09-01_01_local.md",
        "---\n"
        f"workstream_id: {wid}\n"
        "session_end: 2026-09-01T10:00:00+00:00\n"
        "next: LOCAL-HANDOFF-NEXT\n"
        "blockers: none\n"
        "open_items: none\n"
        "status: active\n"
        "---\n\nlocal per-workstream handoff\n",
    )

    if central_handoff:
        # NEWER than the local one. If the scanner still honored a central dir,
        # this would win on session_end. It must be IGNORED.
        _write(
            mem / "hand-offs" / "2026-09-02_01_central.md",
            "---\n"
            f"workstream_id: {wid}\n"
            "session_end: 2026-09-02T23:00:00+00:00\n"
            "next: CENTRAL-HANDOFF-NEXT\n"
            "blockers: none\n"
            "open_items: none\n"
            "status: active\n"
            "---\n\ncentral handoff that must be ignored\n",
        )

    # aliases entry so discovery is CRITICAL-free.
    _write(
        mem / "aliases.yml",
        "workstreams:\n"
        f"  - workstream_id: {wid}\n"
        "    display_name: Synthetic WS\n"
        f"    folder: {ws_rel}\n"
        "    status: active\n"
        "    aliases:\n"
        "      - sws\n",
    )

    # Config: point roots at content/work (PA_CONFIG_FILE is set to this file).
    config = mem / "workstream_config.yml"
    config.write_text(
        "paths:\n"
        f"  memory_dir: {mem.as_posix()}\n"
        f"  working_dir: {REPO_ROOT.as_posix()}\n"
        "roots:\n"
        "  - content/work\n"
        "exclude:\n",
        encoding="utf-8",
    )
    return mem, config, ws_rel, wid


def _run_scanner_cfg(mem: Path, config: Path):
    env = dict(os.environ)
    env["PA_MEMORY_DIR"] = str(mem)
    env["PA_WORKING_DIR"] = str(REPO_ROOT)
    env["PA_CONFIG_FILE"] = str(config)
    return subprocess.run(
        [sys.executable, str(SCANNER_PY)],
        env=env, capture_output=True, text=True,
    )


def _resolve_ws_folder_from_map(map_text: str, wid: str):
    """The resolution wrap.md Step 4 documents: find the workstream bullet whose
    backtick-wrapped id equals `wid` and read the folder from that same line's
    Markdown link target `[Display Name](relative/path/)` (path relative to
    MEMORY_DIR). Returns the folder path without its trailing slash, or None."""
    pattern = re.compile(
        r"^- \*\*\[.+?\]\((.+?)\)\*\* - `" + re.escape(wid) + r"`",
        re.M,
    )
    m = pattern.search(map_text)
    if not m:
        return None
    return m.group(1).rstrip("/")


def test_wrap_doc_describes_map_bullet_link_resolution():
    """The wrap.md resolution must key off the scanner-stable MAP bullet link,
    not a `Location` column the scanner never emits."""
    text = WRAP_FILE.read_text(encoding="utf-8")
    assert "MAP.md" in text
    # The doc names the bullet-link target and the backtick id as the anchors.
    assert "(relative/path/)" in text, (
        "wrap.md no longer documents the Markdown link-target folder resolution"
    )
    assert "Location` column" not in text and "Location column" not in text, (
        "wrap.md still references a Location column the scanner does not emit"
    )


def test_h3_map_resolution_finds_workstream_folder(tmp_path):
    """Generate a FRESH MAP with the real scanner and assert the wrap.md-
    documented bullet-link resolution recovers the workstream's folder path."""
    if not (MEMORY_TEMPLATE / "BRIEFING.md").exists():
        pytest.skip("shipped memory tree not present")
    mem, config, ws_rel, wid = _build_ws_fixture(tmp_path)
    result = _run_scanner_cfg(mem, config)
    assert result.returncode == 0, (
        f"scanner failed: rc={result.returncode}\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    map_text = (mem / "MAP.md").read_text(encoding="utf-8")
    # Sanity: the scanner really emitted a bullet section, not the stub table.
    assert "Location |" not in map_text, "MAP still carries a Location table"

    resolved = _resolve_ws_folder_from_map(map_text, wid)
    assert resolved == ws_rel, (
        f"bullet-link resolution returned {resolved!r}, expected {ws_rel!r}.\n"
        f"MAP:\n{map_text}"
    )
    # The resolved path points at a real workstream folder with a hand-offs/ dir.
    assert (mem / resolved).is_dir()
    assert (mem / resolved / "hand-offs").is_dir()


def test_h5_central_handoff_ignored_integration(tmp_path):
    """A NEWER handoff at central memory/hand-offs/ must NOT be selected: the
    workstream's latest handoff stays the older per-workstream one, and the
    central file is left on disk (ignored, not deleted)."""
    if not (MEMORY_TEMPLATE / "BRIEFING.md").exists():
        pytest.skip("shipped memory tree not present")
    mem, config, ws_rel, wid = _build_ws_fixture(tmp_path, central_handoff=True)
    central = mem / "hand-offs" / "2026-09-02_01_central.md"
    assert central.exists(), "fixture failed to plant the central handoff"

    result = _run_scanner_cfg(mem, config)
    assert result.returncode == 0, (
        f"scanner failed: rc={result.returncode}\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    map_text = (mem / "MAP.md").read_text(encoding="utf-8")
    briefing = (mem / "BRIEFING.md").read_text(encoding="utf-8")

    # The per-workstream (older) handoff wins; the central (newer) one is ignored.
    assert "Latest handoff: 2026-09-01" in map_text, (
        f"expected the per-workstream 2026-09-01 handoff to be latest.\n{map_text}"
    )
    assert "Latest handoff: 2026-09-02" not in map_text, (
        "the newer CENTRAL handoff leaked into MAP - central dir still honored"
    )
    assert "CENTRAL-HANDOFF-NEXT" not in briefing, (
        "central handoff content surfaced in BRIEFING - central dir still honored"
    )
    # The scanner ignored the central file; it did not delete it.
    assert central.exists(), "scanner unexpectedly removed the central handoff"


def test_h5_collect_handoff_candidates_excludes_central(tmp_path):
    """Unit-level: collect_handoff_candidates returns ONLY per-workstream
    hand-offs; a file at the memory-root central hand-offs/ is never a candidate.
    Independent of the module-level MEMORY_DIR (the central branch is gone)."""
    mem = tmp_path / "memory"
    ws_dir = mem / "content" / "work" / "synthetic-ws"
    local_ho = ws_dir / "hand-offs"
    local_ho.mkdir(parents=True)
    local_file = local_ho / "2026-09-01_01_local.md"
    local_file.write_text("---\nworkstream_id: synthetic-ws\n---\n", encoding="utf-8")

    central_dir = mem / "hand-offs"
    central_dir.mkdir(parents=True)
    central_file = central_dir / "2026-09-02_01_central.md"
    central_file.write_text("---\nworkstream_id: synthetic-ws\n---\n", encoding="utf-8")

    ws = scanner.Workstream(
        workstream_id="synthetic-ws",
        display_name="Synthetic WS",
        status="active",
        folder=ws_dir,
        readme_path=ws_dir / "README.md",
    )
    candidates = scanner.collect_handoff_candidates({"synthetic-ws": ws})
    resolved = {p.resolve() for p in candidates}

    assert local_file.resolve() in resolved, "per-workstream handoff was dropped"
    assert central_file.resolve() not in resolved, (
        "central memory/hand-offs/ file is still collected as a candidate"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
