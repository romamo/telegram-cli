"""The session and the credentials stay private to the user (issue #1)."""
from __future__ import annotations

import io
import json
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from treaty import App, Ctx, NoArgs

from telegram_cli.client import checked_client, new_client
from telegram_cli.config import Settings
from telegram_cli.privacy import exposures, make_private_dir


@pytest.fixture(autouse=True)
def umask() -> Iterator[None]:
    """A typical permissive umask, so files come out world-readable unless made private"""
    saved = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(saved)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def settings(session_dir: Path) -> Settings:
    return Settings(api_id=1, api_hash="hash", session_dir=session_dir)


def open_session(session_dir: Path) -> list[str]:
    """Create the session through the client factory; the paths it tightened"""
    client, tightened = new_client(settings(session_dir))
    client.session.close()
    return [str(e.path) for e in tightened]


class TestSessionFiles:
    def test_new_session_dir_and_file_are_private(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "home" / ".telegram-cli"
        assert open_session(session_dir) == []
        assert mode(session_dir) == 0o700
        assert mode(session_dir / "session.session") == 0o600

    def test_parents_keep_their_default_mode(self, tmp_path: Path) -> None:
        make_private_dir(tmp_path / "home" / "sessions")
        assert mode(tmp_path / "home") == 0o755
        assert mode(tmp_path / "home" / "sessions") == 0o700

    def test_existing_session_file_is_tightened_and_reported(self, tmp_path: Path) -> None:
        open_session(tmp_path)
        session = tmp_path / "session.session"
        journal = tmp_path / "session.session-journal"
        session.chmod(0o644)
        journal.write_bytes(b"")
        journal.chmod(0o644)
        assert open_session(tmp_path) == [str(session), str(journal)]
        assert mode(session) == 0o600
        assert mode(journal) == 0o600
        assert open_session(tmp_path) == []

    def test_existing_wide_dir_is_left_alone(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "chosen"
        session_dir.mkdir(mode=0o755)
        open_session(session_dir)
        assert mode(session_dir) == 0o755
        assert [e.path for e in exposures(session_dir, [])] == [session_dir]


@dataclass(frozen=True, slots=True)
class _Done:
    done: bool


def _warnings(session_dir: Path, env_file: Path) -> list[dict[str, Any]]:
    """The warnings of a run connecting three times, checking privacy on the first only,
    as `tg chats review` does"""
    app = App("privacydemo", version="0")
    env = [env_file, session_dir / "missing.env"]

    @app.command("work", description="Open the session", danger_level="safe", exit_codes=())
    def work(args: NoArgs, ctx: Ctx) -> _Done:
        for index in range(3):
            client = checked_client(ctx, settings(session_dir), env, check_privacy=index == 0)
            client.session.close()
        return _Done(True)

    out = io.StringIO()
    assert app.run(["work", "--format", "json"], stdout=out, stderr=io.StringIO()) == 0
    warnings: list[dict[str, Any]] = json.loads(out.getvalue())["warnings"]
    return warnings


class TestWarnings:
    def test_readable_env_file_is_a_warning(self, tmp_path: Path) -> None:
        make_private_dir(tmp_path / "s")
        env_file = tmp_path / ".env"
        env_file.write_text("TG_API_HASH=secret\n")
        assert mode(env_file) == 0o644
        [warning] = _warnings(tmp_path / "s", env_file)
        assert warning["code"] == "PERMISSIONS_TOO_OPEN"
        assert str(env_file) in json.dumps(warning)
        assert f"chmod 600 {env_file}" in json.dumps(warning)

    def test_wide_session_dir_is_a_warning(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("TG_API_HASH=secret\n")
        env_file.chmod(0o600)
        session_dir = tmp_path / "chosen"
        session_dir.mkdir(mode=0o755)
        [warning] = _warnings(session_dir, env_file)
        assert warning["code"] == "PERMISSIONS_TOO_OPEN"
        assert f"chmod 700 {session_dir}" in json.dumps(warning)

    def test_private_files_warn_nothing(self, tmp_path: Path) -> None:
        make_private_dir(tmp_path / "s")
        env_file = tmp_path / ".env"
        env_file.write_text("TG_API_HASH=secret\n")
        env_file.chmod(0o600)
        assert _warnings(tmp_path / "s", env_file) == []

    def test_each_exposure_warns_once_per_run(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "chosen"
        session_dir.mkdir(mode=0o755)
        open_session(session_dir)
        (session_dir / "session.session").chmod(0o644)
        env_file = tmp_path / ".env"
        env_file.write_text("TG_API_HASH=secret\n")
        warnings = _warnings(session_dir, env_file)
        assert sorted((w["code"], w["context"]["path"]) for w in warnings) == [
            ("PERMISSIONS_TOO_OPEN", str(env_file)),
            ("PERMISSIONS_TOO_OPEN", str(session_dir)),
            ("SESSION_FILE_TIGHTENED", str(session_dir / "session.session")),
        ]
