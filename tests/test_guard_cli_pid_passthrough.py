"""`cozempic guard` must hand the owner PID it was given to every daemon entry.

The owner-lifecycle fix (upstream #185) has two proven ends: the SessionStart
hook puts ``--claude-pid`` on the daemon argv, and ``start_guard`` exits when
that PID dies. The link between them is ``cmd_guard``, which forwards
``args.claude_pid`` into one of three entry points. None of those forwards was
covered: replacing ``claude_pid=claude_pid`` with ``claude_pid=None`` in the
self-reload branch survived the suite on 2026-10-21. A guard that loses its
owner on the way in is the #187 leak with extra steps.

These tests parse real argv through ``build_parser`` and intercept the three
entry points, asserting the keyword each one receives.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _run_guard(argv: list[str], found_pid):
    """Parse argv, run cmd_guard with all three daemon entries intercepted.

    Returns {entry_name: kwargs} for whichever entries were called.
    """
    from cozempic import cli, guard
    calls: dict[str, dict] = {}

    def rec(name, ret):
        def f(*a, **kw):
            calls[name] = kw
            return ret
        return f

    args = cli.build_parser().parse_args(["guard", *argv])
    with patch.object(cli, "start_guard", rec("start_guard", None)), \
         patch.object(cli, "start_guard_daemon", rec("start_guard_daemon", {"started": True, "pid": 1, "log_file": "x"})), \
         patch.object(guard, "reload_self_daemon", rec("reload_self_daemon", {"reloaded": False, "reason": "test"})), \
         patch.object(cli, "find_claude_pid", return_value=found_pid):
        cli.cmd_guard(args)
    return calls


class TestGuardForwardsOwnerPid(unittest.TestCase):
    def test_foreground_forwards_given_pid(self):
        calls = _run_guard(["--claude-pid", "4242"], found_pid=None)
        self.assertEqual(calls["start_guard"]["claude_pid"], 4242)

    def test_daemon_forwards_given_pid(self):
        calls = _run_guard(["--daemon", "--claude-pid", "4242"], found_pid=None)
        self.assertEqual(calls["start_guard_daemon"]["claude_pid"], 4242)

    def test_reload_self_forwards_given_pid(self):
        calls = _run_guard(["--reload-self", "--claude-pid", "4242"], found_pid=None)
        self.assertEqual(calls["reload_self_daemon"]["claude_pid"], 4242)

    def test_given_pid_beats_ancestry_walk(self):
        # The hook passes the identity-verified PID precisely because the walk
        # is unreliable once the spawn is backgrounded; the explicit value wins.
        calls = _run_guard(["--claude-pid", "4242"], found_pid=9999)
        self.assertEqual(calls["start_guard"]["claude_pid"], 4242)

    def test_without_flag_falls_back_to_ancestry_walk(self):
        calls = _run_guard([], found_pid=9999)
        self.assertEqual(calls["start_guard"]["claude_pid"], 9999)

    def test_without_flag_and_no_ancestor_forwards_none(self):
        # Honest None, not a made-up PID: the daemon then runs unowned and the
        # watchdog stays disarmed, which is the documented pre-#185 behaviour.
        calls = _run_guard(["--daemon"], found_pid=None)
        self.assertIsNone(calls["start_guard_daemon"]["claude_pid"])


if __name__ == "__main__":
    unittest.main()
