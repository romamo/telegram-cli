"""stats — account summary and dialog aggregates."""
from __future__ import annotations

from dataclasses import dataclass

from treaty import Ctx, NoArgs

from telegram_cli.chat_type import ChatType, dialog_type
from telegram_cli.client import get_client
from telegram_cli.logs import logging_to
from telegram_cli.running import run_async


@dataclass(frozen=True, slots=True)
class Stats:
    name: str
    username: str | None
    premium: bool
    dc: int | None
    total_dialogs: int
    users: int
    groups: int
    channels: int
    total_unread: int
    with_unread: int
    muted: int


async def _fetch_stats(ctx: Ctx) -> Stats:
    async with get_client(ctx) as client:
        me = await client.get_me()
        # Fetch ALL dialogs (paged automatically by Telethon)
        dialogs = await client.get_dialogs(limit=None)
        dc = client.session.dc_id

    counts = dict.fromkeys(ChatType, 0)
    total_unread = with_unread = muted = 0
    for d in dialogs:
        counts[dialog_type(d.entity)] += 1
        total_unread += d.unread_count
        if d.unread_count > 0:
            with_unread += 1
        notify = getattr(d.dialog, "notify_settings", None)
        if getattr(notify, "mute_until", None) is not None:
            muted += 1

    return Stats(
        name=f"{me.first_name or ''} {me.last_name or ''}".strip(),
        username=f"@{me.username}" if me.username else None,
        premium=bool(getattr(me, "premium", False)),
        dc=dc,
        total_dialogs=len(dialogs),
        users=counts[ChatType.USER],
        groups=counts[ChatType.GROUP],
        channels=counts[ChatType.CHANNEL],
        total_unread=total_unread,
        with_unread=with_unread,
        muted=muted,
    )


def stats(args: NoArgs, ctx: Ctx) -> Stats:
    """Show account info and dialog statistics.

    Fetches all dialogs to compute aggregates, so it may take a while with many chats.
    """
    with logging_to(ctx):
        return run_async(_fetch_stats(ctx))
