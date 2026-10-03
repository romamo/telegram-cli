"""What agents can rely on: review never waits for a person who isn't there, and logs
reach stderr through treaty."""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_cli import tg
from treaty import App, CliExit, Ctx, NoArgs

from telegram_cli.commands.chats import ReviewArgs, review_targets
from telegram_cli.ids import ChatId
from telegram_cli.logs import logging_to


class TestReview:
    def test_manifest_marks_it_interactive(self) -> None:
        _, envelope = tg("manifest")
        review = envelope["data"]["commands"]["chats.review"]
        assert review["interactive"] is True
        assert "chats delete" in review["description"]

    def test_refuses_before_anything_runs_when_no_one_can_answer(self) -> None:
        code, envelope = tg("chats", "review", "--chat", "5")
        assert code == 4
        assert envelope["error"]["code"] == "INPUT_REQUIRED"
        assert "chats delete" in envelope["error"]["suggestion"]

    def test_needs_chats_to_review(self) -> None:
        code, envelope = tg("chats", "review")
        assert code == 2

    def test_reads_chats_and_search_snippets_from_an_input_file(self, tmp_path: Path) -> None:
        hits = [
            {"chat_id": 5, "msg_id": 1, "text": "invoice"},
            {"chat_id": 5, "msg_id": 2, "text": "second hit"},
            {"chat_id": -1007, "msg_id": 3, "text": ""},
        ]
        source = tmp_path / "hits.json"
        source.write_text(json.dumps({"ok": True, "data": hits, "meta": {}}))
        targets = review_targets(ReviewArgs(chat=(ChatId(9),), input_file=source))
        assert targets == {ChatId(9): None, ChatId(5): "invoice", ChatId(-1007): None}

    def test_unreadable_input_file_is_an_argument_error(self, tmp_path: Path) -> None:
        with pytest.raises(CliExit) as caught:
            review_targets(ReviewArgs(input_file=tmp_path / "missing.json"))
        assert caught.value.name.value == "ARG_ERROR"

    def test_input_file_without_chats_is_an_argument_error(self, tmp_path: Path) -> None:
        source = tmp_path / "empty.json"
        source.write_text("[]")
        with pytest.raises(CliExit):
            review_targets(ReviewArgs(input_file=source))


@dataclass(frozen=True, slots=True)
class _Done:
    done: bool


def _logging_app() -> App:
    app = App("logdemo", version="0")

    @app.command("work", description="Log from a helper", danger_level="safe", exit_codes=())
    def work(args: NoArgs, ctx: Ctx) -> _Done:
        with logging_to(ctx):
            logging.getLogger("telegram_cli.helper").info("Deleting %d messages", 3)
            logging.getLogger("telegram_cli.helper").debug("hidden detail")
        return _Done(True)

    return app


class TestLogging:
    def test_package_logs_reach_stderr_as_treaty_json(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        code = _logging_app().run(["work", "--format", "json"], stdout=out, stderr=err)
        assert code == 0
        records = [json.loads(line) for line in err.getvalue().splitlines()]
        assert [r["message"] for r in records] == ["Deleting 3 messages"]
        assert records[0]["fields"]["logger"] == "telegram_cli.helper"
        # stdout still carries only the envelope
        assert json.loads(out.getvalue())["data"] == {"done": True}

    def test_logger_is_restored_after_the_run(self) -> None:
        logger = logging.getLogger("telegram_cli")
        before = (list(logger.handlers), logger.level, logger.propagate)
        _logging_app().run(["work", "--format", "json"], stdout=io.StringIO(), stderr=io.StringIO())
        assert (list(logger.handlers), logger.level, logger.propagate) == before
