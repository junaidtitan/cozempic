"""Generate a compact conversation recap from a session JSONL."""

from __future__ import annotations

import re
from pathlib import Path

from .helpers import get_content_blocks, get_msg_type, text_of
from .types import Message


def _extract_text(msg: dict) -> str:
    """Extract readable text from a message, stripping system tags and noise."""
    blocks = get_content_blocks(msg)
    parts = []
    for block in blocks:
        if block.get("type") == "text":
            parts.append(text_of(block))
    return " ".join(parts)


def _clean_user_text(text: str) -> str:
    """Remove system tags, command noise, and whitespace from user text."""
    # Strip XML-style system tags and their content
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    text = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "", text, flags=re.DOTALL)
    text = re.sub(r"<command-name>.*?</command-name>", "", text, flags=re.DOTALL)
    text = re.sub(r"<command-message>.*?</command-message>", "", text, flags=re.DOTALL)
    text = re.sub(r"<command-args>.*?</command-args>", "", text, flags=re.DOTALL)
    text = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", text, flags=re.DOTALL)
    # Strip any remaining XML-style tags
    text = re.sub(r"<[^>]+>.*?</[^>]+>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+/?>", "", text)
    # Strip common noise patterns
    text = re.sub(r"SessionStart:.*", "", text)
    text = re.sub(r"\[Request interrupted by user.*?\]", "", text)
    # Strip Claude Code UI chrome
    text = re.sub(r"[▖▗▘▝▚▞]+", "", text)
    text = re.sub(r"Claude Code v[\d.]+", "", text)
    text = re.sub(r"Opus \d+\.\d+ · Claude \w+", "", text)
    text = re.sub(r"~/Documents/\S+", "", text)
    # Strip markdown headers/formatting for compactness
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _first_sentence(text: str, max_len: int = 100) -> str:
    """Extract the first meaningful sentence from text."""
    # Find sentence boundary
    match = re.search(r"[.!?]\s", text[:max_len + 50])
    if match and match.end() <= max_len + 10:
        return text[: match.start() + 1]
    # No sentence boundary — just truncate
    if len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text


def _clean_assistant_text(text: str) -> str:
    """Extract a concise first-sentence summary from assistant text."""
    text = re.sub(r"\s+", " ", text).strip()
    return _first_sentence(text, max_len=100)


def generate_recap(messages: list[Message], max_turns: int = 40) -> str:
    """Generate a compact conversation recap.

    Shows the full conversation arc — user messages (cleaned, truncated)
    and assistant responses (first sentence only).
    """
    turns: list[tuple[str, str]] = []  # (role, text)

    for _, msg, _ in messages:
        msg_type = get_msg_type(msg)

        if msg_type == "user":
            text = _extract_text(msg)
            text = _clean_user_text(text)
            if not text or len(text) < 3:
                continue
            turns.append(("you", text))

        elif msg_type == "assistant":
            text = _extract_text(msg)
            text = _clean_assistant_text(text)
            if not text or len(text) < 3:
                continue
            turns.append(("claude", text))

    if not turns:
        return ""

    # Deduplicate consecutive same-role turns (merge them)
    merged: list[tuple[str, str]] = []
    for role, text in turns:
        if merged and merged[-1][0] == role:
            # For assistant, keep only the first chunk (already summarized)
            if role == "claude":
                continue
            merged[-1] = (role, merged[-1][1] + " " + text)
        else:
            merged.append((role, text))

    # Bookend: show first few + gap + last batch
    head_count = 5
    tail_count = max_turns - head_count - 1  # -1 for the gap line

    # Format
    lines = []
    lines.append("")
    lines.append("  ╔══════════════════════════════════════════════════════════════╗")
    lines.append("  ║              PREVIOUSLY ON THIS SESSION                     ║")
    lines.append("  ╚══════════════════════════════════════════════════════════════╝")
    lines.append("")

    def _fmt(role: str, text: str) -> str:
        if role == "you":
            if len(text) > 90:
                text = text[:87] + "..."
            return f"  > {text}"
        else:
            return f"    -> {text}"

    if len(merged) <= max_turns:
        # Everything fits
        for role, text in merged:
            lines.append(_fmt(role, text))
    else:
        # Head
        for role, text in merged[:head_count]:
            lines.append(_fmt(role, text))
        skipped = len(merged) - head_count - tail_count
        lines.append(f"  ... ({skipped} turns skipped) ...")
        # Tail
        for role, text in merged[-tail_count:]:
            lines.append(_fmt(role, text))

    lines.append("")
    lines.append("  ── context pruned by cozempic ── resuming ──")
    lines.append("")

    return "\n".join(lines)


def save_recap(messages: list[Message], dest: Path, max_turns: int = 40) -> Path:
    """Generate and save recap to a file. Returns the path."""
    recap = generate_recap(messages, max_turns)
    dest.write_text(recap)
    return dest
