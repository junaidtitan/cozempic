"""File watcher for JSONL growth detection.

Uses kqueue on macOS (sub-millisecond latency, 0% CPU idle) with
os.stat() polling fallback on other platforms (200ms interval).

Stdlib only — no dependencies.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable


class JsonlWatcher:
    """Watch a JSONL file for size growth. Sub-second on macOS via kqueue."""

    def __init__(self, filepath: str, on_growth: Callable[[str, int], None]):
        self.filepath = filepath
        self.on_growth = on_growth
        self._running = False
        self._last_size = self._get_size()
        self._use_kqueue = hasattr(__import__("select"), "kqueue")

    def _get_size(self) -> int:
        try:
            return os.stat(self.filepath).st_size
        except OSError:
            return 0

    def start(self) -> None:
        """Block and watch for file growth. Run in a daemon thread."""
        self._running = True
        if self._use_kqueue:
            self._watch_kqueue()
        else:
            self._watch_poll()

    def stop(self) -> None:
        self._running = False

    def _watch_kqueue(self) -> None:
        """macOS kqueue watcher — 0.04ms wake latency, 0% CPU idle."""
        import select

        fd = os.open(self.filepath, os.O_RDONLY)
        try:
            kq = select.kqueue()
            ev = select.kevent(
                fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND,
            )
            while self._running:
                # Block up to 1s, then re-check _running
                events = kq.control([ev], 1, 1.0)
                if events:
                    new_size = self._get_size()
                    if new_size > self._last_size:
                        self._last_size = new_size
                        try:
                            self.on_growth(self.filepath, new_size)
                        except Exception:
                            pass  # Don't crash the watcher thread
        finally:
            os.close(fd)

    def _watch_poll(self) -> None:
        """Fallback polling watcher — 200ms interval."""
        while self._running:
            time.sleep(0.2)
            new_size = self._get_size()
            if new_size > self._last_size:
                self._last_size = new_size
                try:
                    self.on_growth(self.filepath, new_size)
                except Exception:
                    pass
