"""Standard-tier strategies: recommended pruning with cross-message correlation."""

from __future__ import annotations

import hashlib
import json
import re

from ..helpers import get_content_blocks, get_msg_type, msg_bytes, set_content_blocks, text_of
from ..registry import strategy
from ..types import Message, PruneAction, StrategyResult


@strategy("thinking-blocks", "Truncate or remove thinking/signature blocks", "standard", "2-5%")
def strategy_thinking_blocks(messages: list[Message], config: dict) -> StrategyResult:
    """Remove or truncate thinking blocks and signatures from assistant messages.

    Modes (via config['thinking_mode']):
        'remove'         - Remove thinking blocks entirely (default)
        'truncate'       - Keep first 200 chars of thinking
        'signature-only' - Only strip signature fields
    """
    mode = config.get("thinking_mode", "remove")
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    replaced = 0

    for pos, (idx, msg, size) in enumerate(messages):
        if get_msg_type(msg) != "assistant":
            continue

        blocks = get_content_blocks(msg)
        if not blocks:
            continue

        new_blocks = []
        changed = False
        for block in blocks:
            btype = block.get("type", "")
            if btype == "thinking":
                changed = True
                if mode == "remove":
                    continue
                elif mode == "truncate":
                    thinking = block.get("thinking", "")
                    new_block = {k: v for k, v in block.items() if k != "signature"}
                    if len(thinking) > 200:
                        new_block["thinking"] = thinking[:200] + "...[truncated]"
                    new_blocks.append(new_block)
                elif mode == "signature-only":
                    new_block = {k: v for k, v in block.items() if k != "signature"}
                    new_blocks.append(new_block)
                    changed = new_block != block
            else:
                if "signature" in block:
                    changed = True
                    new_blocks.append({k: v for k, v in block.items() if k != "signature"})
                else:
                    new_blocks.append(block)

        if changed:
            new_msg = set_content_blocks(msg, new_blocks)
            new_size = msg_bytes(new_msg)
            saved = size - new_size
            if saved > 0:
                actions.append(PruneAction(
                    line_index=idx,
                    action="replace",
                    reason=f"thinking-blocks ({mode})",
                    original_bytes=size,
                    pruned_bytes=new_size,
                    replacement=new_msg,
                ))
                total_pruned += saved
                replaced += 1

    return StrategyResult(
        strategy_name="thinking-blocks",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Processed {replaced} thinking blocks (mode={mode})",
    )


@strategy("tool-output-trim", "Trim large tool_result blocks (>8KB or >100 lines)", "standard", "1-8%")
def strategy_tool_output_trim(messages: list[Message], config: dict) -> StrategyResult:
    """Trim oversized tool results while preserving structure."""
    max_bytes = config.get("tool_output_max_bytes", 8192)
    max_lines = config.get("tool_output_max_lines", 100)

    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    replaced = 0

    for pos, (idx, msg, size) in enumerate(messages):
        blocks = get_content_blocks(msg)
        if not blocks:
            continue

        new_blocks = []
        changed = False
        for block in blocks:
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, str):
                    content_bytes = len(content.encode("utf-8"))
                    content_lines = content.count("\n") + 1
                    if content_bytes > max_bytes or content_lines > max_lines:
                        lines = content.split("\n")
                        if len(lines) > max_lines:
                            keep = max_lines // 2
                            trimmed = (
                                lines[:keep]
                                + [f"\n... [{len(lines) - max_lines} lines trimmed by cozempic] ...\n"]
                                + lines[-keep:]
                            )
                            new_content = "\n".join(trimmed)
                        else:
                            half = max_bytes // 2
                            new_content = (
                                content[:half]
                                + f"\n... [{content_bytes - max_bytes} bytes trimmed by cozempic] ...\n"
                                + content[-half:]
                            )
                        new_blocks.append({**block, "content": new_content})
                        changed = True
                        continue
                elif isinstance(content, list):
                    block_json = json.dumps(content, separators=(",", ":"))
                    if len(block_json.encode("utf-8")) > max_bytes:
                        trimmed_content = []
                        for sub in content:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                text = sub.get("text", "")
                                if len(text.encode("utf-8")) > max_bytes:
                                    half = max_bytes // 2
                                    sub = {**sub, "text": text[:half] + "\n...[trimmed by cozempic]...\n" + text[-half:]}
                            trimmed_content.append(sub)
                        new_blocks.append({**block, "content": trimmed_content})
                        changed = True
                        continue
            new_blocks.append(block)

        if changed:
            new_msg = set_content_blocks(msg, new_blocks)
            new_size = msg_bytes(new_msg)
            saved = size - new_size
            if saved > 0:
                actions.append(PruneAction(
                    line_index=idx,
                    action="replace",
                    reason="tool-output-trim",
                    original_bytes=size,
                    pruned_bytes=new_size,
                    replacement=new_msg,
                ))
                total_pruned += saved
                replaced += 1

    return StrategyResult(
        strategy_name="tool-output-trim",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Trimmed {replaced} oversized tool outputs",
    )


