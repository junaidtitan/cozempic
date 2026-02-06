"""Tests for Cozempic strategies."""

from __future__ import annotations

import json
import unittest

from cozempic.helpers import msg_bytes
from cozempic.registry import STRATEGIES

# Ensure strategies are registered
import cozempic.strategies  # noqa: F401


def make_message(line_idx: int, msg: dict) -> tuple[int, dict, int]:
    return (line_idx, msg, msg_bytes(msg))


def make_progress(line_idx: int) -> tuple[int, dict, int]:
    msg = {
        "type": "progress",
        "data": {"type": "hook_progress"},
        "timestamp": "2026-01-01T00:00:00Z",
        "uuid": f"uuid-{line_idx}",
    }
    return make_message(line_idx, msg)


def make_assistant(line_idx: int, text: str = "hello", thinking: str | None = None) -> tuple[int, dict, int]:
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking, "signature": "sig" * 50})
    content.append({"type": "text", "text": text})
    msg = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": content,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "stop_reason": "end_turn",
        },
        "costUSD": 0.01,
        "duration": 1234,
    }
    return make_message(line_idx, msg)


def make_user(line_idx: int, text: str = "hi") -> tuple[int, dict, int]:
    msg = {
        "type": "user",
        "cwd": "/home/user/project",
        "version": "2.1.25",
        "slug": "test-slug",
        "gitBranch": "main",
        "userType": "external",
        "isSidechain": False,
        "message": {"role": "user", "content": text},
        "uuid": f"uuid-{line_idx}",
    }
    return make_message(line_idx, msg)


def make_file_history(line_idx: int, mid: str = "mid-1") -> tuple[int, dict, int]:
    msg = {
        "type": "file-history-snapshot",
        "messageId": mid,
        "snapshot": {"messageId": mid, "trackedFileBackups": {}, "timestamp": "2026-01-01T00:00:00Z"},
        "isSnapshotUpdate": False,
    }
    return make_message(line_idx, msg)


class TestProgressCollapse(unittest.TestCase):
    def test_collapses_consecutive_progress(self):
        messages = [
            make_progress(0),
            make_progress(1),
            make_progress(2),
            make_user(3),
            make_progress(4),
        ]
        sr = STRATEGIES["progress-collapse"].func(messages, {})
        self.assertEqual(sr.messages_removed, 2)
        self.assertEqual(sr.messages_replaced, 0)
        # Lines 0 and 1 should be removed, line 2 kept
        removed_lines = {a.line_index for a in sr.actions}
        self.assertEqual(removed_lines, {0, 1})

    def test_single_progress_not_collapsed(self):
        messages = [make_user(0), make_progress(1), make_user(2)]
        sr = STRATEGIES["progress-collapse"].func(messages, {})
        self.assertEqual(len(sr.actions), 0)


class TestFileHistoryDedup(unittest.TestCase):
    def test_dedup_same_message_id(self):
        messages = [
            make_file_history(0, "mid-1"),
            make_file_history(1, "mid-1"),
            make_file_history(2, "mid-1"),
        ]
        sr = STRATEGIES["file-history-dedup"].func(messages, {})
        self.assertEqual(sr.messages_removed, 2)
        # Should keep last (line 2), remove lines 0 and 1
        removed_lines = {a.line_index for a in sr.actions}
        self.assertEqual(removed_lines, {0, 1})

    def test_different_ids_not_deduped(self):
        messages = [
            make_file_history(0, "mid-1"),
            make_file_history(1, "mid-2"),
        ]
        sr = STRATEGIES["file-history-dedup"].func(messages, {})
        self.assertEqual(len(sr.actions), 0)


class TestMetadataStrip(unittest.TestCase):
    def test_strips_usage_and_costs(self):
        messages = [make_assistant(0, text="hi", thinking="hmm")]
        sr = STRATEGIES["metadata-strip"].func(messages, {})
        self.assertEqual(sr.messages_replaced, 1)
        replacement = sr.actions[0].replacement
        self.assertNotIn("costUSD", replacement)
        self.assertNotIn("duration", replacement)
        self.assertNotIn("usage", replacement.get("message", {}))
        self.assertNotIn("stop_reason", replacement.get("message", {}))


class TestThinkingBlocks(unittest.TestCase):
    def test_remove_mode(self):
        messages = [make_assistant(0, text="hi", thinking="deep thoughts")]
        sr = STRATEGIES["thinking-blocks"].func(messages, {"thinking_mode": "remove"})
        self.assertEqual(sr.messages_replaced, 1)
        replacement = sr.actions[0].replacement
        content = replacement["message"]["content"]
        types = [b["type"] for b in content]
        self.assertNotIn("thinking", types)

    def test_truncate_mode(self):
        long_thinking = "x" * 500
        messages = [make_assistant(0, text="hi", thinking=long_thinking)]
        sr = STRATEGIES["thinking-blocks"].func(messages, {"thinking_mode": "truncate"})
        self.assertEqual(sr.messages_replaced, 1)
        replacement = sr.actions[0].replacement
        thinking_blocks = [b for b in replacement["message"]["content"] if b["type"] == "thinking"]
        self.assertEqual(len(thinking_blocks), 1)
        self.assertIn("truncated", thinking_blocks[0]["thinking"])
        self.assertNotIn("signature", thinking_blocks[0])


class TestEnvelopeStrip(unittest.TestCase):
    def test_strips_constant_fields(self):
        messages = [
            make_user(0, "hi"),
            make_user(1, "there"),
            make_user(2, "friend"),
        ]
        sr = STRATEGIES["envelope-strip"].func(messages, {})
        # Should strip from messages 1 and 2 (not 0)
        self.assertEqual(sr.messages_replaced, 2)
        for action in sr.actions:
            replacement = action.replacement
            self.assertNotIn("cwd", replacement)
            self.assertNotIn("version", replacement)
            self.assertNotIn("slug", replacement)


class TestToolOutputTrim(unittest.TestCase):
    def test_trims_large_tool_result(self):
        big_content = "line\n" * 200  # 200 lines
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": big_content},
                ],
            },
        }
        messages = [make_message(0, msg)]
        sr = STRATEGIES["tool-output-trim"].func(messages, {"tool_output_max_lines": 100})
        self.assertEqual(sr.messages_replaced, 1)
        saved = sr.actions[0].original_bytes - sr.actions[0].pruned_bytes
        self.assertGreater(saved, 0)


if __name__ == "__main__":
    unittest.main()
