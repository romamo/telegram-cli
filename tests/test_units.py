"""Pure helpers: ID value objects, piped JSON, the dialog scan cap, plain rendering."""
from __future__ import annotations

import asyncio
import json

import pytest
from fakes import Dialog, FakeClient, user
from treaty import CliExit

from telegram_cli.commands.chats import ListChatsArgs, fetch_dialogs_with
from telegram_cli.commands.search import search_dialogs_with
from telegram_cli.ids import ChatId, MessageId
from telegram_cli.render import render_plain
from telegram_cli.utils import chat_id_field, message_id_field, parse_piped_items


class TestIds:
    def test_chat_ids_may_be_negative_but_not_zero(self) -> None:
        assert ChatId(-1001).value == -1001
        with pytest.raises(ValueError):
            ChatId(0)

    def test_message_ids_are_positive(self) -> None:
        with pytest.raises(ValueError):
            MessageId(0)

    @pytest.mark.parametrize("value", [True, "5", 1.0])
    def test_only_real_integers(self, value: object) -> None:
        with pytest.raises(TypeError):
            ChatId(value)


class TestPipedItems:
    def test_unwraps_a_treaty_envelope(self) -> None:
        envelope = {"ok": True, "data": [{"chat_id": 1, "msg_id": 2}], "meta": {}}
        assert parse_piped_items(json.dumps(envelope)) == [{"chat_id": 1, "msg_id": 2}]

    def test_reads_stream_lines_and_skips_the_closing_envelope(self) -> None:
        lines = [{"data": {"id": 5}, "meta": {"seq": 1}}, {"data": None, "meta": {"end": True}}]
        raw = "\n".join(json.dumps(line) for line in lines)
        assert parse_piped_items(raw) == [{"id": 5}]

    def test_accepts_bare_arrays_and_objects(self) -> None:
        assert parse_piped_items('[{"id": 1}]') == [{"id": 1}]
        assert parse_piped_items('{"id": 1}') == [{"id": 1}]
        assert parse_piped_items("  ") == []

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]"])
    def test_rejects_anything_else(self, raw: str) -> None:
        with pytest.raises(CliExit):
            parse_piped_items(raw)

    def test_id_fields_become_value_objects(self) -> None:
        item: dict[str, object] = {"chat_id": -100, "id": 7}
        assert chat_id_field(item, "chat_id") == ChatId(-100)
        assert message_id_field(item, "msg_id", "id") == MessageId(7)
        assert chat_id_field(item, "missing") is None

    @pytest.mark.parametrize("value", [0, True, "12"])
    def test_invalid_piped_ids_are_argument_errors(self, value: object) -> None:
        with pytest.raises(CliExit) as caught:
            chat_id_field({"chat_id": value}, "chat_id")
        assert caught.value.name.value == "ARG_ERROR"


def _dialogs(count: int) -> FakeClient:
    return FakeClient(dialogs=[Dialog(i, f"chat {i}", user(i)) for i in range(1, count + 1)])


class TestScanCap:
    def test_chats_list_reports_a_capped_scan(self) -> None:
        args = ListChatsArgs(query="chat", scan_limit=3)
        scan = asyncio.run(fetch_dialogs_with(_dialogs(5), None, args))
        assert scan.capped
        assert len(scan.rows) == 3

    def test_filling_the_page_is_not_a_capped_scan(self) -> None:
        scan = asyncio.run(fetch_dialogs_with(_dialogs(5), 2, ListChatsArgs(scan_limit=3)))
        assert not scan.capped
        assert [r.id for r in scan.rows] == [ChatId(1), ChatId(2)]

    def test_scan_limit_0_scans_everything(self) -> None:
        scan = asyncio.run(fetch_dialogs_with(_dialogs(5), None, ListChatsArgs(scan_limit=0)))
        assert not scan.capped
        assert len(scan.rows) == 5

    def test_name_search_reports_a_capped_scan(self) -> None:
        matches, capped = asyncio.run(search_dialogs_with(_dialogs(5), "chat", None, None, 2))
        assert capped
        assert len(matches) == 2


class TestRenderPlain:
    def test_lists_become_aligned_tables(self) -> None:
        text = render_plain([{"id": 1, "name": "Ann"}, {"id": 22, "name": None}])
        assert text == "id  name\n--  ----\n1   Ann\n22\n"

    def test_objects_become_key_value_lines(self) -> None:
        assert render_plain({"premium": True, "dc": 2}) == "premium: yes\ndc: 2\n"

    def test_empty_lists_say_so(self) -> None:
        assert render_plain([]) == "(no results)\n"

    def test_missing_values_leave_no_trailing_space(self) -> None:
        assert render_plain({"next_step": None}) == "next_step:\n"

    def test_nested_objects_flatten_to_dotted_keys(self) -> None:
        summary = "Deletes 0 message(s) matching 'spam' across 3 chat(s); nothing else changes"
        data = {"effect": "noop", "would_affect": {"summary": summary, "count": 0}}
        assert render_plain(data) == (
            f"effect: noop\nwould_affect.summary: {summary}\nwould_affect.count: 0\n"
        )

    def test_lists_of_objects_flatten_to_indexed_keys(self) -> None:
        data = {"chats": [{"chat_id": 1, "deleted": 2}, {"chat_id": 3, "deleted": 0}]}
        assert render_plain(data) == (
            "chats.0.chat_id: 1\nchats.0.deleted: 2\nchats.1.chat_id: 3\nchats.1.deleted: 0\n"
        )

    def test_empty_containers_keep_their_key(self) -> None:
        data = {"targets": [], "errors": {}, "next": {"cursor": None}}
        assert render_plain(data) == "targets: []\nerrors: {}\nnext.cursor:\n"

    def test_key_value_lines_are_never_truncated(self) -> None:
        long = "x" * 200
        assert render_plain({"text": long, "deep": {"text": long}}) == (
            f"text: {long}\ndeep.text: {long}\n"
        )

    def test_multiline_values_stay_on_one_line(self) -> None:
        assert render_plain({"a": {"b": "one\ntwo"}}) == "a.b: one two\n"

    def test_table_cells_are_still_truncated(self) -> None:
        text = render_plain([{"text": "x" * 200, "meta": {"k": 1}}])
        assert text.splitlines()[2] == "x" * 59 + "…" + '  {"k":1}'

    def test_streamed_messages_render_one_line_each(self) -> None:
        from telegram_cli.commands.export import render_message

        rows = [{"id": i, "date": "2024-01-01T00:00:00+00:00", "sender": "A", "text": "hi"}
                for i in (1, 2)]
        assert "".join(render_message(r) for r in rows).count("\n") == 2
