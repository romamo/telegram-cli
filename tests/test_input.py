"""Input that does not come from flags: --input-file, piped JSON, and search results."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fakes import Dialog, FakeClient, group, user
from telethon.tl.types import User
from test_cli import tg
from treaty import CliExit

from telegram_cli.chat_type import ChatType
from telegram_cli.commands.delete import MessagesDeleteArgs, _targets
from telegram_cli.commands.folders import FolderPeersArgs, _chat_ids
from telegram_cli.commands.search import search_dialogs_with, search_messages_with
from telegram_cli.ids import ChatId, MessageId
from telegram_cli.utils import load_items


@pytest.fixture
def hits(tmp_path: Path) -> Path:
    """A saved `tg messages search --format json` result"""
    data = [
        {"chat_id": 5, "msg_id": 1, "text": "a"},
        {"chat_id": 5, "msg_id": 2, "text": "b"},
        {"chat_id": -1007, "msg_id": 9, "text": "c"},
    ]
    path = tmp_path / "hits.json"
    path.write_text(json.dumps({"ok": True, "data": data, "meta": {}}))
    return path


class TestInputFile:
    def test_loads_items_from_a_file(self, hits: Path) -> None:
        assert len(load_items(hits)) == 3

    def test_missing_file_is_an_argument_error(self, tmp_path: Path) -> None:
        with pytest.raises(CliExit) as caught:
            load_items(tmp_path / "missing.json")
        assert caught.value.name.value == "ARG_ERROR"

    def test_messages_delete_groups_file_hits_by_chat(self, hits: Path) -> None:
        targets = _targets(MessagesDeleteArgs(input_file=hits))
        assert targets == {
            ChatId(5): [MessageId(1), MessageId(2)],
            ChatId(-1007): [MessageId(9)],
        }

    def test_folders_take_chat_ids_from_a_file(self, hits: Path) -> None:
        assert _chat_ids(FolderPeersArgs(folder="Work", input_file=hits)) == [
            ChatId(5),
            ChatId(-1007),
        ]

    @pytest.mark.parametrize(
        "argv",
        [
            ("messages", "delete", "--chat", "1", "--message", "2", "--input-file", "/x.json"),
            ("folders", "add", "Work", "--chat", "1", "--input-file", "/x.json"),
            ("messages", "export", "--chat", "1", "--input-file", "/x.json"),
        ],
    )
    def test_input_file_and_id_flags_exclude_each_other(self, argv: tuple[str, ...]) -> None:
        code, envelope = tg(*argv)
        assert code == 2
        assert envelope["error"]["phase"] == "validation"

    def test_without_flags_or_a_file_the_empty_stdin_is_an_argument_error(self) -> None:
        # What exec lines and MCP calls see: treaty gives them an empty stdin
        code, envelope = tg("messages", "delete")
        assert code == 1
        assert "--input-file" in envelope["error"]["suggestion"]


class TestMessageSearch:
    def test_type_filter_uses_the_chats_sent_with_the_results(self) -> None:
        client = FakeClient()
        client.add_chat(user(5), "invoice from Ann")
        grp = client.add_chat(group(7), "invoice in the group")
        found = asyncio.run(
            search_messages_with(client, "invoice", None, ChatType.GROUP, None)
        )
        assert [m.chat_id for m in found] == [ChatId(grp)]
        assert not [c for c in client.calls if c.startswith("get_entity")]


class TestChatSearch:
    def test_existing_chats_match_by_username_too(self) -> None:
        roma = User(id=145, first_name="Roman", username="romavm")
        client = FakeClient(dialogs=[Dialog(145, "Roman", roma), Dialog(7, "Ann", user(7))])
        for phrase in ("romavm", "@romavm", "ROMA"):
            found, _ = asyncio.run(search_dialogs_with(client, phrase, None, None, 0))
            assert [(m.id, m.match) for m in found] == [
                (ChatId(145), "name" if phrase == "ROMA" else "username")
            ], phrase
        assert found[0].snippet == "Roman"

    def test_username_hits_show_the_handle(self) -> None:
        roma = User(id=145, first_name="Roman", username="romavm")
        client = FakeClient(dialogs=[Dialog(145, "Roman", roma)])
        found, _ = asyncio.run(search_dialogs_with(client, "@romavm", None, None, 0))
        assert found[0].snippet == "@romavm"
