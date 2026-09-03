#!/usr/bin/env python
"""HAS transcript filter - content-type-aware stripping + Bash normalization + chunking.

Reads a Claude Code transcript JSONL, strips low-signal content, preserves git operations
and all user/assistant text, outputs human-readable filtered transcript with optional chunking.

Usage:
    python has-filter.py <input.jsonl> <output_base> [--chunk-size TOKENS] [--verbose]
"""

import json
import re
import sys
import argparse
from pathlib import Path

ALWAYS_STRIP_TOOLS = {"Grep", "Glob", "Read"}
ALWAYS_KEEP_RESULT_TOOLS = {"Write", "Edit", "Agent"}
SIZE_GATE_BYTES = 2048
DEFAULT_CHUNK_TOKENS = 15000
CHARS_PER_TOKEN = 4
SYSTEM_REMINDER_RE = re.compile(r'<system-reminder>.*?</system-reminder>', re.DOTALL)


def strip_system_reminders(text: str) -> str:
    """Remove <system-reminder> blocks from text content."""
    return SYSTEM_REMINDER_RE.sub('', text).strip()


def normalize_bash_command(command: str) -> str:
    """Strip env prefixes and path prefixes, return the base command name."""
    cmd = command.strip()
    # Strip leading env var assignments: KEY=VALUE or KEY="VALUE"
    cmd = re.sub(r'^(\w+=\S+\s+)+', '', cmd)
    # Extract first token (the command)
    first = cmd.split()[0] if cmd.split() else ""
    # Strip path prefix (Unix or Windows)
    base = re.sub(r'^.*[/\\]', '', first)
    return base


def is_git_command(command: str) -> bool:
    """Classify a Bash command as git or non-git after normalization."""
    base = normalize_bash_command(command)
    return bool(re.match(r'^git(\.exe)?$', base, re.IGNORECASE))


def extract_tool_use(content_block):
    """Extract tool name and input from a tool_use content block."""
    return content_block.get("name", ""), content_block.get("input", {})


def format_tool_use(name, inp):
    """Format a tool_use block for output."""
    lines = [f"=== TOOL_USE: {name} ==="]
    if name == "Bash":
        lines.append(f"command: {inp.get('command', '')}")
    elif name == "Read":
        lines.append(f"file_path: {inp.get('file_path', '')}")
    elif name == "Edit":
        lines.append(f"file_path: {inp.get('file_path', '')}")
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        if old:
            lines.append(f"old_string: {old[:200]}{'...' if len(old) > 200 else ''}")
        if new:
            lines.append(f"new_string: {new[:200]}{'...' if len(new) > 200 else ''}")
    elif name == "Write":
        lines.append(f"file_path: {inp.get('file_path', '')}")
        content = inp.get("content", "")
        lines.append(f"content: [{len(content)} chars]")
    elif name == "Agent":
        lines.append(f"description: {inp.get('description', '')}")
        prompt = inp.get("prompt", "")
        lines.append(f"prompt: {prompt[:300]}{'...' if len(prompt) > 300 else ''}")
    else:
        for k, v in inp.items():
            v_str = str(v)
            lines.append(f"{k}: {v_str[:200]}{'...' if len(v_str) > 200 else ''}")
    return "\n".join(lines)


def format_tool_result(tool_name, content, is_git=False):
    """Format a tool_result block, applying size gating as appropriate."""
    if isinstance(content, list):
        text = "\n".join(
            c.get("text", "") if isinstance(c, dict) else str(c)
            for c in content
        )
    elif isinstance(content, str):
        text = content
    else:
        text = str(content) if content else ""

    text = strip_system_reminders(text)

    # Always strip
    if tool_name in ALWAYS_STRIP_TOOLS:
        return f"=== TOOL_RESULT: {tool_name} [STRIPPED] ==="

    # Always keep (full)
    if tool_name in ALWAYS_KEEP_RESULT_TOOLS:
        return f"=== TOOL_RESULT: {tool_name} ===\n{text}"

    # Bash: keep git results fully, size-gate non-git
    if tool_name == "Bash":
        label = "git" if is_git else "non-git"
        if is_git:
            return f"=== TOOL_RESULT: Bash ({label}) ===\n{text}"
        elif len(text) > SIZE_GATE_BYTES:
            return f"=== TOOL_RESULT: Bash ({label}) [TRUNCATED - {len(text)} bytes] ===\n{text[:500]}..."
        else:
            return f"=== TOOL_RESULT: Bash ({label}) ===\n{text}"

    # Everything else: size-gate
    if len(text) > SIZE_GATE_BYTES:
        return f"=== TOOL_RESULT: {tool_name} [TRUNCATED - {len(text)} bytes] ===\n{text[:500]}..."
    return f"=== TOOL_RESULT: {tool_name} ===\n{text}"


