"""Aggressive-tier strategies: maximum savings, more invasive."""

from __future__ import annotations

import copy
import hashlib
import json

from ..helpers import (
    content_block_bytes,
    get_content_blocks,
    get_msg_type,
    msg_bytes,
    set_content_blocks,
    text_of,
)
from ..registry import strategy
from ..types import Message, PruneAction, StrategyResult


@strategy("http-spam", "Collapse consecutive HTTP request/response messages", "aggressive", "0-2%")
def strategy_http_spam(messages: list[Message], config: dict) -> StrategyResult:
    """Collapse runs of HTTP-related tool calls (WebFetch, WebSearch) that repeat."""
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0

    http_tools = {"WebFetch", "WebSearch", "webfetch", "websearch"}

    i = 0
    while i < len(messages):
        idx, msg, size = messages[i]
        blocks = get_content_blocks(msg)
        is_http = any(
            b.get("type") == "tool_use" and b.get("name") in http_tools
            for b in blocks
        )
        if is_http:
            run_start = i
            run_end = i + 1
            while run_end < len(messages):
                _, nmsg, _ = messages[run_end]
                nblocks = get_content_blocks(nmsg)
                n_is_http = any(
                    b.get("type") in ("tool_use", "tool_result")
                    and (
                        b.get("name") in http_tools
                        or any(
                            b2.get("type") == "tool_use" and b2.get("name") in http_tools
                            for b2 in get_content_blocks(messages[max(0, run_end - 1)][1])
                        )
                    )
                    for b in nblocks
                )
                if n_is_http or get_msg_type(nmsg) == "progress":
                    run_end += 1
                else:
                    break

            if run_end - run_start > 3:
                for j in range(run_start + 1, run_end - 1):
                    rm_idx, rm_msg, rm_size = messages[j]
                    if get_msg_type(rm_msg) == "progress":
                        actions.append(PruneAction(
                            line_index=rm_idx,
                            action="remove",
                            reason="http-spam progress tick",
                            original_bytes=rm_size,
                            pruned_bytes=0,
                        ))
                        total_pruned += rm_size
            i = run_end
        else:
            i += 1

    removed = len(actions)
    return StrategyResult(
        strategy_name="http-spam",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=removed,
        messages_removed=removed,
        messages_replaced=0,
        summary=f"Collapsed {removed} HTTP spam messages",
    )


@strategy("error-retry-collapse", "Collapse repeated tool_use->error->retry sequences", "aggressive", "0-5%")
def strategy_error_retry_collapse(messages: list[Message], config: dict) -> StrategyResult:
    """When a tool fails and is retried identically, collapse intermediate attempts."""
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0

    tool_sequence: list[tuple[int, str, str, bool]] = []

    for pos, (idx, msg, size) in enumerate(messages):
        for block in get_content_blocks(msg):
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = json.dumps(block.get("input", {}), sort_keys=True)
                h = hashlib.md5(inp.encode()).hexdigest()
                tool_sequence.append((pos, name, h, False))
            elif block.get("type") == "tool_result" and block.get("is_error"):
                tool_sequence.append((pos, "_error", "", True))

    i = 0
    while i < len(tool_sequence) - 2:
        pos_a, name_a, hash_a, err_a = tool_sequence[i]
        if not err_a and name_a != "_error":
            retries = []
            j = i + 1
            while j < len(tool_sequence) - 1:
                _, _, _, is_err = tool_sequence[j]
                if not is_err:
                    break
                if j + 1 < len(tool_sequence):
                    _, name_retry, hash_retry, _ = tool_sequence[j + 1]
                    if name_retry == name_a and hash_retry == hash_a:
                        retries.append((tool_sequence[j][0], tool_sequence[j + 1][0]))
                        j += 2
                        continue
                break

            if retries:
                for err_pos, retry_pos in retries[:-1]:
                    for rm_pos in (err_pos, retry_pos):
                        rm_idx, _, rm_size = messages[rm_pos]
                        actions.append(PruneAction(
                            line_index=rm_idx,
                            action="remove",
                            reason="error-retry-collapse (intermediate retry)",
                            original_bytes=rm_size,
                            pruned_bytes=0,
                        ))
                        total_pruned += rm_size
            i = j if retries else i + 1
        else:
            i += 1

    removed = len(actions)
    return StrategyResult(
        strategy_name="error-retry-collapse",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=removed,
        messages_removed=removed,
        messages_replaced=0,
        summary=f"Collapsed {removed} error retry messages",
    )


