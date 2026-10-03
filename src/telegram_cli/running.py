"""Event loops whose Telegram work a SIGINT or SIGTERM can stop cleanly.

treaty runs a handler on a worker thread and, on a signal, calls the command's
``cleanup=`` hook on the main thread while the worker may still be inside its event
loop. The hook must not touch the Telethon client from there: the client belongs to the
worker's loop. Instead each loop registers here, and :func:`cancel_running` asks every
registered loop, through the thread-safe ``call_soon_threadsafe``, to cancel its task.
The cancellation unwinds the handler's ``async with get_client()`` on its own thread, so
the client disconnects and the session lock is released before the process exits.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Coroutine, Iterator
from contextlib import contextmanager
from typing import Any

CLEANUP_WAIT_SECONDS = 5.0

_registry_lock = threading.Lock()
_loops: set[CancellableLoop] = set()


async def _awaited[T](awaitable: Awaitable[T]) -> T:
    return await awaitable


class CancellableLoop:
    """An event loop driven from one thread that another thread may cancel"""

    def __init__(self) -> None:
        self._runner = asyncio.Runner()
        self._loop = self._runner.get_loop()
        self._lock = threading.Lock()
        self._task: asyncio.Task[Any] | None = None
        self._cancelled = False
        self._idle = threading.Event()
        self._idle.set()

    def run[T](self, awaitable: Awaitable[T]) -> T:
        """Run ``awaitable`` to completion; after :meth:`cancel` it is cancelled at once"""
        task = self._loop.create_task(_awaited(awaitable))
        with self._lock:
            self._task = task
            self._idle.clear()
            if self._cancelled:
                task.cancel()
        try:
            return self._loop.run_until_complete(task)
        finally:
            with self._lock:
                self._task = None
                self._idle.set()

    def finish[T](self, awaitable: Awaitable[T]) -> T:
        """Run closing work (an async generator's ``aclose``) even after :meth:`cancel`"""
        return self._loop.run_until_complete(_awaited(awaitable))

    def cancel(self) -> None:
        """Cancel the running task from any thread, and any task started after"""
        with self._lock:
            self._cancelled = True
            task = self._task
            if task is not None:
                self._loop.call_soon_threadsafe(task.cancel)

    def wait_idle(self, timeout: float) -> bool:
        """Whether the running task, if any, finished within ``timeout`` seconds"""
        return self._idle.wait(timeout)

    def close(self) -> None:
        """Cancel leftover tasks and close the loop, as ``asyncio.run`` does"""
        self._runner.close()


@contextmanager
def cancellable_loop() -> Iterator[CancellableLoop]:
    """A fresh loop that :func:`cancel_running` reaches until the block ends"""
    loop = CancellableLoop()
    with _registry_lock:
        _loops.add(loop)
    try:
        yield loop
    finally:
        with _registry_lock:
            _loops.discard(loop)
        loop.close()


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` for handlers: the treaty cleanup hook can cancel it"""
    with cancellable_loop() as loop:
        return loop.run(coro)


def cancel_running(wait: float = CLEANUP_WAIT_SECONDS) -> None:
    """treaty ``cleanup=`` hook: cancel every running Telegram call and wait for it to unwind

    Raises TimeoutError when a call is still running after ``wait`` seconds; treaty
    reports that as ``cleanup_failed`` in the CANCELLED envelope.
    """
    with _registry_lock:
        loops = list(_loops)
    for loop in loops:
        loop.cancel()
    deadline = time.monotonic() + wait
    stuck = sum(not loop.wait_idle(max(0.0, deadline - time.monotonic())) for loop in loops)
    if stuck:
        raise TimeoutError(f"{stuck} Telegram call(s) still running {wait:g}s after cancelling")