@strategy("stale-reads", "Remove file reads superseded by later edits", "standard", "0.5-2%")
def strategy_stale_reads(messages: list[Message], config: dict) -> StrategyResult:
    """If a file was read and then later edited/written, the read result is stale."""
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0

    file_events: dict[str, list[tuple[int, str, int]]] = {}

    for pos, (idx, msg, size) in enumerate(messages):
        for block in get_content_blocks(msg):
            if block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                tool_input = block.get("input", {})
                if tool_name in ("Read", "read"):
                    fp = tool_input.get("file_path", "")
                    if fp:
                        file_events.setdefault(fp, []).append((pos, "read", idx))
                elif tool_name in ("Edit", "edit", "Write", "write"):
                    fp = tool_input.get("file_path", "")
                    if fp:
                        file_events.setdefault(fp, []).append((pos, "edit", idx))

    stale_read_positions: set[int] = set()
    for fp, events in file_events.items():
        events.sort(key=lambda x: x[0])
        for i, (pos, etype, idx) in enumerate(events):
            if etype == "read":
                for j in range(i + 1, len(events)):
                    if events[j][1] == "edit":
                        stale_read_positions.add(pos)
                        break

    for pos, (idx, msg, size) in enumerate(messages):
        if pos not in stale_read_positions:
            continue
        for block in get_content_blocks(msg):
            if block.get("type") == "tool_use" and block.get("name") in ("Read", "read"):
                tool_use_id = block.get("id", "")
                if not tool_use_id:
                    continue
                for fpos in range(pos + 1, min(pos + 5, len(messages))):
                    fidx, fmsg, fsize = messages[fpos]
                    for fb in get_content_blocks(fmsg):
                        if fb.get("type") == "tool_result" and fb.get("tool_use_id") == tool_use_id:
                            content = fb.get("content", "")
                            if isinstance(content, str) and len(content) > 500:
                                new_fb = {**fb, "content": "[stale read - file was later edited, trimmed by cozempic]"}
                                new_blocks = []
                                did_replace = False
                                for ob in get_content_blocks(fmsg):
                                    if ob.get("type") == "tool_result" and ob.get("tool_use_id") == tool_use_id and not did_replace:
                                        new_blocks.append(new_fb)
                                        did_replace = True
                                    else:
                                        new_blocks.append(ob)
                                new_msg = set_content_blocks(fmsg, new_blocks)
                                new_size = msg_bytes(new_msg)
                                saved = fsize - new_size
                                if saved > 0:
                                    actions.append(PruneAction(
                                        line_index=fidx,
                                        action="replace",
                                        reason="stale-read (file later edited)",
                                        original_bytes=fsize,
                                        pruned_bytes=new_size,
                                        replacement=new_msg,
                                    ))
                                    total_pruned += saved

    replaced = len(actions)
    return StrategyResult(
        strategy_name="stale-reads",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Trimmed {replaced} stale file read results",
    )


@strategy("system-reminder-dedup", "Deduplicate repeated <system-reminder> tags", "standard", "0.1-3%")
def strategy_system_reminder_dedup(messages: list[Message], config: dict) -> StrategyResult:
    """Remove duplicate system-reminder content, keeping only the first occurrence."""
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    replaced = 0

    reminder_pattern = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
    seen_hashes: set[str] = set()

    for pos, (idx, msg, size) in enumerate(messages):
        blocks = get_content_blocks(msg)
        if not blocks:
            continue

        new_blocks = []
        changed = False
        for block in blocks:
            if block.get("type") in ("text", "tool_result"):
                text = block.get("text", "") or (block.get("content", "") if isinstance(block.get("content"), str) else "")
                if not text:
                    new_blocks.append(block)
                    continue

                reminders = reminder_pattern.findall(text)
                if reminders:
                    new_text = text
                    for reminder in reminders:
                        h = hashlib.md5(reminder.encode()).hexdigest()
                        if h in seen_hashes:
                            new_text = new_text.replace(reminder, "")
                            changed = True
                        else:
                            seen_hashes.add(h)

                    if changed:
                        new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip()
                        if block.get("type") == "text":
                            new_blocks.append({**block, "text": new_text})
                        elif block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                            new_blocks.append({**block, "content": new_text})
                        else:
                            new_blocks.append(block)
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)
            else:
                new_blocks.append(block)

        if changed:
            new_msg = set_content_blocks(msg, new_blocks)
            new_size = msg_bytes(new_msg)
            saved = size - new_size
            if saved > 0:
                actions.append(PruneAction(
                    line_index=idx,
                    action="replace",
                    reason="system-reminder-dedup",
                    original_bytes=size,
                    pruned_bytes=new_size,
                    replacement=new_msg,
                ))
                total_pruned += saved
                replaced += 1

    return StrategyResult(
        strategy_name="system-reminder-dedup",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Deduped system-reminders in {replaced} messages ({len(seen_hashes)} unique)",
    )
