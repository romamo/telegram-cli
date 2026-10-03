"""chats delete / messages delete / messages cleanup — destructive commands.

The ``*_with`` coroutines take a connected client, so tests can drive them with a fake one.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.types import User
from treaty import Affects, Arg, Ctx, Exit, Flag, ParseError

from telegram_cli.client import get_client, resolve_entity
from telegram_cli.ids import ChatId, MessageId
from telegram_cli.utils import chat_id_field, message_id_field, read_piped_items

logger = logging.getLogger(__name__)

# MTProto's limit for one DeleteMessages call
_BATCH = 100
# Delete everything up to the largest signed int32; 0 may be read as "no messages"
_ALL_MESSAGES = 0x7FFFFFFF

_DRY_RUN = "Preview what would be deleted; change nothing"


def _affected(results: Any) -> int:
    """Messages Telegram actually deleted, from delete_messages' AffectedMessages list"""
    return sum(r.pts_count for r in (results if isinstance(results, list) else [results]))


def _deletes_others(entity: object) -> bool:
    """Whether the user may delete other members' messages in a group or channel"""
    if getattr(entity, "creator", False):
        return True
    rights = getattr(entity, "admin_rights", None)
    return bool(rights is not None and rights.delete_messages)


# ── chats delete ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChatDeleteArgs:
    chat: ChatId = Flag(description="Chat or group ID (from `tg chats list`)")
    remove: bool = Flag(
        default=False, description="Remove the dialog completely (leave the group/channel)"
    )
    dry_run: bool = Flag(default=False, description=_DRY_RUN)


@dataclass(frozen=True, slots=True)
class ChatDeleteResult:
    effect: Literal["deleted", "noop", "would_delete"]
    chat_id: ChatId
    name: str
    action: Literal["clear_history", "remove"]
    deleted_count: int | None = None
    """Messages Telegram reports as deleted; None for --remove and dry runs"""
    own_messages_only: bool = False
    """Not an admin of the group: only the user's own messages are deleted"""
    would_affect: Affects | None = None


async def clear_history_with(client: TelegramClient, entity: object, chat_id: ChatId) -> int:
    """Delete every message the user may delete in a chat; returns how many were deleted"""
    if isinstance(entity, User):
        # Private chats: DeleteHistoryRequest clears both sides, one batch per call
        logger.info(f"Clearing all history for private chat {chat_id} (double-sided)...")
        total = 0
        while True:
            result = await client(
                DeleteHistoryRequest(
                    peer=entity, max_id=_ALL_MESSAGES, just_clear=False, revoke=True
                )
            )
            total += int(result.pts_count)
            if result.offset <= 0:
                return total

    # Groups/channels: iterate and delete in batches; non-admins can only delete their own
    from_user = None if _deletes_others(entity) else "me"
    logger.info(f"Scanning and deleting messages from group/channel {chat_id}...")
    total = 0
    ids: list[int] = []
    async for msg in client.iter_messages(entity, from_user=from_user):
        ids.append(msg.id)
        if len(ids) >= _BATCH:
            total += _affected(await client.delete_messages(entity, ids))
            ids = []
    if ids:
        total += _affected(await client.delete_messages(entity, ids))
    return total


async def remove_with(client: TelegramClient, entity: object, chat_id: ChatId) -> None:
    if isinstance(entity, User):
        # Delete everything for both sides and drop the dialog entry
        logger.info(f"Removing private chat {chat_id} and revoking for both sides...")
        await clear_history_with(client, entity, chat_id)
    else:
        # For channels/groups, delete_dialog leaves them
        logger.info(f"Leaving group/channel {chat_id} and removing from dialog list...")
        await client.delete_dialog(entity)


async def delete_chat_history(chat_id: ChatId) -> int:
    """Clear all messages in a dialog and return how many were deleted."""
    async with get_client() as client:
        return await clear_history_with(client, await resolve_entity(client, chat_id), chat_id)


async def remove_chat(chat_id: ChatId) -> None:
    """Delete the dialog and remove it from the list (leaves a group/channel)."""
    async with get_client() as client:
        await remove_with(client, await resolve_entity(client, chat_id), chat_id)


async def delete_chat_with(client: TelegramClient, args: ChatDeleteArgs) -> ChatDeleteResult:
    action: Literal["clear_history", "remove"] = "remove" if args.remove else "clear_history"
    entity = await resolve_entity(client, args.chat)
    name = utils.get_display_name(entity)
    own_only = not args.remove and not isinstance(entity, User) and not _deletes_others(entity)
    if args.dry_run:
        if args.remove:
            summary = f"Removes chat {args.chat} ({name})"
        elif own_only:
            summary = (
                f"Deletes your own messages in chat {args.chat} ({name}); you are not an admin"
            )
        else:
            summary = f"Clears all history of chat {args.chat} ({name})"
        return ChatDeleteResult(
            effect="would_delete",
            chat_id=args.chat,
            name=name,
            action=action,
            own_messages_only=own_only,
            would_affect=Affects(summary, (f"chat/{args.chat}",), 1),
        )
    if args.remove:
        await remove_with(client, entity, args.chat)
        return ChatDeleteResult(effect="deleted", chat_id=args.chat, name=name, action=action)
    count = await clear_history_with(client, entity, args.chat)
    return ChatDeleteResult(
        effect="deleted" if count else "noop",
        chat_id=args.chat,
        name=name,
        action=action,
        deleted_count=count,
        own_messages_only=own_only,
    )


