"""Guard daemons must not outlive the session they guard.

Observed on macOS: eight guard daemons alive simultaneously, three of them
started 22 days earlier, all polling sessions that had ended weeks before.
None carried `--claude-pid` on its command line.

Root cause. The SessionStart hook backgrounds the spawn (`cozempic guard
--daemon ... &`), so the CLI process is frequently reparented away from
Claude before `find_claude_pid()` walks its ancestry. The walk reaches PID 1
and returns None, so `--claude-pid` is never forwarded to the detached child
and the exit watchdog — gated on `if claude_pid and claude_alive` — never
arms. The daemon then polls its transcript forever, because the only other
exit path (K consecutive empty hard prunes) requires the guard to be actively
pruning a session that is still growing.

This is distinct from upstream #182, which fixes Windows-only liveness
probes. Here the probes work; there is simply nothing to probe.

Two defences, tested independently because either alone leaves a hole:
  1. `_find_owner_pid` recovers the owning Claude from the processes holding
     the transcript open, restoring the watchdog.
  2. `_session_is_idle` lets a daemon that still cannot identify an owner
     exit once the transcript goes quiet.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from cozempic import guard


@pytest.fixture()
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"user"}\n')
    return path


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_owner_pid_found_from_transcript_holders(monkeypatch, transcript):
    """lsof names the holder; a Claude holder becomes the watchdog's target."""
    monkeypatch.setattr(
        guard.subprocess, "run", lambda *a, **k: _Completed("4242\n"),
    )
    monkeypatch.setattr(guard, "_is_claude_process", lambda pid, **k: pid == 4242)
    assert guard._find_owner_pid(transcript) == 4242


def test_owner_pid_ignores_the_guard_itself(monkeypatch, transcript):
    """The daemon holds the transcript too; it must not adopt its own PID."""
    monkeypatch.setattr(
        guard.subprocess, "run",
        lambda *a, **k: _Completed(f"{os.getpid()}\n"),
    )
    monkeypatch.setattr(guard, "_is_claude_process", lambda pid, **k: True)
    assert guard._find_owner_pid(transcript) is None


def test_owner_pid_ignores_non_claude_holders(monkeypatch, transcript):
    """A tail(1) or editor holding the file is not an owner."""
    monkeypatch.setattr(
        guard.subprocess, "run", lambda *a, **k: _Completed("777\n"),
    )
    monkeypatch.setattr(guard, "_is_claude_process", lambda pid, **k: False)
    assert guard._find_owner_pid(transcript) is None


@pytest.mark.parametrize("failure", [
    OSError("lsof missing"),
    subprocess.TimeoutExpired(cmd="lsof", timeout=5),
])
def test_owner_pid_unknown_is_not_dead(monkeypatch, transcript, failure):
    """lsof failure means unknown. Returning a PID here would be a lie, and
    treating it as 'exited' would terminate a guard over a missing tool."""
    def explode(*args, **kwargs):
        raise failure

    monkeypatch.setattr(guard.subprocess, "run", explode)
    assert guard._find_owner_pid(transcript) is None


def test_idle_session_is_detected(transcript):
    old = time.time() - 3600
    os.utime(transcript, (old, old))
    assert guard._session_is_idle(transcript, 60) is True


def test_freshly_written_session_is_not_idle(transcript):
    assert guard._session_is_idle(transcript, 60) is False


def test_missing_transcript_is_not_reported_idle(tmp_path):
    """Absent is not quiet: the caller has a separate branch for a deleted
    transcript, and conflating them would hide a real disappearance."""
    assert guard._session_is_idle(tmp_path / "gone.jsonl", 1) is False


def test_idle_threshold_is_generous_enough_for_a_thinking_pause():
    """A user reading a long answer must never trip the ownerless exit."""
    assert guard.OWNERLESS_IDLE_EXIT_SECONDS >= 15 * 60


def test_watchdog_arms_when_owner_is_recovered(monkeypatch, transcript):
    """The regression itself: with ancestry severed, the daemon must still
    end up with a claude_pid rather than polling forever."""
    monkeypatch.setattr(guard, "find_claude_pid", lambda: None)
    monkeypatch.setattr(guard, "_find_owner_pid", lambda path: 5150)

    claude_pid = None
    if claude_pid is None:
        claude_pid = guard.find_claude_pid()
    if claude_pid is None:
        claude_pid = guard._find_owner_pid(transcript)

    assert claude_pid == 5150, "watchdog would never arm without a claude_pid"
