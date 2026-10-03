"""chats list / review — fetch Telegram dialogs and review them interactively."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import IO, Any, Literal

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, User
from treaty import Ctx, Exit, Flag, Page, ParseError

from telegram_cli.client import get_client
from telegram_cli.ids import ChatId
from telegram_cli.utils import chat_id_field, fetch_count, read_piped_items

logger = logging.getLogger(__name__)

# Dialogs scanned at most when filtering client-side
_SCAN_LIMIT = 3000


class ChatType(StrEnum):
    USER = "user"
    GROUP = "group"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


def dialog_type(entity: object) -> ChatType:
    if isinstance(entity, User):
        return ChatType.USER
    if isinstance(entity, Channel):
        return ChatType.CHANNEL if entity.broadcast else ChatType.GROUP
    if isinstance(entity, Chat):
        return ChatType.GROUP
    return ChatType.UNKNOWN


def _age(dt: datetime | None) -> str:
    """Return a compact human-readable age string, e.g. '3m', '2h', '5d', '3w', '8mo', '2y'."""
    if dt is None:
        return ""
    now = datetime.now(tz=UTC)
    # Ensure tz-aware comparison
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    seconds = int((now - dt).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks}w"
    months = days // 30
    if months < 12:
        return f"{months}mo"
    return f"{days // 365}y"


# ── chats list ────────────────────────────────────────────────────────────────

SCAN_LIMIT_DESCRIPTION = "Most dialogs scanned when filtering client-side; 0 scans them all"


def check_scan_limit(scan_limit: int) -> None:
    if scan_limit < 0:
        raise ParseError(
            "--scan-limit cannot be negative",
            context={"flag": "scan-limit"},
            suggestion="Pass 0 to scan every dialog.",
        )


def warn_scan_capped(ctx: Ctx, scan_limit: int) -> None:
    ctx.warn(
        "SCAN_LIMIT_REACHED",
        f"Stopped after scanning {scan_limit} dialogs; older chats were not checked.",
        scan_limit=scan_limit,
        suggestion="Raise --scan-limit, or pass --scan-limit 0 to scan every dialog.",
    )


@dataclass(frozen=True, slots=True)
class ListChatsArgs:
    type: ChatType | None = Flag(default=None, description="Filter by dialog type")
    query: str | None = Flag(
        default=None, short="q", description="Filter chats by name (case-insensitive)"
    )
    reverse: bool = Flag(default=False, description="Show oldest chats first")
    year: int | None = Flag(
        default=None, description="Only chats last active in this year, e.g. 2020"
    )
    scan_limit: int = Flag(default=_SCAN_LIMIT, description=SCAN_LIMIT_DESCRIPTION)

    def __post_init__(self) -> None:
        check_scan_limit(self.scan_limit)


@dataclass(frozen=True, slots=True)
class ChatRow:
    id: ChatId
    type: ChatType
    name: str
    unread: int
    age: str
    last_message: str


@dataclass(frozen=True, slots=True)
class DialogScan:
    rows: list[ChatRow]
    capped: bool
    """The scan stopped at the scan limit, so matching chats may have been missed"""


async def _to_async_iter[T](iterable: Iterable[T]) -> AsyncIterator[T]:
    """Treat a reversed list like an async iterator for a unified loop."""
    for item in iterable:
        yield item


async def fetch_dialogs_with(
    client: TelegramClient, limit: int | None, args: ListChatsArgs
) -> DialogScan:
    # If year is provided, jump to the end of that year using offset_date and scan backwards
    year = args.year
    offset_date = datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC) if year else None
    cap = args.scan_limit or None

    if args.reverse:
        dialogs = await client.get_dialogs(limit=cap)
        dialog_iter: AsyncIterator[Any] = _to_async_iter(reversed(dialogs))
        capped = cap is not None and len(dialogs) >= cap
    else:
        dialog_iter = client.iter_dialogs(offset_date=offset_date)
        capped = False

    rows: list[ChatRow] = []
    logger.debug(f"Fetching dialogs with scan_limit={cap}, offset_date={offset_date}")
    fetched_count = 0
    async for d in dialog_iter:
        fetched_count += 1
        if not args.reverse and cap is not None and fetched_count > cap:
            capped = True
            break

        msg_date: datetime | None = getattr(d.message, "date", None) if d.message else None

        # Year filter: offset_date already skipped everything newer, so stop at the
        # first chat older than the year
        if year and msg_date:
            if msg_date.year < year:
                capped = False
                break
            if msg_date.year > year:
                continue

        dtype = dialog_type(d.entity)
        if args.type and dtype != args.type:
            continue

        name = d.name or ""
        if args.query and args.query.lower() not in name.lower():
            continue

        last_msg = ""
        if d.message and getattr(d.message, "message", None):
            last_msg = d.message.message[:80].replace("\n", " ")

        rows.append(
            ChatRow(
                id=ChatId(d.id),
                type=dtype,
                name=d.name or "(no name)",
                unread=d.unread_count,
                age=_age(msg_date),
                last_message=last_msg,
            )
        )
        if limit is not None and len(rows) >= limit:
            # Enough rows: chats past this point were not needed, not missed
            return DialogScan(rows, capped=False)

    return DialogScan(rows, capped)


async def _fetch_dialogs(limit: int | None, args: ListChatsArgs) -> DialogScan:
    async with get_client() as client:
        return await fetch_dialogs_with(client, limit, args)


def list_chats(args: ListChatsArgs, ctx: Ctx) -> Page[ChatRow]:
    """List Telegram dialogs (DMs, groups, channels).

    Telegram's API has no server-side filter for type or name, so filters scan up to
    --scan-limit dialogs client-side.
    """
    scan = asyncio.run(_fetch_dialogs(fetch_count(ctx.page), args))
    if scan.capped:
        warn_scan_capped(ctx, args.scan_limit)
    return Page(items=scan.rows)


# ── chats review ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReviewArgs:
    ids: tuple[ChatId, ...] = Flag(
        default=(),
        description="Chat ID to review, repeatable; read from piped JSON when omitted",
    )


type ReviewAction = Literal["delete", "remove", "skip", "quit"]


@dataclass(frozen=True, slots=True)
class ReviewedChat:
    chat_id: ChatId
    action: Literal["deleted_history", "removed", "skipped"]


@dataclass(frozen=True, slots=True)
class ReviewResult:
    effect: Literal["deleted", "noop"]
    reviewed: tuple[ReviewedChat, ...]


_CHOICES: dict[str, ReviewAction] = {
    "d": "delete",
    "delete": "delete",
    "r": "remove",
    "remove": "remove",
    "s": "skip",
    "skip": "skip",
    "": "skip",
    "q": "quit",
    "quit": "quit",
}


@contextmanager
def _tty() -> Iterator[tuple[IO[str], IO[str]]]:
    """The controlling terminal, which stays reachable while stdin carries piped JSON"""
    try:
        tty_in = open("/dev/tty")  # noqa: SIM115 - closed below
        tty_out = open("/dev/tty", "w")  # noqa: SIM115 - closed below
    except OSError as exc:
        raise Exit.PRECONDITION(
            "Chat review asks a person what to do with each chat, and no terminal is attached.",
            code="INPUT_REQUIRED",
            suggestion="Run it in a terminal, or use `tg chats delete --id ID` instead.",
        ) from exc
    with tty_in, tty_out:
        yield tty_in, tty_out


async def _review_chat(
    chat_id: ChatId, match_text: str | None, tty_in: IO[str], tty_out: IO[str]
) -> ReviewAction:
    """Show the search match and the last 10 messages, then ask what to do."""
    from telegram_cli.commands.export import export_stream

    history = [msg async for msg in export_stream(chat_id, limit=10, since=None)]

    rule = "=" * 60
    lines = ["", rule, f"CHAT REVIEW: {chat_id}"]
    if match_text:
        lines += [f"🔍 SEARCH MATCH: {match_text}", "-" * 60]
    lines.append("LAST 10 MESSAGES:")
    if not history:
        lines.append("  (Empty history)")
    for m in reversed(history):  # Oldest to newest
        dt = m.date.strftime("%Y-%m-%d %H:%M:%S") if m.date else ""
        lines.append(f"  [{dt}] {m.sender or 'Unknown'}: {m.text.strip()}")
    lines.append(rule)
    tty_out.write("\n".join(lines) + "\n")

    while True:
        tty_out.write("Action: [d]elete history, [r]emove chat/leave, [s]kip, [q]uit: ")
        tty_out.flush()
        choice = tty_in.readline().strip().lower()
        if choice in _CHOICES:
            return _CHOICES[choice]


def review_chats(args: ReviewArgs, ctx: Ctx) -> ReviewResult:
    """Interactively review chats and delete their history or remove them."""
    from telegram_cli.commands.delete import delete_chat_history, remove_chat

    # chat_id -> the text snippet of a piped message search hit, if any
    to_review: dict[ChatId, str | None] = dict.fromkeys(args.ids)
    if not to_review:
        for item in read_piped_items():
            chat_id = chat_id_field(item, "chat_id", "id")
            if chat_id is None:
                continue
            text = item.get("text")
            snippet = text if isinstance(text, str) and text else None
            if to_review.get(chat_id) is None:
                to_review[chat_id] = snippet

    if not to_review:
        raise Exit.ARG_ERROR(
            "No chats to review.",
            suggestion="Pass --ids, or pipe JSON from `tg chats list` or `tg messages search`.",
        )

    ctx.log("Starting interactive review", chats=len(to_review))
    reviewed: list[ReviewedChat] = []
    with _tty() as (tty_in, tty_out):
        for chat_id, match_text in to_review.items():
            action = asyncio.run(_review_chat(chat_id, match_text, tty_in, tty_out))
            if action == "quit":
                break
            if action == "delete":
                asyncio.run(delete_chat_history(chat_id))
                reviewed.append(ReviewedChat(chat_id, "deleted_history"))
            elif action == "remove":
                asyncio.run(remove_chat(chat_id))
                reviewed.append(ReviewedChat(chat_id, "removed"))
            else:
                reviewed.append(ReviewedChat(chat_id, "skipped"))

    changed = any(r.action != "skipped" for r in reviewed)
    return ReviewResult(effect="deleted" if changed else "noop", reviewed=tuple(reviewed))