async def _delete_chat(args: ChatDeleteArgs) -> ChatDeleteResult:
    async with get_client() as client:
        return await delete_chat_with(client, args)


def delete_chat(args: ChatDeleteArgs, ctx: Ctx) -> ChatDeleteResult:
    """Clear a chat's history, or remove the chat completely with --remove."""
    return asyncio.run(_delete_chat(args))


# ── messages delete ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MessagesDeleteArgs:
    chat: ChatId | None = Flag(default=None, description="Chat or group ID")
    message: tuple[MessageId, ...] = Flag(
        default=(), description="Message ID to delete, repeatable"
    )
    dry_run: bool = Flag(default=False, description=_DRY_RUN)

    def __post_init__(self) -> None:
        if (self.chat is None) != (not self.message):
            raise ParseError(
                "--chat and --message go together",
                context={"field": "message" if self.chat is not None else "chat"},
                suggestion="Pass both --chat and --message, or neither and pipe JSON from "
                "`tg messages search`.",
            )


@dataclass(frozen=True, slots=True)
class MessageKey:
    chat_id: ChatId
    msg_id: MessageId


@dataclass(frozen=True, slots=True)
class MessageRef:
    chat_id: ChatId
    msg_id: MessageId
    text: str


@dataclass(frozen=True, slots=True)
class MessagesDeleteResult:
    effect: Literal["deleted", "noop", "would_delete"]
    messages: tuple[MessageRef, ...]
    """The requested messages that exist"""
    not_found: tuple[MessageKey, ...]
    """Requested messages that do not exist (already deleted, or a wrong ID)"""
    deleted_count: int | None = None
    """Messages Telegram reports as deleted; None on a dry run"""
    would_affect: Affects | None = None


type Targets = dict[ChatId, list[MessageId]]


def _targets(args: MessagesDeleteArgs) -> Targets:
    """Message IDs to delete per chat, from the flags or from piped JSON"""
    if args.chat is not None:
        return {args.chat: list(args.message)}
    by_chat: defaultdict[ChatId, list[MessageId]] = defaultdict(list)
    for item in read_piped_items():
        chat_id = chat_id_field(item, "chat_id")
        msg_id = message_id_field(item, "msg_id", "id")
        if chat_id is not None and msg_id is not None:
            by_chat[chat_id].append(msg_id)
    if not by_chat:
        raise Exit.ARG_ERROR(
            "No messages to delete.",
            suggestion="Pass --chat ID --message MSG_ID, or pipe JSON from `tg messages search`.",
        )
    return dict(by_chat)


def _preview_text(text: str | None) -> str:
    text = (text or "").replace("\n", " ")
    if not text:
        return "(no text/service message)"
    return text if len(text) <= 50 else text[:47] + "..."


async def delete_messages_with(
    client: TelegramClient, targets: Targets, dry_run: bool
) -> MessagesDeleteResult:
    found: list[MessageRef] = []
    not_found: list[MessageKey] = []
    entities: dict[ChatId, object] = {}
    for chat_id, ids in targets.items():
        entities[chat_id] = entity = await resolve_entity(client, chat_id)
        # Missing messages come back as None, in request order
        messages = await client.get_messages(entity, ids=[i.value for i in ids])
        for msg_id, msg in zip(ids, messages, strict=True):
            if msg:
                found.append(MessageRef(chat_id, msg_id, _preview_text(msg.message)))
            else:
                not_found.append(MessageKey(chat_id, msg_id))

    chats = sorted({m.chat_id for m in found})
    if dry_run:
        summary = (
            f"Deletes {len(found)} message(s) from {len(chats)} chat(s)"
            if found
            else "Deletes nothing: none of the messages exist"
        )
        affects = Affects(
            summary,
            tuple(f"chat/{m.chat_id}/message/{m.msg_id}" for m in found),
            len(found),
        )
        return MessagesDeleteResult("would_delete", tuple(found), tuple(not_found), None, affects)

    deleted = 0
    for chat_id in chats:
        raw_ids = [m.msg_id.value for m in found if m.chat_id == chat_id]
        logger.info(f"Deleting {len(raw_ids)} messages from {chat_id}...")
        deleted += _affected(await client.delete_messages(entities[chat_id], raw_ids))
    effect: Literal["deleted", "noop"] = "deleted" if deleted else "noop"
    return MessagesDeleteResult(effect, tuple(found), tuple(not_found), deleted)


