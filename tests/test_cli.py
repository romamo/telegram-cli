"""The command-line contract, end to end through treaty, without touching Telegram."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from telegram_cli.cli import app


def tg(*argv: str, stdin: str = "") -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"], stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO()
    )
    return code, json.loads(out.getvalue().splitlines()[-1])


def test_manifest_lists_every_command() -> None:
    code, envelope = tg("manifest")
    assert code == 0
    assert set(envelope["data"]["commands"]) >= {
        "auth",
        "stats",
        "search",
        "chats.list",
        "chats.delete",
        "chats.review",
        "messages.search",
        "messages.delete",
        "messages.cleanup",
        "messages.export",
        "folders.list",
        "folders.create",
        "folders.add",
        "folders.remove",
        "folders.delete",
    }


@pytest.mark.parametrize(
    ("argv", "field"),
    [
        (("messages", "delete", "--ids", "5"), "chat"),
        (("messages", "delete", "--chat", "1"), "ids"),
        (("messages", "delete", "--chat", "0", "--ids", "5"), "chat"),
        (("messages", "delete", "--chat", "1", "--ids", "0"), "ids"),
        (("chats", "delete", "--id", "x"), "id"),
        (("chats", "list", "--scan-limit", "-1"), "scan-limit"),
        (("chats", "list", "--type", "bogus"), "type"),
        (("messages", "export", "--since", "2025-13-01"), "since"),
        (("messages", "cleanup", "spam", "--max-matches", "0"), "max-matches"),
        (("auth", "--phone", "not-a-phone"), "phone"),
        (("auth", "--phone", "+380501234567", "--code", "12345"), "code"),
    ],
)
def test_bad_arguments_exit_2_before_anything_runs(argv: tuple[str, ...], field: str) -> None:
    code, envelope = tg(*argv)
    assert code == 2
    assert envelope["error"]["phase"] == "validation"
    fields = {e.get("field") for e in envelope["error"]["errors"]}
    assert field in fields


def test_destructive_commands_require_confirmation() -> None:
    code, envelope = tg("manifest")
    commands = envelope["data"]["commands"]
    for path in ("chats.delete", "messages.delete", "messages.cleanup", "folders.delete"):
        assert commands[path]["danger_level"] == "destructive"


def test_missing_credentials_exit_4_with_a_fix(tmp_path: Path) -> None:
    # A fresh process in an empty directory: no .env, no TG_* variables
    env = {k: v for k, v in os.environ.items() if not k.startswith("TG_")}
    env["HOME"] = str(tmp_path)
    done = subprocess.run(
        [sys.executable, "-m", "telegram_cli.cli", "stats", "--format", "json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    envelope = json.loads(done.stdout.splitlines()[-1])
    assert done.returncode == 4
    assert envelope["error"]["code"] == "PRECONDITION"
    assert "TG_API_ID" in envelope["error"]["suggestion"]
