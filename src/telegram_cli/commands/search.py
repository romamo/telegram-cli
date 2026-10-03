"""search — find chats by name or search messages across dialogs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from telethon import TelegramClient, functions, utils
from treaty import Arg, Ctx, Flag, Page

from telegram_cli.chat_type import ChatType, dialog_type
from telegram_cli.client import get_client, resolve_entity
from telegram_cli.commands.chats import (
    SCAN_LIMIT_DESCRIPTION,
    check_scan_limit,
    warn_scan_capped,
)
from telegram_cli.ids import ChatId, MessageId
from telegram_cli.logs import logging_to
from telegram_cli.utils import fetch_count

# Results asked of Telegram's global directory when no limit is given
_GLOBAL_LIMIT = 100


# ── search (chat names) ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SearchArgs:
    phrase: str = Arg(description="Text to look for in chat names")
    type: ChatType | None = Flag(default=None, description="Filter by chat type")
    scan_limit: int = Flag(default=3000, description=SCAN_LIMIT_DESCRIPTION)

    def __post_init__(self) -> None:
        check_scan_limit(self.scan_limit)


@dataclass(frozen=True, slots=True)
class ChatMatch:
    id: ChatId
    name: str
    type: ChatType
    match: Literal["name", "global"]
    snippet: str


async def search_dialogs_with(
    client: TelegramClient,
    phrase: str,
    limit: int | None,
    chat_type: ChatType | None,
    scan_limit: int,
) -> tuple[list[ChatMatch], bool]:
    """Search dialog names in the user's account, archived included; the flag says whether
    the scan stopped at ``scan_limit`` dialogs (0: no limit)"""
    phrase_lower = phrase.lower()
    results: list[ChatMatch] = []
    fetched_count = 0
    async for d in client.iter_dialogs():
        fetched_count += 1
        if scan_limit and fetched_count > scan_limit:
            return results, True
        dtype = dialog_type(d.entity)
        if chat_type and dtype != chat_type:
            continue
        name = d.name or ""
        if phrase_lower in name.lower():
            results.append(
                ChatMatch(id=ChatId(d.id), name=name, type=dtype, match="name", snippet=name)
            )
            if limit is not None and len(results) >= limit:
                break
    return results, False


def _entity_name(entity: Any) -> str:
    return utils.get_display_name(entity) or getattr(entity, "username", None) or str(entity.id)


async def _search_global(
    client: TelegramClient, phrase: str, limit: int | None, chat_type: ChatType | None
) -> list[ChatMatch]:
    """Search Telegram's global directory for public users and channels."""
    found = await client(functions.contacts.SearchRequest(q=phrase, limit=limit or _GLOBAL_LIMIT))
    results: list[ChatMatch] = []
    for entity in [*found.users, *found.chats]:
        dtype = dialog_type(entity)
        if chat_type and dtype != chat_type:
            continue
        name = _entity_name(entity)
        username = getattr(entity, "username", None)
        results.append(
            ChatMatch(
                id=ChatId(utils.get_peer_id(entity)),
                name=name,
                type=dtype,
                match="global",
                snippet=f"@{username}" if username else name,
            )
        )
    return results


async def _combined_search(args: SearchArgs, limit: int | None) -> tuple[list[ChatMatch], bool]:
    async with get_client() as client:
        # Local and global searches run in parallel on the same client
        (local, capped), remote = await asyncio.gather(
            search_dialogs_with(client, args.phrase, limit, args.type, args.scan_limit),
            _search_global(client, args.phrase, limit, args.type),
        )
    # Local hits (existing chats) win over global ones
    merged: dict[ChatId, ChatMatch] = {}
    for match in [*local, *remote]:
        merged.setdefault(match.id, match)
    return list(merged.values())[:limit], capped


def search(args: SearchArgs, ctx: Ctx) -> Page[ChatMatch]:
    """Search chats by name: your dialogs first, then Telegram's global directory."""
    with logging_to(ctx):
        matches, capped = asyncio.run(_combined_search(args, fetch_count(ctx.page)))
        if capped:
            warn_scan_capped(ctx, args.scan_limit)
        return Page(items=matches)


# ── messages search ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MessageSearchArgs:
    phrase: str = Arg(description="Text to look for in messages")
    type: ChatType | None = Flag(default=None, description="Filter by chat type")
    chat: ChatId | None = Flag(default=None, description="Search only inside this chat ID")


@dataclass(frozen=True, slots=True)
class MessageMatch:
    chat_id: ChatId
    msg_id: MessageId
    date: datetime | None
    sender_id: ChatId | None
    text: str


def _match(chat_id: ChatId, msg: Any) -> MessageMatch:
    return MessageMatch(
        chat_id=chat_id,
        msg_id=MessageId(msg.id),
        date=msg.date,
        sender_id=ChatId(msg.sender_id) if msg.sender_id else None,
        text=(msg.message or "").replace("\n", " ")[:120],
    )


async def search_messages_with(
    client: TelegramClient,
    phrase: str,
    limit: int | None,
    chat_type: ChatType | None,
    chat: ChatId | None,
) -> list[MessageMatch]:
    """Server-side message search, in one chat or across all of them."""
    if chat is not None:
        entity = await resolve_entity(client, chat)
        messages = await client.get_messages(entity, search=phrase, limit=limit)
        return [_match(chat, msg) for msg in messages]

    # A type filter drops hits, so ask for more
    asked = limit * 3 if (chat_type and limit is not None) else limit
    results: list[MessageMatch] = []
    for msg in await client.get_messages(None, search=phrase, limit=asked):
        # get_chat() reuses the chats Telegram returned with the search; it only
        # fetches when one is missing
        if chat_type and dialog_type(await msg.get_chat()) != chat_type:
            continue
        results.append(_match(ChatId(utils.get_peer_id(msg.peer_id)), msg))
        if limit is not None and len(results) >= limit:
            break
    return results


async def _search_messages(args: MessageSearchArgs, limit: int | None) -> list[MessageMatch]:
    async with get_client() as client:
        return await search_messages_with(client, args.phrase, limit, args.type, args.chat)


def search_messages(args: MessageSearchArgs, ctx: Ctx) -> Page[MessageMatch]:
    """Search inside message content (server-side) across all chats or in one chat."""
    with logging_to(ctx):
        return Page(items=asyncio.run(_search_messages(args, fetch_count(ctx.page))))
