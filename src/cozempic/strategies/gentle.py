"""Gentle-tier strategies: safe, minimal pruning."""

from __future__ import annotations

import copy

from ..helpers import get_msg_type, msg_bytes
from ..registry import strategy
from ..types import Message, PruneAction, StrategyResult


@strategy("progress-collapse", "Collapse consecutive progress tick messages", "gentle", "40-48%")
def strategy_progress_collapse(messages: list[Message], config: dict) -> StrategyResult:
    """Consecutive progress messages (hook_progress, bash_progress, etc.) get collapsed into one."""
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0

    i = 0
    while i < len(messages):
        idx, msg, size = messages[i]
        if get_msg_type(msg) == "progress":
            run_start = i
            run_end = i + 1
            while run_end < len(messages) and get_msg_type(messages[run_end][1]) == "progress":
                run_end += 1

            run_length = run_end - run_start
            if run_length > 1:
                for j in range(run_start, run_end - 1):
                    rm_idx, _, rm_size = messages[j]
                    actions.append(PruneAction(
                        line_index=rm_idx,
                        action="remove",
                        reason=f"progress tick {j - run_start + 1}/{run_length}",
                        original_bytes=rm_size,
                        pruned_bytes=0,
                    ))
                    total_pruned += rm_size
            i = run_end
        else:
            i += 1

    removed = len(actions)
    return StrategyResult(
        strategy_name="progress-collapse",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=removed,
        messages_removed=removed,
        messages_replaced=0,
        summary=f"Collapsed {removed} progress ticks",
    )


@strategy("file-history-dedup", "Deduplicate file-history-snapshot messages", "gentle", "3-6%")
def strategy_file_history_dedup(messages: list[Message], config: dict) -> StrategyResult:
    """Remove duplicate file-history-snapshot messages, keeping only the latest per messageId."""
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0

    snapshots: dict[str, list[int]] = {}
    for pos, (idx, msg, size) in enumerate(messages):
        if get_msg_type(msg) == "file-history-snapshot":
            mid = msg.get("messageId", "")
            if mid:
                snapshots.setdefault(mid, []).append(pos)

    for mid, positions in snapshots.items():
        if len(positions) > 1:
            for pos in positions[:-1]:
                idx, _, size = messages[pos]
                actions.append(PruneAction(
                    line_index=idx,
                    action="remove",
                    reason=f"duplicate file-history-snapshot (messageId={mid[:8]}...)",
                    original_bytes=size,
                    pruned_bytes=0,
                ))
                total_pruned += size

    # Collapse consecutive isSnapshotUpdate=true runs
    current_run: list[int] = []
    update_runs: list[list[int]] = []
    for pos, (idx, msg, size) in enumerate(messages):
        if get_msg_type(msg) == "file-history-snapshot" and msg.get("isSnapshotUpdate"):
            current_run.append(pos)
        else:
            if len(current_run) > 1:
                update_runs.append(current_run)
            current_run = []
    if len(current_run) > 1:
        update_runs.append(current_run)

    already_removed = {a.line_index for a in actions}
    for run in update_runs:
        for pos in run[:-1]:
            idx, _, size = messages[pos]
            if idx not in already_removed:
                actions.append(PruneAction(
                    line_index=idx,
                    action="remove",
                    reason="consecutive snapshot update",
                    original_bytes=size,
                    pruned_bytes=0,
                ))
                total_pruned += size

    removed = len(actions)
    return StrategyResult(
        strategy_name="file-history-dedup",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=removed,
        messages_removed=removed,
        messages_replaced=0,
        summary=f"Removed {removed} duplicate file-history snapshots",
    )


@strategy("metadata-strip", "Strip token usage stats, signatures, stop_reason", "gentle", "1-3%")
def strategy_metadata_strip(messages: list[Message], config: dict) -> StrategyResult:
    """Remove metadata fields: usage, stop_reason, stop_sequence, costUSD, duration."""
    strip_inner = {"usage", "stop_reason", "stop_sequence"}
    strip_outer = {"costUSD", "duration", "apiDuration"}

    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    replaced = 0

    for pos, (idx, msg, size) in enumerate(messages):
        new_msg = copy.deepcopy(msg)
        changed = False

        inner = new_msg.get("message", {})
        for f in strip_inner:
            if f in inner:
                del inner[f]
                changed = True

        for f in strip_outer:
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
                    reason="metadata-strip",
                    original_bytes=size,
                    pruned_bytes=new_size,
                    replacement=new_msg,
                ))
                total_pruned += saved
                replaced += 1

    return StrategyResult(
        strategy_name="metadata-strip",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Stripped metadata from {replaced} messages",
    )