@strategy("background-poll-collapse", "Collapse repeated TaskOutput/queue-operation checks", "aggressive", "0-1%")
def strategy_background_poll_collapse(messages: list[Message], config: dict) -> StrategyResult:
    """Collapse repeated polling messages (TaskOutput, queue-operation)."""
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0

    i = 0
    while i < len(messages):
        idx, msg, size = messages[i]
        mtype = get_msg_type(msg)

        if mtype == "queue-operation" and msg.get("operation") in ("check", "poll"):
            run_start = i
            run_end = i + 1
            while run_end < len(messages):
                _, nmsg, _ = messages[run_end]
                if get_msg_type(nmsg) == "queue-operation" and nmsg.get("operation") in ("check", "poll"):
                    run_end += 1
                else:
                    break
            if run_end - run_start > 1:
                for j in range(run_start, run_end - 1):
                    rm_idx, _, rm_size = messages[j]
                    actions.append(PruneAction(
                        line_index=rm_idx,
                        action="remove",
                        reason="background-poll-collapse",
                        original_bytes=rm_size,
                        pruned_bytes=0,
                    ))
                    total_pruned += rm_size
            i = run_end
        else:
            blocks = get_content_blocks(msg)
            is_poll = any(
                b.get("type") == "tool_use"
                and b.get("name") in ("TaskOutput", "taskoutput")
                and b.get("input", {}).get("block") is False
                for b in blocks
            )
            if is_poll:
                run_start = i
                run_end = i + 1
                while run_end < len(messages):
                    _, nmsg, _ = messages[run_end]
                    nblocks = get_content_blocks(nmsg)
                    n_is_poll = any(
                        b.get("type") == "tool_use"
                        and b.get("name") in ("TaskOutput", "taskoutput")
                        and b.get("input", {}).get("block") is False
                        for b in nblocks
                    )
                    if n_is_poll or get_msg_type(nmsg) == "progress":
                        run_end += 1
                    else:
                        break
                if run_end - run_start > 2:
                    for j in range(run_start + 1, run_end - 1):
                        rm_idx, _, rm_size = messages[j]
                        actions.append(PruneAction(
                            line_index=rm_idx,
                            action="remove",
                            reason="background-poll-collapse",
                            original_bytes=rm_size,
                            pruned_bytes=0,
                        ))
                        total_pruned += rm_size
                i = run_end
            else:
                i += 1

    removed = len(actions)
    return StrategyResult(
        strategy_name="background-poll-collapse",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=removed,
        messages_removed=removed,
        messages_replaced=0,
        summary=f"Collapsed {removed} background poll messages",
    )


@strategy("document-dedup", "Deduplicate large document blocks (CLAUDE.md injection)", "aggressive", "0-44%")
def strategy_document_dedup(messages: list[Message], config: dict) -> StrategyResult:
    """Detect and deduplicate large text blocks that appear multiple times."""
    min_block_size = config.get("document_dedup_min_bytes", 1024)
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    affected: set[int] = set()

    block_hashes: dict[str, list[tuple[int, int, int]]] = {}

    for pos, (idx, msg, size) in enumerate(messages):
        blocks = get_content_blocks(msg)
        for bi, block in enumerate(blocks):
            text = text_of(block)
            if len(text.encode("utf-8")) >= min_block_size:
                h = hashlib.md5(text.encode()).hexdigest()
                block_hashes.setdefault(h, []).append((pos, bi, len(text.encode("utf-8"))))

    for h, occurrences in block_hashes.items():
        if len(occurrences) <= 1:
            continue

        for occ_pos, occ_bi, occ_bytes in occurrences[1:]:
            idx, msg, size = messages[occ_pos]
            blocks = get_content_blocks(msg)
            if occ_bi >= len(blocks):
                continue

            block = blocks[occ_bi]
            btype = block.get("type", "text")
            new_blocks = list(blocks)

            if btype == "text":
                preview = text_of(block)[:80].replace("\n", " ")
                new_blocks[occ_bi] = {"type": "text", "text": f"[duplicate content removed by cozempic - first seen earlier: {preview}...]"}
            elif btype == "tool_result":
                content = block.get("content", "")
                if isinstance(content, str):
                    preview = content[:80].replace("\n", " ")
                    new_blocks[occ_bi] = {**block, "content": f"[duplicate content removed by cozempic: {preview}...]"}

            new_msg = set_content_blocks(msg, new_blocks)
            new_size = msg_bytes(new_msg)
            saved = size - new_size
            if saved > 0 and idx not in affected:
                actions.append(PruneAction(
                    line_index=idx,
                    action="replace",
                    reason=f"document-dedup ({occ_bytes} bytes, hash={h[:8]})",
                    original_bytes=size,
                    pruned_bytes=new_size,
                    replacement=new_msg,
                ))
                total_pruned += saved
                affected.add(idx)

    replaced = len(actions)
    return StrategyResult(
        strategy_name="document-dedup",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Deduped {replaced} large document blocks",
    )


