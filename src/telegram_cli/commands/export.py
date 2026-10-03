"""messages export — stream all messages from a chat."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from treaty import Ctx, Exit, Flag, ParseError

from telegram_cli.client import get_client, resolve_entity
from telegram_cli.ids import ChatId, MessageId
from telegram_cli.utils import chat_id_field, message_id_field, read_piped_items


@dataclass(frozen=True, slots=True)
class ExportArgs:
    chat: ChatId | None = Flag(
        default=None,
        description="Chat or group ID (from `tg chats list`); read from piped JSON when omitted",
    )
    limit: int | None = Flag(default=None, description="Max messages (omit for all)")
    since: date | None = Flag(
        default=None, description="Only messages on or after this ISO date, e.g. 2025-01-01"
    )

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 1:
            raise ParseError("--limit must be at least 1", context={"flag": "limit"})


@dataclass(frozen=True, slots=True)
class ExportedMessage:
    id: MessageId
    date: datetime | None
    sender_id: ChatId | None
    sender: str
    text: str
    has_media: bool
    reply_to: MessageId | None


def _sender_name(sender: Any) -> str:
    if sender is None:
        return ""
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    return f"{first} {last}".strip() or getattr(sender, "username", "") or ""


async def export_stream(
    chat_id: ChatId,
    limit: int | None,
    since: date | None,
    ids: list[MessageId] | None = None,
) -> AsyncGenerator[ExportedMessage]:
    """Messages of a chat, newest first; only ``ids`` when given"""
    since_dt = datetime(since.year, since.month, since.day, tzinfo=UTC) if since else None
    async with get_client() as client:
        entity = await resolve_entity(client, chat_id)

        async def iterate_messages() -> AsyncGenerator[Any]:
            if ids:
                # Specific IDs requested (e.g. piped search results): fetch only those
                for m in await client.get_messages(entity, ids=[i.value for i in ids]):
                    if m:
                        yield m
            else:
                async for m in client.iter_messages(entity, limit=limit):
                    yield m

        async for msg in iterate_messages():
            msg_date: datetime | None = msg.date
            if msg_date is not None and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=UTC)

            # The since filter only applies to a full history export
            if not ids and since_dt is not None:
                if msg_date is None:
                    continue
                if msg_date < since_dt:
                    # iter_messages goes newest-first: everything after this is older
                    break

            yield ExportedMessage(
                id=MessageId(msg.id),
                date=msg_date,
                sender_id=ChatId(msg.sender_id) if msg.sender_id else None,
                sender=_sender_name(msg.sender),
                text=msg.message or "",
                has_media=msg.media is not None,
                reply_to=MessageId(msg.reply_to.reply_to_msg_id)
                if msg.reply_to and msg.reply_to.reply_to_msg_id
                else None,
            )


def _blocking[T](agen: AsyncGenerator[T]) -> Iterator[T]:
    """Drive an async generator from synchronous code, one item per ``next()``"""
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                yield loop.run_until_complete(anext(agen))
            except StopAsyncIteration:
                return
    finally:
        loop.run_until_complete(agen.aclose())
        loop.close()


def _piped_selection() -> tuple[ChatId, list[MessageId]] | None:
    """The first chat in piped JSON and the message IDs piped for it"""
    items = read_piped_items()
    chat_id = next((c for item in items if (c := chat_id_field(item, "chat_id")) is not None), None)
    if chat_id is None:
        return None
    ids = [
        m
        for item in items
        if chat_id_field(item, "chat_id") in (None, chat_id)
        and (m := message_id_field(item, "msg_id", "id")) is not None
    ]
    return chat_id, ids


def export_messages(args: ExportArgs, ctx: Ctx) -> Iterator[ExportedMessage]:
    """Export messages from a chat or group, one event per message, newest first."""
    chat_id, ids = args.chat, None
    if chat_id is None:
        selection = _piped_selection()
        if selection is None:
            raise Exit.ARG_ERROR(
                "No chat to export.",
                suggestion="Pass --chat ID, or pipe JSON from `tg messages search`.",
            )
        chat_id, ids = selection
    yield from _blocking(export_stream(chat_id, args.limit, args.since, ids=ids or None))


def render_message(data: Any) -> str:
    """One line per exported message for `--format plain`"""
    if data is None:
        return ""
    stamp = (data.get("date") or "")[:19].replace("T", " ")
    text = (data.get("text") or "").replace("\n", " ").replace("\r", "")
    if len(text) > 80:
        text = text[:79] + "…"
    sender = (data.get("sender") or "")[:20]
    return f"{data['id']:<10} {stamp:<19}  {sender:<20}  {text}"
