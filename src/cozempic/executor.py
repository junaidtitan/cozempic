"""Action executor and prescription runner."""

from __future__ import annotations

from .helpers import msg_bytes
from .registry import STRATEGIES
from .types import Message, PruneAction, StrategyResult


def execute_actions(
    messages: list[Message],
    actions: list[PruneAction],
) -> list[Message]:
    """Apply PruneActions to messages and return the new message list."""
    removals: set[int] = set()
    replacements: dict[int, dict] = {}

    for action in actions:
        if action.action == "remove":
            removals.add(action.line_index)
        elif action.action == "replace" and action.replacement:
            replacements[action.line_index] = action.replacement

    result: list[Message] = []
    for idx, msg, size in messages:
        if idx in removals:
            continue
        if idx in replacements:
            new_msg = replacements[idx]
            new_size = msg_bytes(new_msg)
            result.append((idx, new_msg, new_size))
        else:
            result.append((idx, msg, size))

    return result


def run_prescription(
    messages: list[Message],
    strategy_names: list[str],
    config: dict,
) -> tuple[list[Message], list[StrategyResult]]:
    """Run strategies sequentially, each on the result of the previous.

    This ensures replacements compose correctly when multiple strategies
    modify the same message.
    """
    current = messages
    results: list[StrategyResult] = []
    for sname in strategy_names:
        if sname not in STRATEGIES:
            continue
        sr = STRATEGIES[sname].func(current, config)
        results.append(sr)
        if sr.actions:
            current = execute_actions(current, sr.actions)
    return current, results
