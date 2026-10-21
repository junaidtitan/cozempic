"""The tmux pane must own the PID before any keystroke is sent into it.

Incident, 2026-10-21: a full run of this test suite from a Bash tool inside a
live Claude Code session (running in tmux) typed "/exit" into that session's
own pane four times in forty minutes. TMUX_PANE is inherited by every process
the pane's shell starts, so _terminate_and_resume, called with a FIXTURE PID
and a patched identity check, resolved the pane to the live session and sent
it "/exit" followed by "claude --resume". Each was a clean self-termination
that every watcher reported as "claude exited".

Two defences, both tested here:

  1. guard._tmux_pane_owns_pid(pane, pid): the pane's #{pane_pid} must be
     `pid` or one of its ancestors. Fails closed on no pane, bad output, or
     any subprocess error.
  2. The tmux branch of _terminate_and_resume and the tmux branch of
     `cozempic treat` consult it BEFORE the sentinel write and BEFORE
     send-keys, so a refusal leaks nothing and types nothing.

These tests never touch a real tmux: every subprocess call is intercepted,
and the intercept RECORDS any send-keys so the assertion is "no keystroke was
attempted", not "no harm was observed".
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _fake_run_factory(pane_pid: int | None, ppid_chain: dict[int, int], sent: list):
    """Build a subprocess.run double.

    - `tmux display-message ... #{pane_pid}` → pane_pid (or rc=1 when None)
    - `ps -o ppid= -p N`                      → ppid_chain[N]
    - `tmux send-keys ...`                     → recorded in `sent`, never run
    Anything else is also recorded; nothing executes.
    """
    def fake_run(args, *a, **kw):
        cmd = list(args)
        if cmd[:2] == ["tmux", "display-message"]:
            if pane_pid is None:
                return subprocess.CompletedProcess(cmd, 1, "", "no such pane")
            return subprocess.CompletedProcess(cmd, 0, f"{pane_pid}\n", "")
        if cmd[:1] == ["ps"]:
            pid = int(cmd[-1])
            if pid not in ppid_chain:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return subprocess.CompletedProcess(cmd, 0, f"{ppid_chain[pid]}\n", "")
        if cmd[:2] == ["tmux", "send-keys"]:
            sent.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        sent.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return fake_run


class TestPaneOwnsPid(unittest.TestCase):
    def _owns(self, pane, pid, pane_pid, chain):
        from cozempic import guard
        sent: list = []
        with patch.object(guard.subprocess, "run", side_effect=_fake_run_factory(pane_pid, chain, sent)):
            return guard._tmux_pane_owns_pid(pane, pid)

    def test_no_pane_is_refused(self):
        self.assertFalse(self._owns("", 4242, 100, {4242: 100}))

    def test_pid_is_the_pane_process(self):
        self.assertTrue(self._owns("%7", 100, 100, {}))

    def test_pid_is_a_descendant_of_the_pane_shell(self):
        # pane shell 100 → claude 4242
        self.assertTrue(self._owns("%7", 4242, 100, {4242: 100}))

    def test_pid_two_levels_down(self):
        # pane shell 100 → wrapper 200 → claude 4242
        self.assertTrue(self._owns("%7", 4242, 100, {4242: 200, 200: 100}))

    def test_foreign_pid_is_refused(self):
        # the incident shape: fixture PID 89113 is nobody's child; pane runs 100
        self.assertFalse(self._owns("%7", 89113, 100, {89113: 1}))

    def test_pane_lookup_failure_is_refused(self):
        self.assertFalse(self._owns("%7", 4242, None, {4242: 100}))

    def test_unparseable_pane_pid_is_refused(self):
        from cozempic import guard
        def bad_run(args, *a, **kw):
            return subprocess.CompletedProcess(list(args), 0, "not-a-pid\n", "")
        with patch.object(guard.subprocess, "run", side_effect=bad_run):
            self.assertFalse(guard._tmux_pane_owns_pid("%7", 4242))

    def test_subprocess_error_is_refused(self):
        from cozempic import guard
        with patch.object(guard.subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 3)):
            self.assertFalse(guard._tmux_pane_owns_pid("%7", 4242))

    def test_init_pid_is_refused(self):
        # a pane whose pid reads as 1 or 0 is not a real shell; never trust it
        self.assertFalse(self._owns("%7", 4242, 1, {4242: 1}))


class TestTerminateAndResumeRefusesForeignPane(unittest.TestCase):
    """The incident, replayed against the double: tmux env, fixture PID,
    identity check patched True (as the suite's own tests do). Nothing may be
    typed and no sentinel may be left behind."""

    def setUp(self):
        self.sid = "0123456789abcdef0123456789abcdef"
        self.sentinel = Path(f"/tmp/cozempic_reload_{self.sid[:12]}.in-flight")
        self.sentinel.unlink(missing_ok=True)
        self.addCleanup(self.sentinel.unlink, missing_ok=True)

    def _run(self, pid, pane_pid, chain):
        from cozempic import guard
        sent: list = []
        env = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%7"}
        with patch.dict(os.environ, env), \
             patch.object(guard.subprocess, "run", side_effect=_fake_run_factory(pane_pid, chain, sent)), \
             patch("cozempic.guard._is_claude_process", return_value=True), \
             patch("cozempic.guard._wait_for_exit", return_value=True), \
             patch("cozempic.guard._spawn_reload_watcher"), \
             patch("cozempic.guard.os.kill"), \
             patch("cozempic.guard.time.sleep"):
            guard._terminate_and_resume(
                claude_pid=pid, project_dir="/tmp/fake_project", session_id=self.sid,
            )
        return sent

    def test_foreign_pid_sends_nothing_and_leaks_no_sentinel(self):
        sent = self._run(89113, pane_pid=100, chain={89113: 1})
        keystrokes = [c for c in sent if c[:2] == ["tmux", "send-keys"]]
        self.assertEqual(keystrokes, [], f"keystrokes were sent into a pane that does not own the PID: {keystrokes}")
        self.assertFalse(self.sentinel.exists(), "refusal must not leave a reload sentinel")

    def test_owned_pid_still_reloads(self):
        # pane shell 100 → claude 4242: the legitimate case keeps working
        sent = self._run(4242, pane_pid=100, chain={4242: 100})
        keystrokes = [c for c in sent if c[:2] == ["tmux", "send-keys"]]
        self.assertEqual(len(keystrokes), 2, f"expected /exit then resume, got {keystrokes}")
        self.assertIn("/exit", keystrokes[0])
        self.assertTrue(any("claude --resume" in part for part in keystrokes[1]), keystrokes[1])

    def test_unset_pane_sends_nothing(self):
        from cozempic import guard
        sent: list = []
        env = {"TMUX": "/tmp/tmux-1/default,1,0"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TMUX_PANE", None)
            with patch.object(guard.subprocess, "run", side_effect=_fake_run_factory(100, {4242: 100}, sent)), \
                 patch("cozempic.guard._is_claude_process", return_value=True), \
                 patch("cozempic.guard._wait_for_exit", return_value=True), \
                 patch("cozempic.guard._spawn_reload_watcher"), \
                 patch("cozempic.guard.os.kill"), \
                 patch("cozempic.guard.time.sleep"):
                guard._terminate_and_resume(claude_pid=4242, project_dir="/tmp/fake_project", session_id=self.sid)
        self.assertEqual([c for c in sent if c[:2] == ["tmux", "send-keys"]], [])
        self.assertFalse(self.sentinel.exists())


class TestTreatTmuxPathRefusesForeignPane(unittest.TestCase):
    """`cozempic treat` has its own /exit keystroke; same guard, same proof."""

    def test_cli_source_consults_the_guard_before_send_keys(self):
        # Structural pin: the cli tmux branch must call the guard before its
        # send-keys. A behavioural test would need the whole treat pipeline;
        # this at least fails loudly if someone removes the call.
        src = (SRC / "cozempic" / "cli.py").read_text(encoding="utf-8")
        tmux_block = src[src.index('if term_env == "tmux":'):]
        guard_at = tmux_block.index("_tmux_pane_owns_pid(")
        keys_at = tmux_block.index('"send-keys"')
        self.assertLess(guard_at, keys_at, "cli tmux branch must check pane ownership before send-keys")


if __name__ == "__main__":
    unittest.main()
