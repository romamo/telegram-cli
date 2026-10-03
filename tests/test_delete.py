"""Destructive commands against an in-memory client: counts, idempotency, partial failure."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fakes import FakeClient, basic_group, group, user
from telethon.errors import ChatAdminRequiredError, MessageDeleteForbiddenError
from telethon.tl.types import User
from treaty import CliExit

from telegram_cli.client import resolve_entity, rpc_failure
from telegram_cli.commands.delete import (
    ChatDeleteArgs,
    cleanup_with,
    clear_history_with,
    delete_chat_with,
    delete_messages_with,
)
from telegram_cli.ids import ChatId, MessageId


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def exit_name(exc: CliExit) -> str:
    return exc.name.value


class TestClearHistory:
    def test_private_chat_repeats_until_telegram_reports_no_offset(self) -> None:
        client = FakeClient(history_batch=2)
        chat = client.add_chat(user(5), "a", "b", "c", "d", "e")
        deleted = run(clear_history_with(client, client.entities[chat], ChatId(chat)))
        assert deleted == 5
        assert client.messages[chat] == []
        assert client.calls.count(f"delete_history:{chat}") == 3

    def test_group_non_admin_deletes_only_own_messages(self) -> None:
        client = FakeClient()
        chat = client.add_chat(group(7), "theirs 1", "theirs 2")
        client.add_chat(group(7), "mine", out=True)
        deleted = run(clear_history_with(client, client.entities[chat], ChatId(chat)))
        assert deleted == 1
        assert [m.message for m in client.messages[chat]] == ["theirs 1", "theirs 2"]

    def test_group_admin_deletes_everything(self) -> None:
        client = FakeClient()
        chat = client.add_chat(group(7, admin=True), "theirs")
        client.add_chat(group(7, admin=True), "mine", out=True)
        deleted = run(clear_history_with(client, client.entities[chat], ChatId(chat)))
        assert deleted == 2
        assert client.messages[chat] == []


class TestChatDelete:
    def test_dry_run_changes_nothing(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a")
        result = run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat), dry_run=True)))
        assert result.effect == "would_delete"
        assert result.would_affect is not None and "Ann" in result.would_affect.summary
        assert len(client.messages[chat]) == 1

    def test_repeat_on_an_empty_chat_is_a_noop(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a")
        first = run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat))))
        again = run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat))))
        assert (first.effect, first.deleted_count) == ("deleted", 1)
        assert (again.effect, again.deleted_count) == ("noop", 0)

    def test_non_admin_dry_run_says_only_own_messages_go(self) -> None:
        client = FakeClient()
        chat = client.add_chat(group(7), "theirs")
        result = run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat), dry_run=True)))
        assert result.own_messages_only
        assert result.would_affect is not None
        assert "your own messages" in result.would_affect.summary

    def test_unknown_chat_is_not_found(self) -> None:
        with pytest.raises(CliExit) as caught:
            run(resolve_entity(FakeClient(), ChatId(404)))
        assert exit_name(caught.value) == "NOT_FOUND"


class TestMessagesDelete:
    def test_reports_found_and_missing_and_deletes_found(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a", "b")
        targets = {ChatId(chat): [MessageId(1), MessageId(9)]}
        result = run(delete_messages_with(client, targets, dry_run=False))
        assert result.effect == "deleted"
        assert result.deleted_count == 1
        assert [m.msg_id for m in result.messages] == [MessageId(1)]
        assert [k.msg_id for k in result.not_found] == [MessageId(9)]
        assert [m.id for m in client.messages[chat]] == [2]

    def test_retry_after_success_is_a_noop_not_an_error(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a")
        targets = {ChatId(chat): [MessageId(1)]}
        run(delete_messages_with(client, targets, dry_run=False))
        again = run(delete_messages_with(client, targets, dry_run=False))
        assert (again.effect, again.deleted_count) == ("noop", 0)
        assert len(again.not_found) == 1

    def test_dry_run_previews_without_deleting(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "hello")
        result = run(delete_messages_with(client, {ChatId(chat): [MessageId(1)]}, dry_run=True))
        assert result.effect == "would_delete"
        assert result.would_affect is not None and result.would_affect.count == 1
        assert result.messages[0].text == "hello"
        assert len(client.messages[chat]) == 1


class TestCleanup:
    def test_deletes_own_matches_in_every_chat(self) -> None:
        client = FakeClient()
        a = client.add_chat(user(5), "spam 1", "keep", out=True)
        b = client.add_chat(user(6, "Bob"), "spam 2", out=True)
        result = run(cleanup_with(client, "spam", 100, dry_run=False))
        assert (result.effect, result.matched, result.deleted_count) == ("deleted", 2, 2)
        assert [m.message for m in client.messages[a]] == ["keep"]
        assert client.messages[b] == []

    def test_keeps_other_peoples_matches_by_default(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "spam from Ann")
        client.add_chat(user(5), "spam from me", out=True)
        result = run(cleanup_with(client, "spam", 100, dry_run=False))
        assert (result.matched, result.deleted_count, result.skipped_others) == (1, 1, 1)
        assert not result.include_others
        assert [m.message for m in client.messages[chat]] == ["spam from Ann"]

    def test_include_others_deletes_everyones_matches(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "spam from Ann")
        client.add_chat(user(5), "spam from me", out=True)
        result = run(cleanup_with(client, "spam", 100, dry_run=False, include_others=True))
        assert (result.matched, result.deleted_count, result.skipped_others) == (2, 2, 0)
        assert result.include_others
        assert client.messages[chat] == []

    def test_dry_run_names_the_scope(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "spam from Ann")
        client.add_chat(user(5), "spam from me", out=True)
        own = run(cleanup_with(client, "spam", 100, dry_run=True))
        everyone = run(cleanup_with(client, "spam", 100, dry_run=True, include_others=True))
        assert own.would_affect is not None and everyone.would_affect is not None
        assert own.would_affect.summary == (
            "Deletes 1 of your own message(s) matching 'spam' from 1 chat(s); "
            "keeps 1 sent by others"
        )
        assert "including other people's (for both sides" in everyone.would_affect.summary
        assert everyone.would_affect.count == 2
        assert len(client.messages[chat]) == 2

    def test_max_matches_counts_only_messages_in_scope(self) -> None:
        client = FakeClient()
        client.add_chat(user(5), "spam a", "spam b", "spam c")
        client.add_chat(user(5), "spam mine 1", "spam mine 2", "spam mine 3", out=True)
        result = run(cleanup_with(client, "spam", 2, dry_run=True))
        assert (result.matched, result.skipped_others, result.limit_reached) == (2, 3, True)
        # Stops reading the search once the limit is met
        assert client.scanned == 5

    def test_one_forbidden_chat_is_a_partial_failure_and_the_rest_still_run(self) -> None:
        client = FakeClient()
        blocked = client.add_chat(group(7), "spam here", out=True)
        ok = client.add_chat(user(5), "spam there", out=True)
        client.forbidden.add(blocked)
        with pytest.raises(CliExit) as caught:
            run(cleanup_with(client, "spam", 100, dry_run=False))
        assert exit_name(caught.value) == "PARTIAL_FAILURE"
        data = caught.value.data
        assert data.deleted_count == 1
        assert {d.chat_id.value: d.error for d in data.chats} == {
            blocked: "MessageDeleteForbiddenError",
            ok: None,
        }
        assert client.messages[ok] == []

    def test_flags_when_the_match_limit_is_reached(self) -> None:
        client = FakeClient()
        client.add_chat(user(5), "spam 1", "spam 2", "spam 3", out=True)
        result = run(cleanup_with(client, "spam", 2, dry_run=True))
        assert result.limit_reached
        assert result.matched == 2

    def test_fewer_matches_than_the_limit_are_not_flagged(self) -> None:
        client = FakeClient()
        client.add_chat(user(5), "spam 1", out=True)
        result = run(cleanup_with(client, "spam", 2, dry_run=True))
        assert not result.limit_reached

    def test_nothing_left_is_a_noop(self) -> None:
        client = FakeClient()
        client.add_chat(user(5), "keep", out=True)
        result = run(cleanup_with(client, "spam", 100, dry_run=False))
        assert (result.effect, result.deleted_count) == ("noop", 0)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MessageDeleteForbiddenError(request=None), "PERMISSION_DENIED"),
        (ChatAdminRequiredError(request=None), "PERMISSION_DENIED"),
    ],
)
def test_telegram_permission_errors_become_permission_denied(
    error: Exception, expected: str
) -> None:
    assert exit_name(rpc_failure(error)) == expected


def test_counts_never_exceed_the_messages_asked_for() -> None:
    from telethon.tl.types.messages import AffectedMessages

    from telegram_cli.commands.delete import _affected

    # Emptying Saved Messages reports one more event than messages deleted
    assert _affected([AffectedMessages(pts=1, pts_count=7)], requested=6) == 6
    assert _affected([AffectedMessages(pts=1, pts_count=2)], requested=6) == 2


def test_other_telegram_errors_name_their_cause() -> None:
    from telethon.errors import PeerIdInvalidError

    exc = rpc_failure(PeerIdInvalidError(request=None))
    assert exit_name(exc) == "GENERAL_ERROR"
    assert "PeerIdInvalidError" in exc.message
    assert "invalid Peer" in exc.message


class TestRevoke:
    def test_private_chat_preview_says_both_sides_and_clears_with_revoke(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a")
        preview = run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat), dry_run=True)))
        assert preview.would_affect is not None
        assert preview.would_affect.summary.endswith("(Ann) for both sides")
        assert preview.revoked
        result = run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat))))
        assert result.revoked
        assert client.revokes == [True]

    def test_just_me_clears_a_private_chat_without_revoking(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a")
        args = ChatDeleteArgs(ChatId(chat), just_me=True)
        preview_args = ChatDeleteArgs(ChatId(chat), just_me=True, dry_run=True)
        preview = run(delete_chat_with(client, preview_args))
        assert preview.would_affect is not None and "for you only" in preview.would_affect.summary
        result = run(delete_chat_with(client, args))
        assert (result.deleted_count, result.revoked) == (1, False)
        assert client.revokes == [False]

    def test_removing_a_private_chat_says_both_sides(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a")
        args = ChatDeleteArgs(ChatId(chat), remove=True, dry_run=True)
        preview = run(delete_chat_with(client, args))
        assert preview.would_affect is not None
        assert "deleting its history for both sides" in preview.would_affect.summary
        assert preview.revoked

    def test_just_me_in_a_basic_group_deletes_without_revoking(self) -> None:
        client = FakeClient()
        chat = client.add_chat(basic_group(9), "theirs", "mine")
        result = run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat), just_me=True)))
        assert (result.deleted_count, result.revoked) == (2, False)
        assert client.revokes == [False]

    def test_just_me_is_refused_in_a_supergroup_before_anything_is_deleted(self) -> None:
        client = FakeClient()
        chat = client.add_chat(group(7, admin=True), "a")
        with pytest.raises(CliExit) as caught:
            run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat), just_me=True)))
        assert exit_name(caught.value) == "ARG_ERROR"
        assert caught.value.context["chat_ids"] == [chat]
        assert len(client.messages[chat]) == 1

    def test_messages_delete_revokes_by_default_and_says_so(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a")
        targets = {ChatId(chat): [MessageId(1)]}
        preview = run(delete_messages_with(client, targets, dry_run=True))
        assert preview.would_affect is not None
        assert "for both sides in private chats" in preview.would_affect.summary
        result = run(delete_messages_with(client, targets, dry_run=False))
        assert result.revoked
        assert client.revokes == [True]

    def test_messages_delete_just_me_sends_revoke_false(self) -> None:
        client = FakeClient()
        chat = client.add_chat(user(5), "a")
        targets = {ChatId(chat): [MessageId(1)]}
        result = run(delete_messages_with(client, targets, dry_run=False, just_me=True))
        assert (result.deleted_count, result.revoked) == (1, False)
        assert client.revokes == [False]

    def test_messages_delete_just_me_refuses_the_whole_run_if_any_chat_is_a_channel(self) -> None:
        client = FakeClient()
        private = client.add_chat(user(5), "a")
        channel = client.add_chat(group(7), "b")
        targets = {ChatId(private): [MessageId(1)], ChatId(channel): [MessageId(1)]}
        with pytest.raises(CliExit) as caught:
            run(delete_messages_with(client, targets, dry_run=False, just_me=True))
        assert exit_name(caught.value) == "ARG_ERROR"
        assert client.revokes == []
        assert len(client.messages[private]) == 1

    def test_saved_messages_preview_names_no_other_side(self) -> None:
        client = FakeClient()
        chat = client.add_chat(User(id=1, first_name="Me", is_self=True), "note")
        preview = run(delete_chat_with(client, ChatDeleteArgs(ChatId(chat), dry_run=True)))
        assert preview.would_affect is not None
        assert "both sides" not in preview.would_affect.summary
