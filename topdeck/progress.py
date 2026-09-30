"""Minimal stderr progress for long cold fetches.

One line, `label: n/total`, on stderr. No TUI, no dependencies, no
progress when there is nothing to wait for. The CLI disables it under
--json; everywhere else it defaults to on.
"""

from __future__ import annotations

import sys
import threading

_enabled = True


def set_enabled(value: bool) -> None:
    """Turn progress output on or off. The CLI turns it off under --json."""
    global _enabled
    _enabled = value


def is_enabled() -> bool:
    return _enabled


class Progress:
    """A thread-safe `label: n/total` counter on stderr.

    Single-item jobs never print: there is nothing to wait for.
    Call tick() once per finished unit, finish() when the job is done.
    """

    def __init__(self, label: str, total: int):
        self._label = label
        self._total = total
        self._done = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return _enabled and self._total > 1

    def tick(self) -> None:
        if not self.active:
            return
        with self._lock:
            self._done += 1
            done = self._done
        print(f"\r{self._label}: {done}/{self._total}", end="", file=sys.stderr, flush=True)

    def finish(self) -> None:
        if self.active:
            print(file=sys.stderr, flush=True)