@strategy("mega-block-trim", "Trim any content block over 32KB", "aggressive", "safety net")
def strategy_mega_block_trim(messages: list[Message], config: dict) -> StrategyResult:
    """Safety net: any single content block over 32KB gets truncated."""
    max_block_bytes = config.get("mega_block_max_bytes", 32768)
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    replaced = 0

    for pos, (idx, msg, size) in enumerate(messages):
        if get_msg_type(msg) in ("summary", "queue-operation"):
            continue

        blocks = get_content_blocks(msg)
        if not blocks:
            continue

        new_blocks = []
        changed = False
        for block in blocks:
            block_size = content_block_bytes(block)
            if block_size > max_block_bytes:
                text = text_of(block)
                btype = block.get("type", "text")
                if text and len(text.encode("utf-8")) > max_block_bytes:
                    half = max_block_bytes // 2
                    trimmed = (
                        text[:half]
                        + f"\n\n... [{len(text.encode('utf-8')) - max_block_bytes} bytes trimmed by cozempic] ...\n\n"
                        + text[-half:]
                    )
                    if btype == "thinking":
                        new_blocks.append({**block, "thinking": trimmed})
                    elif btype == "text":
                        new_blocks.append({**block, "text": trimmed})
                    elif btype == "tool_result" and isinstance(block.get("content"), str):
                        new_blocks.append({**block, "content": trimmed})
                    else:
                        new_blocks.append(block)
                    changed = True
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
                    reason="mega-block-trim",
                    original_bytes=size,
                    pruned_bytes=new_size,
                    replacement=new_msg,
                ))
                total_pruned += saved
                replaced += 1

    return StrategyResult(
        strategy_name="mega-block-trim",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Trimmed {replaced} mega blocks (>{max_block_bytes // 1024}KB)",
    )


@strategy("envelope-strip", "Strip redundant top-level fields (cwd, version, slug)", "aggressive", "2-4%")
def strategy_envelope_strip(messages: list[Message], config: dict) -> StrategyResult:
    """Remove repetitive envelope fields that are constant across all messages."""
    strip_candidates = {"cwd", "version", "gitBranch", "slug", "userType", "isSidechain"}

    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    replaced = 0

    field_values: dict[str, set[str]] = {f: set() for f in strip_candidates}
    for _, (_, msg, _) in enumerate(messages):
        for f in strip_candidates:
            if f in msg:
                field_values[f].add(str(msg[f]))

    constant_fields = {f for f, vals in field_values.items() if len(vals) == 1}

    if not constant_fields:
        return StrategyResult(
            strategy_name="envelope-strip",
            actions=[],
            original_bytes=total_orig,
            pruned_bytes=0,
            messages_affected=0,
            messages_removed=0,
            messages_replaced=0,
            summary="No constant envelope fields found",
        )

    for pos, (idx, msg, size) in enumerate(messages):
        if pos == 0:
            continue

        new_msg = copy.deepcopy(msg)
        changed = False
        for f in constant_fields:
            if f in new_msg:
                del new_msg[f]
                changed = True

        if changed:
            new_size = msg_bytes(new_msg)
            saved = size - new_size
            if saved > 0:
                actions.append(PruneAction(
                    line_index=idx,
                    action="replace",
                    reason=f"envelope-strip ({', '.join(sorted(constant_fields))})",
                    original_bytes=size,
                    pruned_bytes=new_size,
                    replacement=new_msg,
                ))
                total_pruned += saved
                replaced += 1

    return StrategyResult(
        strategy_name="envelope-strip",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Stripped {', '.join(sorted(constant_fields))} from {replaced} messages",
    )
