"""SIGINT and SIGTERM stop a Telegram call on its own loop: the client's ``async with``
unwinds and the session lock is free before the process exits."""
from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from treaty import App, CliExit, Ctx, NoArgs

from telegram_cli.client import connect
from telegram_cli.commands.export import _blocking
from telegram_cli.running import cancel_running, run_async
from telegram_cli.session_lock import session_lock


class Held:
    """A stand-in Telegram call: holds the session lock until it is cancelled"""

    def __init__(self, lock: Path) -> None:
        self.lock = lock
        self.entered = threading.Event()
        self.steps: list[str] = []

    async def __call__(self) -> None:
        async with session_lock(self.lock, wait=0):
            self.steps.append("connected")
            self.entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                self.steps.append("disconnected")


def lock_is_free(lock: Path) -> bool:
    async def enter() -> None:
        async with session_lock(lock, wait=0):
            pass

    run_async(enter())
    return True


def test_cancel_running_unwinds_a_call_on_another_thread(tmp_path: Path) -> None:
    call = Held(tmp_path / "session.lock")
    outcome: list[str] = []

    def worker() -> None:
        try:
            run_async(call())
        except asyncio.CancelledError:
            outcome.append("cancelled")

    thread = threading.Thread(target=worker)
    thread.start()
    assert call.entered.wait(5)
    cancel_running(wait=5)
    assert call.steps == ["connected", "disconnected"]
    assert lock_is_free(call.lock)
    thread.join(5)
    assert outcome == ["cancelled"]


def test_cancel_running_with_nothing_running_is_a_no_op() -> None:
    cancel_running(wait=0)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_runs_the_hook_before_the_cancelled_envelope(
    tmp_path: Path, sig: signal.Signals
) -> None:
    call = Held(tmp_path / "session.lock")
    app = App("t", version="1", description="cancel test")

    @app.command(
        "hold",
        description="Hold the session until cancelled",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
        cleanup=cancel_running,
        timeout=30,  # a timeout puts the handler on a worker thread, as in tg
        examples=[("Hold", "t hold")],
    )
    def hold(args: NoArgs, ctx: Ctx) -> None:
        run_async(call())

    def interrupt() -> None:
        assert call.entered.wait(5)
        os.kill(os.getpid(), sig)

    sender = threading.Thread(target=interrupt)
    sender.start()
    out = io.StringIO()
    code = app.run(["hold", "--format", "json"], stdout=out, stderr=io.StringIO())
    sender.join()

    envelope = json.loads(out.getvalue().splitlines()[-1])
    assert code == {signal.SIGINT: 130, signal.SIGTERM: 143}[sig]
    assert envelope["error"]["code"] == "CANCELLED"
    assert "cleanup_failed" not in envelope["error"]["context"]
    assert call.steps == ["connected", "disconnected"]
    assert lock_is_free(call.lock)


class Unreachable:
    async def connect(self) -> None:
        raise ConnectionError("Connection to Telegram failed 5 time(s)")


def test_unreachable_telegram_is_a_retryable_unavailable() -> None:
    with pytest.raises(CliExit) as caught:
        asyncio.run(connect(Unreachable()))
    assert caught.value.name.value == "UNAVAILABLE"
    assert caught.value.code == "TELEGRAM_UNREACHABLE"
    assert "network" in (caught.value.suggestion or "")


@dataclass(frozen=True, slots=True)
class Event:
    n: int


def test_signal_stops_a_stream_waiting_on_telegram(tmp_path: Path) -> None:
    lock = tmp_path / "session.lock"
    entered = threading.Event()
    steps: list[str] = []
    app = App("t", version="1", description="cancel test")

    async def events() -> AsyncIterator[Event]:
        async with session_lock(lock, wait=0):
            try:
                yield Event(1)
                entered.set()
                await asyncio.sleep(60)
                yield Event(2)
            finally:
                steps.append("disconnected")

    @app.command(
        "export",
        description="Stream until cancelled",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
        cleanup=cancel_running,
        streaming=True,
        timeout=30,
        examples=[("Export", "t export")],
    )
    def export(args: NoArgs, ctx: Ctx) -> Iterator[Event]:
        yield from _blocking(events())

    def interrupt() -> None:
        assert entered.wait(5)
        os.kill(os.getpid(), signal.SIGINT)

    sender = threading.Thread(target=interrupt)
    sender.start()
    out = io.StringIO()
    code = app.run(["export", "--format", "jsonl"], stdout=out, stderr=io.StringIO())
    sender.join()

    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 130
    assert lines[-1]["error"]["code"] == "CANCELLED"
    assert "cleanup_failed" not in lines[-1]["error"]["context"]
    assert steps == ["disconnected"]
    assert lock_is_free(lock)
