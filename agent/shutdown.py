"""Cleanups to run when `systemctl restart` stops coolton.

It sends SIGTERM, which by default ends Python at once, without atexit or `finally`
(agent.inflight_runs is built around that). Cleanups registered here (leaving huddles
properly, sending the last traces) run first; then SIGTERM ends the process exactly as it
always did.
"""
from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable

logger = logging.getLogger(__name__)

_cleanups: list[Callable[[], None]] = []


def on_sigterm(cleanup: Callable[[], None]) -> None:
    """Run `cleanup` before a SIGTERM ends coolton (once install() has run)."""
    _cleanups.append(cleanup)


def install() -> None:
    """Install the SIGTERM handler. Call from the main thread (app.py)."""

    def handle(signum, frame):
        try:
            for cleanup in _cleanups:
                try:
                    cleanup()
                except Exception:
                    logger.exception("Shutdown cleanup failed")
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGTERM, handle)
