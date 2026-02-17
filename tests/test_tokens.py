"""Tests for token estimation module."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cozempic.helpers import msg_bytes
from cozempic.tokens import (
    DEFAULT_CONTEXT_WINDOW,
    SYSTEM_OVERHEAD_TOKENS,
    TokenEstimate,
    _is_context_message,
    calibrate_ratio,
    estimate_session_tokens,
    estimate_tokens_heuristic,
    extract_usage_tokens,
    quick_token_estimate,
)


def make_message(line_idx: int, msg: dict) -> tuple[int, dict, int]:
    return (line_idx, msg, msg_bytes(msg))


def make_assistant_with_usage(
    line_idx: int,
    text: str = "hello",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_creation: int = 500,
    cache_read: int = 300,
    sidechain: bool = False,
) -> tuple[int, dict, int]:
    msg = {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
            "stop_reason": "end_turn",
        },
        "costUSD": 0.01,
        "duration": 1234,
    }
    return make_message(line_idx, msg)


def make_assistant_no_usage(line_idx: int, text: str = "hello") -> tuple[int, dict, int]:
    msg = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
    }
    return make_message(line_idx, msg)


def make_user(line_idx: int, text: str = "hi") -> tuple[int, dict, int]:
    msg = {
        "type": "user",
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }
    return make_message(line_idx, msg)


def make_progress(line_idx: int) -> tuple[int, dict, int]:
    msg = {
        "type": "progress",
        "data": {"type": "hook_progress"},
    }
    return make_message(line_idx, msg)


def make_file_history(line_idx: int) -> tuple[int, dict, int]:
    msg = {
        "type": "file-history-snapshot",
        "files": [{"path": "/foo/bar.py", "content": "x" * 1000}],
    }
    return make_message(line_idx, msg)


def make_sidechain_assistant(line_idx: int, text: str = "sub-task") -> tuple[int, dict, int]:
    msg = {
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    return make_message(line_idx, msg)


def make_thinking_only(line_idx: int) -> tuple[int, dict, int]:
    msg = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm let me think", "signature": "sig123"},
            ],
        },
    }
    return make_message(line_idx, msg)


class TestExtractUsageTokens(unittest.TestCase):

    def test_extracts_from_last_assistant(self):
        messages = [
            make_user(0, "hello"),
            make_assistant_with_usage(1, "first", input_tokens=500, cache_creation=100, cache_read=50),
            make_user(2, "more"),
            make_assistant_with_usage(3, "second", input_tokens=1000, cache_creation=200, cache_read=300),
        ]
        result = extract_usage_tokens(messages)
        self.assertIsNotNone(result)
        self.assertEqual(result["input_tokens"], 1000)
        self.assertEqual(result["cache_creation_input_tokens"], 200)
        self.assertEqual(result["cache_read_input_tokens"], 300)
        self.assertEqual(result["total"], 1500)  # 1000 + 200 + 300

    def test_skips_sidechain(self):
        messages = [
            make_user(0, "hello"),
            make_assistant_with_usage(1, "main", input_tokens=800, cache_creation=0, cache_read=0),
            make_sidechain_assistant(2, "sub-task"),
        ]
        result = extract_usage_tokens(messages)
        self.assertIsNotNone(result)
        self.assertEqual(result["input_tokens"], 800)
        self.assertEqual(result["total"], 800)

    def test_skips_parse_errors(self):
        messages = [
            make_user(0, "hello"),
            make_assistant_with_usage(1, "good", input_tokens=600, cache_creation=0, cache_read=0),
            (2, {"_parse_error": True, "_raw": "bad json", "type": "assistant"}, 8),
        ]
        result = extract_usage_tokens(messages)
        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 600)

    def test_returns_none_when_no_usage(self):
        messages = [
            make_user(0, "hello"),
            make_assistant_no_usage(1, "response"),
        ]
        result = extract_usage_tokens(messages)
        self.assertIsNone(result)

    def test_returns_none_for_empty_messages(self):
        result = extract_usage_tokens([])
        self.assertIsNone(result)


class TestIsContextMessage(unittest.TestCase):

    def test_user_message_is_context(self):
        _, msg, _ = make_user(0, "hello")
        self.assertTrue(_is_context_message(msg))

    def test_assistant_message_is_context(self):
        _, msg, _ = make_assistant_with_usage(0, "response")
        self.assertTrue(_is_context_message(msg))

    def test_progress_is_not_context(self):
        _, msg, _ = make_progress(0)
        self.assertFalse(_is_context_message(msg))

    def test_file_history_is_not_context(self):
        _, msg, _ = make_file_history(0)
        self.assertFalse(_is_context_message(msg))

    def test_sidechain_is_not_context(self):
        _, msg, _ = make_sidechain_assistant(0)
        self.assertFalse(_is_context_message(msg))

    def test_thinking_only_is_not_context(self):
        _, msg, _ = make_thinking_only(0)
        self.assertFalse(_is_context_message(msg))


class TestHeuristicEstimation(unittest.TestCase):

    def test_empty_session(self):
        total, breakdown = estimate_tokens_heuristic([])
        self.assertEqual(total, SYSTEM_OVERHEAD_TOKENS)
        self.assertEqual(breakdown, {})

    def test_basic_estimation(self):
        messages = [
            make_user(0, "a" * 370),  # ~100 tokens at 3.7 chars/token
            make_assistant_no_usage(1, "b" * 370),
        ]
        total, breakdown = estimate_tokens_heuristic(messages)
        # Should be roughly 200 content tokens + overhead
        self.assertGreater(total, SYSTEM_OVERHEAD_TOKENS)
        self.assertIn("user", breakdown)
        self.assertIn("assistant", breakdown)

    def test_thinking_blocks_excluded(self):
        """Thinking content should not count toward token estimate."""
        msg = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "x" * 10000},
                    {"type": "text", "text": "short answer"},
                ],
            },
        }
        messages = [make_message(0, msg)]
        total, _ = estimate_tokens_heuristic(messages)
        # Should be much less than 10000/3.7 + overhead
        self.assertLess(total, SYSTEM_OVERHEAD_TOKENS + 100)

    def test_skips_progress_and_file_history(self):
        messages = [
            make_user(0, "hello"),
            make_progress(1),
            make_progress(2),
            make_file_history(3),
            make_assistant_no_usage(4, "response"),
        ]
        total, breakdown = estimate_tokens_heuristic(messages)
        self.assertNotIn("progress", breakdown)
        self.assertNotIn("file-history-snapshot", breakdown)


class TestEstimateSessionTokens(unittest.TestCase):

    def test_exact_preferred_over_heuristic(self):
        messages = [
            make_user(0, "hello"),
            make_assistant_with_usage(1, "resp", input_tokens=50000, cache_creation=10000, cache_read=5000),
        ]
        te = estimate_session_tokens(messages)
        self.assertEqual(te.method, "exact")
        self.assertEqual(te.confidence, "high")
        self.assertEqual(te.total, 65000)  # 50000 + 10000 + 5000
        expected_pct = round(65000 / DEFAULT_CONTEXT_WINDOW * 100, 1)
        self.assertEqual(te.context_pct, expected_pct)

    def test_heuristic_fallback(self):
        messages = [
            make_user(0, "hello"),
            make_assistant_no_usage(1, "response without usage"),
        ]
        te = estimate_session_tokens(messages)
        self.assertEqual(te.method, "heuristic")
        self.assertEqual(te.confidence, "medium")
        self.assertGreater(te.total, 0)

    def test_context_pct_calculation(self):
        messages = [
            make_user(0, "hello"),
            make_assistant_with_usage(1, "resp", input_tokens=100000, cache_creation=0, cache_read=0),
        ]
        te = estimate_session_tokens(messages)
        self.assertEqual(te.context_pct, 50.0)  # 100K / 200K = 50%


class TestQuickTokenEstimate(unittest.TestCase):

    def _write_jsonl(self, messages: list[dict]) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
        for msg in messages:
            tmp.write(json.dumps(msg) + "\n")
        tmp.close()
        return Path(tmp.name)

    def test_reads_usage_from_tail(self):
        messages = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {
                        "input_tokens": 5000,
                        "output_tokens": 200,
                        "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": 500,
                    },
                },
            },
        ]
        path = self._write_jsonl(messages)
        try:
            result = quick_token_estimate(path)
            self.assertEqual(result, 6500)  # 5000 + 1000 + 500
        finally:
            path.unlink()

    def test_returns_none_without_usage(self):
        messages = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
        ]
        path = self._write_jsonl(messages)
        try:
            result = quick_token_estimate(path)
            self.assertIsNone(result)
        finally:
            path.unlink()

    def test_skips_sidechain_in_tail(self):
        messages = [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "main"}],
                    "usage": {
                        "input_tokens": 8000,
                        "output_tokens": 300,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "sub"}],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
        ]
        path = self._write_jsonl(messages)
        try:
            result = quick_token_estimate(path)
            self.assertEqual(result, 8000)
        finally:
            path.unlink()

    def test_handles_missing_file(self):
        result = quick_token_estimate(Path("/nonexistent/file.jsonl"))
        self.assertIsNone(result)


class TestCalibrateRatio(unittest.TestCase):

    def test_returns_ratio_with_usage(self):
        text = "a" * 3700  # ~1000 tokens at 3.7 default
        messages = [
            make_user(0, text),
            make_assistant_with_usage(
                1, text,
                input_tokens=40000,
                cache_creation=0,
                cache_read=0,
            ),
        ]
        ratio = calibrate_ratio(messages)
        self.assertIsNotNone(ratio)
        self.assertGreater(ratio, 0)

    def test_returns_none_without_usage(self):
        messages = [
            make_user(0, "hello"),
            make_assistant_no_usage(1, "response"),
        ]
        ratio = calibrate_ratio(messages)
        self.assertIsNone(ratio)


if __name__ == "__main__":
    unittest.main()