async def _delete_messages(targets: Targets, dry_run: bool) -> MessagesDeleteResult:
    async with get_client() as client:
        return await delete_messages_with(client, targets, dry_run)


def delete_messages(args: MessagesDeleteArgs, ctx: Ctx) -> MessagesDeleteResult:
    """Delete specific messages by ID, or the messages piped in from `tg messages search`."""
    result = asyncio.run(_delete_messages(_targets(args), args.dry_run))
    if result.not_found:
        ctx.warn(
            "MESSAGES_NOT_FOUND",
            f"{len(result.not_found)} message(s) do not exist; they may already be deleted.",
            count=len(result.not_found),
        )
    return result


# ── messages cleanup ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CleanupArgs:
    phrase: str = Arg(description="Phrase to search for across all chats")
    max_matches: int = Flag(default=1000, description="Most matching messages one run deletes")
    dry_run: bool = Flag(default=False, description=_DRY_RUN)

    def __post_init__(self) -> None:
        if self.max_matches < 1:
            raise ParseError("--max-matches must be at least 1", context={"flag": "max-matches"})


@dataclass(frozen=True, slots=True)
class ChatDeletion:
    chat_id: ChatId
    matched: int
    deleted: int | None = None
    """Messages Telegram reports as deleted; None on a dry run or when the chat failed"""
    error: str | None = None
    """The Telegram error that stopped deletion in this chat"""


@dataclass(frozen=True, slots=True)
class CleanupResult:
    effect: Literal["deleted", "noop", "would_delete"]
    phrase: str
    matched: int
    limit_reached: bool
    """--max-matches was reached: more matches may remain, so run the cleanup again"""
    chats: tuple[ChatDeletion, ...]
    deleted_count: int | None = None
    would_affect: Affects | None = None


async def cleanup_with(
    client: TelegramClient, phrase: str, max_matches: int, dry_run: bool
) -> CleanupResult:
    logger.info(f"Searching for matches for '{phrase}' across all chats...")
    messages = await client.get_messages(None, search=phrase, limit=max_matches)
    limit_reached = len(messages) >= max_matches

    by_chat: defaultdict[ChatId, list[int]] = defaultdict(list)
    peers: dict[ChatId, object] = {}
    for msg in messages:
        if msg.peer_id:
            chat_id = ChatId(utils.get_peer_id(msg.peer_id))
            peers[chat_id] = msg.peer_id
            by_chat[chat_id].append(msg.id)
    matched = sum(len(ids) for ids in by_chat.values())

    if dry_run:
        affects = Affects(
            f"Deletes {matched} message(s) matching {phrase!r} from {len(by_chat)} chat(s)",
            tuple(f"chat/{c}" for c in by_chat),
            matched,
        )
        planned = tuple(ChatDeletion(c, len(ids)) for c, ids in by_chat.items())
        return CleanupResult(
            "would_delete", phrase, matched, limit_reached, planned, None, affects
        )

    done: list[ChatDeletion] = []
    for chat_id, msg_ids in by_chat.items():
        logger.info(f"Deleting {len(msg_ids)} messages from {chat_id}...")
        try:
            deleted = _affected(await client.delete_messages(peers[chat_id], msg_ids))
        except FloodWaitError:
            raise  # stop everything; get_client turns it into RATE_LIMITED
        except RPCError as exc:
            done.append(ChatDeletion(chat_id, len(msg_ids), error=type(exc).__name__))
            continue
        done.append(ChatDeletion(chat_id, len(msg_ids), deleted))

    total = sum(d.deleted or 0 for d in done)
    effect: Literal["deleted", "noop"] = "deleted" if total else "noop"
    result = CleanupResult(effect, phrase, matched, limit_reached, tuple(done), total)
    failed = [d for d in done if d.error is not None]
    if failed:
        raise Exit.PARTIAL_FAILURE(
            f"Deleting failed in {len(failed)} of {len(done)} chat(s); {total} message(s) "
            "were deleted.",
            context={"failed_chat_ids": [d.chat_id.value for d in failed]},
            suggestion="Check `data.chats` for each chat's error; usually you lack admin "
            "rights there.",
            data=result,
        )
    return result


async def _cleanup(args: CleanupArgs) -> CleanupResult:
    async with get_client() as client:
        return await cleanup_with(client, args.phrase, args.max_matches, args.dry_run)


def cleanup(args: CleanupArgs, ctx: Ctx) -> CleanupResult:
    """Find messages containing a phrase across all chats and delete them."""
    result = asyncio.run(_cleanup(args))
    if result.limit_reached:
        ctx.warn(
            "MATCH_LIMIT_REACHED",
            f"Stopped at {args.max_matches} matches; more may remain.",
            max_matches=args.max_matches,
        )
    return result
