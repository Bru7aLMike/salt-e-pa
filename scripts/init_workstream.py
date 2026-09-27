#!/usr/bin/env python3
"""Bootstrap a new, empty workstream.

Creates a workstream folder with:
  - README.md (required frontmatter filled, body left as prompts to complete)
  - TODO.md (the workstream backlog, rendered from the shipped template at
    <memory_dir>/system/workspace/templates/TODO.md)
  - hand-offs/ subfolder (empty)
And registers the workstream in memory/aliases.yml under the `workstreams:` list
that the workspace scanner reads.

Usage:
  python init_workstream.py --slug my-new-thing \
      --name "My New Thing" \
      --root <root-folder> \
      --aliases "alias one,alias two" \
      --status active \
      --summary "One-line summary of the workstream"

Only --name (or --slug) is required; every other field falls back to a safe
default. Fields are prompted only when stdin is a terminal, so the script is
safe to run non-interactively (a missing field then uses its default). Preview
without writing using --dry-run.

Why this is a script and not a plain file Write:
  A workstream README must land on disk exactly as written. Some agent harnesses
  route a "Write a new .md" action through an auto-memory layer that wraps the
  file in a name/metadata/node_type envelope and nests workstream_id under
  metadata. The scanner reads workstream_id at the top level only, so a wrapped
  README scans as missing its workstream_id and raises errors. This script
  writes the README and TODO.md with Path.write_text(), which bypasses any
  such wrap.
"""
import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# ----- Config-driven path resolution ---------------------------------------
#
# Nothing personal is hard-coded. Paths resolve with a fixed precedence, so a
# cloner never edits this file:
#   1. environment variable (the per-key PA_* name)
#   2. the value in memory/workstream_config.yml
#   3. a repo-relative default
#
# The config file itself is located by PA_CONFIG_FILE, else the repo-relative
# default memory/workstream_config.yml (relative to this script's parent dir).

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPO_ROOT = SCRIPT_DIR.parent

_PLACEHOLDER_RE = re.compile(r"^\s*(?:\{\{[A-Z0-9_]+\}\}|<[A-Z0-9_]+>)\s*$")


def _is_placeholder(value: Any) -> bool:
    """True if a config value is unset or a shipped placeholder token."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return not value.strip() or bool(_PLACEHOLDER_RE.match(value.strip()))


def _load_config_raw(path: Path) -> dict:
    """Best-effort YAML load of the config file. Empty dict on any error."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_path(env_var: str, config_value: Any, default: Path) -> Path:
    env = os.environ.get(env_var)
    if env and env.strip():
        return Path(env).expanduser()
    if isinstance(config_value, str) and not _is_placeholder(config_value):
        return Path(config_value).expanduser()
    return default


_cfg_env = os.environ.get("PA_CONFIG_FILE")
CONFIG_FILE = (
    Path(_cfg_env).expanduser() if _cfg_env and _cfg_env.strip()
    else DEFAULT_REPO_ROOT / "memory" / "workstream_config.yml"
)
_CONFIG = _load_config_raw(CONFIG_FILE)
_PATHS = _CONFIG.get("paths") if isinstance(_CONFIG.get("paths"), dict) else {}

MEMORY_DIR = _resolve_path(
    "PA_MEMORY_DIR", _PATHS.get("memory_dir"), DEFAULT_REPO_ROOT / "memory"
)
ALIASES_FILE = MEMORY_DIR / "aliases.yml"

# The TODO.md template ships in the memory tree. A memory dir set up before the
# template existed falls back to the copy shipped next to this script.
TODO_TEMPLATE_REL = Path("system") / "workspace" / "templates" / "TODO.md"
TODO_TEMPLATE_CANDIDATES = (
    MEMORY_DIR / TODO_TEMPLATE_REL,
    DEFAULT_REPO_ROOT / "memory" / TODO_TEMPLATE_REL,
)

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
VALID_STATUS = {"active", "dormant", "complete"}


def die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"init_workstream: {msg}\n")
    sys.exit(code)