def process_transcript(input_path: str, verbose: bool = False):
    """Process a transcript JSONL file and return filtered output blocks."""
    blocks = []
    stats = {"total": 0, "kept": 0, "stripped": 0, "errors": 0}

    # Track tool_use IDs to tool names for matching with tool_results
    tool_use_map = {}  # tool_use_id -> (tool_name, is_git)

    with open(input_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            stats["total"] += 1

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                stats["errors"] += 1
                if verbose:
                    print(f"WARN: malformed JSON at line {line_num}", file=sys.stderr)
                continue

            msg_type = obj.get("type", "")

            # Skip non-message entries (queue-operation, last-prompt, etc.)
            if msg_type in ("queue-operation", "last-prompt", "summary"):
                stats["stripped"] += 1
                continue

            # User messages: always keep
            if msg_type == "user":
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        texts = []
                        for c in content:
                            if isinstance(c, dict):
                                if c.get("type") == "text":
                                    t = strip_system_reminders(c.get("text", ""))
                                    if t:
                                        texts.append(t)
                                    continue
                                elif c.get("type") == "tool_result":
                                    # User turn can contain tool_results
                                    tool_id = c.get("tool_use_id", "")
                                    result_content = c.get("content", "")
                                    if tool_id in tool_use_map:
                                        tname, tgit = tool_use_map[tool_id]
                                        blocks.append(format_tool_result(tname, result_content, tgit))
                                    else:
                                        blocks.append(format_tool_result("unknown", result_content))
                                    stats["kept"] += 1
                        if texts:
                            blocks.append(f"=== USER ===\n{chr(10).join(texts)}")
                            stats["kept"] += 1
                    elif isinstance(content, str) and content:
                        blocks.append(f"=== USER ===\n{content}")
                        stats["kept"] += 1
                    else:
                        stats["stripped"] += 1
                else:
                    stats["stripped"] += 1
                continue

            # Assistant messages: keep text, process tool_use, strip thinking
            if msg_type == "assistant":
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type", "")

                            if btype == "thinking":
                                stats["stripped"] += 1
                                continue

                            if btype == "text":
                                text = strip_system_reminders(block.get("text", ""))
                                if text:
                                    blocks.append(f"=== ASSISTANT ===\n{text}")
                                    stats["kept"] += 1
                                continue

                            if btype == "tool_use":
                                name, inp = extract_tool_use(block)
                                tool_id = block.get("id", "")
                                git = False
                                if name == "Bash":
                                    git = is_git_command(inp.get("command", ""))
                                if tool_id:
                                    tool_use_map[tool_id] = (name, git)
                                blocks.append(format_tool_use(name, inp))
                                stats["kept"] += 1
                                continue

                            if btype == "tool_result":
                                tool_id = block.get("tool_use_id", "")
                                result_content = block.get("content", "")
                                if tool_id in tool_use_map:
                                    tname, tgit = tool_use_map[tool_id]
                                    blocks.append(format_tool_result(tname, result_content, tgit))
                                else:
                                    blocks.append(format_tool_result("unknown", result_content))
                                stats["kept"] += 1
                                continue
                continue

            # System messages: always strip
            if msg_type == "system":
                stats["stripped"] += 1
                continue

            # Everything else: strip
            stats["stripped"] += 1

    if verbose:
        print(f"Filter stats: {stats}", file=sys.stderr)

    return blocks, stats


def chunk_blocks(blocks, chunk_token_limit):
    """Split blocks into chunks at message boundaries, respecting token limit."""
    chunks = []
    current_chunk = []
    current_size = 0

    for block in blocks:
        block_size = len(block) // CHARS_PER_TOKEN

        # If adding this block exceeds limit AND current chunk is non-empty, start new chunk
        if current_size + block_size > chunk_token_limit and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = []
            current_size = 0

        current_chunk.append(block)
        current_size += block_size

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def main():
    parser = argparse.ArgumentParser(description="HAS transcript filter")
    parser.add_argument("input", help="Path to transcript JSONL")
    parser.add_argument("output_base", help="Base path for output (without extension)")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_TOKENS,
                        help=f"Chunk threshold in approx tokens (default: {DEFAULT_CHUNK_TOKENS})")
    parser.add_argument("--verbose", action="store_true", help="Print stats to stderr")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    blocks, stats = process_transcript(args.input, args.verbose)

    if not blocks:
        print("WARNING: no blocks extracted from transcript", file=sys.stderr)
        sys.exit(0)

    full_text = "\n\n".join(blocks)
    total_tokens = len(full_text) // CHARS_PER_TOKEN

    if args.verbose:
        print(f"Filtered output: {len(blocks)} blocks, ~{total_tokens} tokens", file=sys.stderr)

    if total_tokens <= args.chunk_size:
        out_path = f"{args.output_base}.txt"
        Path(out_path).write_text(full_text, encoding="utf-8")
        if args.verbose:
            print(f"Single file: {out_path}", file=sys.stderr)
        print(out_path)
    else:
        chunks = chunk_blocks(blocks, args.chunk_size)
        paths = []
        for i, chunk in enumerate(chunks, 1):
            chunk_path = f"{args.output_base}_chunk_{i:03d}.txt"
            Path(chunk_path).write_text(chunk, encoding="utf-8")
            paths.append(chunk_path)
        if args.verbose:
            print(f"Chunked into {len(chunks)} files", file=sys.stderr)
        for p in paths:
            print(p)


if __name__ == "__main__":
    main()
