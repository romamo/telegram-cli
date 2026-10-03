"""One `tg` run at a time per session file.

Telethon keeps its session in SQLite and writes to it on every run, so two concurrent
runs fail with "database is locked". Each client holds an exclusive ``flock`` on a lock
file beside the session; a second run waits for it, then gives up with a retryable
``UNAVAILABLE`` exit. POSIX only (``fcntl``).
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from treaty import Exit

WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.2

_local = threading.local()


def _holder(fd: int) -> int | None:
    """The PID the current holder wrote into the lock file, if readable"""
    os.lseek(fd, 0, os.SEEK_SET)
    text = os.read(fd, 32).decode("ascii", errors="replace").strip()
    return int(text) if text.isdigit() else None


@asynccontextmanager
async def session_lock(path: Path, wait: float = WAIT_SECONDS) -> AsyncIterator[None]:
    """Hold the session's lock for the body; UNAVAILABLE (SESSION_BUSY) after ``wait``"""
    if getattr(_local, "held", False):
        # flock locks belong to the open file, so a nested client would wait on itself
        raise RuntimeError("the session lock is already held by this thread; reuse its client")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Exit.UNAVAILABLE(
                        "Another tg command is using the Telegram session.",
                        code="SESSION_BUSY",
                        context={"lock_file": str(path), "holder_pid": _holder(fd)},
                        suggestion="Retry when the other tg command finishes; runs sharing "
                        "a session must take turns.",
                        retry_after_ms=1000,
                    ) from None
                await asyncio.sleep(_POLL_SECONDS)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        _local.held = True
        try:
            yield
        finally:
            _local.held = False
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