def prompt(label: str, default: str = "") -> str:
    """Ask for a value, but only when stdin is a terminal.

    Non-interactive callers (subagents, CI) get the default instead of hanging
    on input().
    """
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return raw or default


def slugify(text: str) -> str:
    """Derive a lowercase-kebab-case slug from arbitrary display text."""
    s = text.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def load_existing_slugs() -> set[str]:
    """Slugs already registered in aliases.yml. Empty set on any read error."""
    if not ALIASES_FILE.exists():
        return set()
    try:
        data = yaml.safe_load(ALIASES_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    return {
        str(entry.get("workstream_id"))
        for entry in (data.get("workstreams") or [])
        if isinstance(entry, dict) and entry.get("workstream_id")
    }


def load_roots() -> list[str]:
    """Configured discovery roots (folders under memory_dir), if any."""
    return list(_CONFIG.get("roots") or [])


def yaml_scalar(value: str) -> str:
    """Return `value` as a one-line YAML scalar that parses back unchanged.

    Plain text is emitted as-is. Text YAML would misread (a colon-space, a
    leading quote or bracket) is emitted double-quoted; JSON string escaping is
    valid YAML double-quoted syntax. An empty value stays empty.
    """
    if not value:
        return value
    try:
        if yaml.safe_load(f"k: {value}") == {"k": value}:
            return value
    except yaml.YAMLError:
        pass
    return json.dumps(value, ensure_ascii=False)


def readme_purpose(name: str, summary: str) -> str:
    """OKF one-line summary for the README: the summary, else a default."""
    return summary.strip() or f"Workstream README for {name}."


def render_readme(
    slug: str, name: str, status: str, summary: str, today: str,
) -> str:
    return (
        "---\n"
        "type: workstream\n"
        f"purpose: {yaml_scalar(readme_purpose(name, summary))}\n"
        f"workstream_id: {slug}\n"
        f"display_name: {yaml_scalar(name)}\n"
        f"status: {status}\n"
        f"created: {today}\n"
        f"summary: {yaml_scalar(summary)}\n"
        "---\n"
        "\n"
        "## Active handoffs\n"
        "\n"
        "<!-- /wrap appends a handoff entry here at the end of each session. -->\n"
        "\n"
        "## What it is\n"
        "\n"
        f"_Describe {name} here. What is this workstream, who owns it, and "
        f"what does success look like?_\n"
        "\n"
        "Backlog: TODO.md (read on demand)\n"
        "\n"
        "## Key artifacts\n"
        "\n"
        "- _Add links to plans, reference docs, or related READMEs._\n"
    )


def find_todo_template() -> Path | None:
    """First existing TODO.md template path, or None."""
    for candidate in TODO_TEMPLATE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def render_todo(template: str, slug: str, name: str) -> str:
    """Fill the TODO.md template slots for one workstream."""
    purpose = (f"Backlog for {name} - someday items for this workstream "
               f"only, no ticket, not next.")
    return (
        template
        .replace("<PURPOSE>", yaml_scalar(purpose))
        .replace("<WORKSTREAM_ID>", slug)
        .replace("{{DISPLAY_NAME}}", name)
    )


def append_alias_entry(
    slug: str, name: str, folder_rel: str, status: str, aliases: list[str],
) -> None:
    """Register the workstream under the top-level `workstreams:` list.

    Appends as YAML text (preserves any existing comments). Creates the
    `workstreams:` key and the file itself if either is missing.
    """
    existing = ""
    if ALIASES_FILE.exists():
        existing = ALIASES_FILE.read_text(encoding="utf-8")
    if existing and not existing.endswith("\n"):
        existing += "\n"

    lines: list[str] = []
    has_key = bool(re.search(r"^workstreams:\s*$", existing, re.MULTILINE))
    if not has_key:
        lines.append("")
        lines.append("workstreams:")
    lines.append(f"  - workstream_id: {slug}")
    lines.append(f"    display_name: {name}")
    lines.append(f"    folder: {folder_rel}")
    lines.append(f"    status: {status}")
    if aliases:
        lines.append("    aliases:")
        for a in aliases:
            lines.append(f"      - {a}")
    else:
        lines.append("    aliases: []")

    ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALIASES_FILE.write_text(
        existing + "\n".join(lines) + "\n", encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Bootstrap a new workstream.")
    ap.add_argument("--slug", help="workstream_id (lowercase-kebab-case)")
    ap.add_argument("--name", help="display_name")
    ap.add_argument("--root", help=(
        "One of the roots listed in workstream_config.yml. Omit to place the "
        "workstream directly under memory_dir."
    ))
    ap.add_argument("--status", default="active",
                    choices=sorted(VALID_STATUS))
    ap.add_argument("--summary", default="", help="One-line summary")
    ap.add_argument("--aliases", default="",
                    help="Comma-separated alias terms")
    ap.add_argument("--folder-name", default="", help=(
        "Name of the folder under the root. Defaults to the slug."
    ))
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would happen without writing")
    args = ap.parse_args()

    existing = load_existing_slugs()
    roots = load_roots()
    today = date.today().isoformat()

    name = args.name or prompt("display_name")
    slug = args.slug or slugify(name)
    if not slug:
        die("could not derive a slug - pass --slug or --name")
    if not SLUG_RE.match(slug):
        die(f"invalid slug `{slug}`: must be lowercase-kebab-case "
            f"(e.g. my-new-thing)")
    if slug in existing:
        die(f"workstream_id `{slug}` already exists in aliases.yml")

    if not name:
        name = slug.replace("-", " ").title()

    root = args.root or prompt(
        f"root (one of: {', '.join(roots) or 'none configured'})",
        default=roots[0] if roots else "",
    )
    if roots and root and root not in roots:
        sys.stderr.write(
            f"Warning: root `{root}` is not in workstream_config.yml. "
            f"The scanner will not discover this workstream until you add it.\n"
        )

    status = args.status
    if status not in VALID_STATUS:
        die(f"status must be one of {sorted(VALID_STATUS)}, got `{status}`")

    summary = args.summary or prompt("summary (one line)", default="")
    aliases_raw = args.aliases or prompt(
        "aliases (comma-separated)", default="")
    aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]

    folder_name = args.folder_name or slug
    if root:
        folder_path = MEMORY_DIR / root / folder_name
        folder_rel = f"{root}/{folder_name}"
    else:
        folder_path = MEMORY_DIR / folder_name
        folder_rel = folder_name
    readme_path = folder_path / "README.md"
    todo_path = folder_path / "TODO.md"
    handoffs_path = folder_path / "hand-offs"

    readme_content = render_readme(slug, name, status, summary, today)
    todo_template = find_todo_template()
    if todo_template is None:
        die("TODO.md template not found; looked in: "
            + ", ".join(str(c) for c in TODO_TEMPLATE_CANDIDATES))
    todo_content = render_todo(
        todo_template.read_text(encoding="utf-8"), slug, name)

    print("--- Plan ---")
    print(f"Create folder: {folder_path}")
    print(f"Create file:   {readme_path}")
    print(f"Create file:   {todo_path}")
    print(f"Create folder: {handoffs_path}")
    print(f"Register in:   {ALIASES_FILE}")
    print(f"  - slug: {slug}")
    print(f"  - display_name: {name}")
    print(f"  - aliases: {aliases or '[]'}")
    if args.dry_run:
        print("\n[dry-run] No changes written.")
        return 0

    if folder_path.exists():
        die(f"folder already exists: {folder_path} - aborting to avoid "
            f"overwriting any existing work")

    folder_path.mkdir(parents=True, exist_ok=False)
    handoffs_path.mkdir(exist_ok=False)
    readme_path.write_text(readme_content, encoding="utf-8")
    todo_path.write_text(todo_content, encoding="utf-8")
    append_alias_entry(slug, name, folder_rel, status, aliases)

    print(f"\nCreated workstream `{slug}` at {folder_rel}.")
    if not root or root not in roots:
        print(
            f"Note: {folder_rel} is not under any 'roots:' entry in "
            f"workstream_config.yml; the workspace scanner will not discover "
            f"it until you add its parent folder to roots:."
        )
    print("Next: write a handoff, or run the scanner to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
