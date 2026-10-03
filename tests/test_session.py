"""Where credentials come from, and runs taking turns on the session file."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from treaty import CliExit

from telegram_cli.session_lock import session_lock


def _api_id(home: Path, cwd: Path, **env: str) -> str:
    """TG_API_ID as a fresh process sees it, with ``home`` as HOME, run from ``cwd``"""
    environ = {k: v for k, v in os.environ.items() if not k.startswith("TG_")}
    environ.update(HOME=str(home), **env)
    code = "from telegram_cli.config import get_settings; print(get_settings().api_id)"
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=environ, capture_output=True, text=True,
        check=True,
    )
    return done.stdout.strip()


class TestEnvFiles:
    @pytest.fixture
    def home(self, tmp_path: Path) -> Path:
        home = tmp_path / "home"
        (home / ".telegram-cli").mkdir(parents=True)
        (home / ".telegram-cli" / ".env").write_text("TG_API_ID=111\nTG_API_HASH=h\n")
        return home

    def test_user_file_works_from_any_directory(self, home: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        assert _api_id(home, elsewhere) == "111"

    def test_local_env_file_overrides_the_user_file(self, home: Path, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text("TG_API_ID=222\n")
        assert _api_id(home, project) == "222"

    def test_environment_overrides_both(self, home: Path, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text("TG_API_ID=222\n")
        assert _api_id(home, project, TG_API_ID="333") == "333"


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    """A separate process holding the lock until the test ends"""
    path = tmp_path / "session.lock"
    script = textwrap.dedent(f"""
        import fcntl, os, sys, time
        fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, str(os.getpid()).encode())
        print("locked", flush=True)
        time.sleep(60)
    """)
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
    try:
        yield path, holder.pid
    finally:
        holder.kill()
        holder.wait()


async def _enter(path: Path, wait: float) -> None:
    async with session_lock(path, wait=wait):
        pass


class TestSessionLock:
    def test_busy_session_is_a_retryable_unavailable(self, held_lock: tuple[Path, int]) -> None:
        path, pid = held_lock
        with pytest.raises(CliExit) as caught:
            asyncio.run(_enter(path, wait=0.3))
        assert caught.value.name.value == "UNAVAILABLE"
        assert caught.value.code == "SESSION_BUSY"
        assert caught.value.context["holder_pid"] == pid

    def test_waits_for_the_holder_to_finish(self, tmp_path: Path) -> None:
        path = tmp_path / "session.lock"

        async def both() -> list[str]:
            order: list[str] = []

            async def first() -> None:
                async with session_lock(path):
                    order.append("first in")
                    await asyncio.sleep(0.3)
                    order.append("first out")

            async def second() -> None:
                await asyncio.sleep(0.05)
                async with session_lock(path):
                    order.append("second in")

            # Separate threads, as concurrent runs would be
            await asyncio.gather(asyncio.to_thread(asyncio.run, first()),
                                 asyncio.to_thread(asyncio.run, second()))
            return order

        assert asyncio.run(both()) == ["first in", "first out", "second in"]

    def test_nested_use_in_one_thread_fails_fast(self, tmp_path: Path) -> None:
        path = tmp_path / "session.lock"

        async def nested() -> None:
            async with session_lock(path), session_lock(path):
                pass

        with pytest.raises(RuntimeError, match="already held"):
            asyncio.run(nested())

    def test_released_after_use(self, tmp_path: Path) -> None:
        path = tmp_path / "session.lock"
        asyncio.run(_enter(path, wait=0))
        asyncio.run(_enter(path, wait=0))
