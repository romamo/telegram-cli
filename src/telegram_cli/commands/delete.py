"""chats delete / messages delete / messages cleanup — destructive commands.

The ``*_with`` coroutines take a connected client, so tests can drive them with a fake one.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.types import Channel, ChannelForbidden, User
from treaty import Affects, Arg, Ctx, Exit, Flag, ParseError

from telegram_cli.client import get_client, resolve_entity
from telegram_cli.ids import ChatId, MessageId
from telegram_cli.logs import logging_to
from telegram_cli.running import run_async
from telegram_cli.utils import (
    INPUT_FILE_DESCRIPTION,
    chat_id_field,
    load_items,
    message_id_field,
)

logger = logging.getLogger(__name__)

# MTProto's limit for one DeleteMessages call
_BATCH = 100
# Delete everything up to the largest signed int32; 0 may be read as "no messages"
_ALL_MESSAGES = 0x7FFFFFFF

_DRY_RUN = "Preview what would be deleted; change nothing"
_JUST_ME = (
    "Delete only your copy; the other side keeps theirs. Not available in channels and "
    "supergroups, where deletion is always for everyone"
)


def _affected(results: Any, requested: int) -> int:
    """Messages Telegram deleted, from delete_messages' AffectedMessages list

    ``pts_count`` counts update events, not messages: emptying Saved Messages, for one,
    adds a "history cleared" event. No call deletes more messages than it names, so the
    count is capped at ``requested``.
    """
    events: int = sum(r.pts_count for r in (results if isinstance(results, list) else [results]))
    return min(events, requested)


def _refuse_just_me_in_channels(entities: dict[ChatId, object]) -> None:
    """--just-me cannot work where Telegram deletes for everyone: refuse before deleting"""
    channels = [c for c, e in entities.items() if isinstance(e, Channel | ChannelForbidden)]
    if channels:
        raise Exit.ARG_ERROR(
            "--just-me does not work in channels and supergroups: Telegram deletes their "
            "messages for everyone.",
            context={"flag": "just-me", "chat_ids": [c.value for c in channels]},
            suggestion="Drop --just-me to delete for everyone, or leave those chats out.",
        )


def _scope(entity: object, revoke: bool) -> str:
    """How far a deletion reaches, for a preview summary"""
    if isinstance(entity, User):
        if entity.is_self:
            return ""  # Saved Messages: there is no other side
        return " for both sides" if revoke else " for you only; the other side keeps theirs"
    return " for everyone" if revoke else " for you only; other members keep theirs"


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
    just_me: bool = Flag(default=False, description=_JUST_ME)
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
    revoked: bool = False
    """Messages are (or would be) deleted for everyone in the chat, not only for the user"""


async def clear_history_with(
    client: TelegramClient, entity: object, chat_id: ChatId, revoke: bool = True
) -> int:
    """Delete every message the user may delete in a chat; returns how many were deleted

    ``revoke`` deletes for both sides in private chats and for everyone in basic groups;
    channels and supergroups always delete for everyone.
    """
    if isinstance(entity, User):
        # Private chats: DeleteHistoryRequest clears the history, one batch per call
        side = "both sides" if revoke else "your side only"
        logger.info(f"Clearing all history for private chat {chat_id} ({side})...")
        total = 0
        while True:
            result = await client(
                DeleteHistoryRequest(
                    peer=entity, max_id=_ALL_MESSAGES, just_clear=False, revoke=revoke
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
            total += _affected(
                await client.delete_messages(entity, ids, revoke=revoke), len(ids)
            )
            ids = []
    if ids:
        total += _affected(await client.delete_messages(entity, ids, revoke=revoke), len(ids))
    return total


async def remove_with(
    client: TelegramClient, entity: object, chat_id: ChatId, revoke: bool = True
) -> None:
    if isinstance(entity, User):
        # Delete everything (for both sides unless revoke is off) and drop the dialog entry
        logger.info(f"Removing private chat {chat_id}...")
        await clear_history_with(client, entity, chat_id, revoke)
    else:
        # For channels/groups, delete_dialog leaves them
        logger.info(f"Leaving group/channel {chat_id} and removing from dialog list...")
        await client.delete_dialog(entity)


async def delete_chat_history(ctx: Ctx, chat_id: ChatId) -> int:
    """Clear all messages in a dialog and return how many were deleted.

    Used by `chats review` after a connection that already checked privacy.
    """
    async with get_client(ctx, check_privacy=False) as client:
        return await clear_history_with(client, await resolve_entity(client, chat_id), chat_id)


async def remove_chat(ctx: Ctx, chat_id: ChatId) -> None:
    """Delete the dialog and remove it from the list (leaves a group/channel).

    Used by `chats review` after a connection that already checked privacy.
    """
    async with get_client(ctx, check_privacy=False) as client:
        await remove_with(client, await resolve_entity(client, chat_id), chat_id)


async def delete_chat_with(client: TelegramClient, args: ChatDeleteArgs) -> ChatDeleteResult:
    action: Literal["clear_history", "remove"] = "remove" if args.remove else "clear_history"
    entity = await resolve_entity(client, args.chat)
    if args.just_me:
        _refuse_just_me_in_channels({args.chat: entity})
    name = utils.get_display_name(entity)
    revoke = not args.just_me
    own_only = not args.remove and not isinstance(entity, User) and not _deletes_others(entity)
    # Removing a group or channel only leaves it; a private chat's history is cleared first
    revoked = revoke and (isinstance(entity, User) or not args.remove)
    if args.dry_run:
        if args.remove and isinstance(entity, User):
            summary = (
                f"Removes chat {args.chat} ({name}), deleting its history"
                f"{_scope(entity, revoke)}"
            )
        elif args.remove:
            summary = f"Removes chat {args.chat} ({name})"
        elif own_only:
            summary = (
                f"Deletes your own messages in chat {args.chat} ({name})"
                f"{_scope(entity, revoke)}; you are not an admin"
            )
        else:
            summary = f"Clears all history of chat {args.chat} ({name}){_scope(entity, revoke)}"
        return ChatDeleteResult(
            effect="would_delete",
            chat_id=args.chat,
            name=name,
            action=action,
            own_messages_only=own_only,
            would_affect=Affects(summary, (f"chat/{args.chat}",), 1),
            revoked=revoked,
        )
    if args.remove:
        await remove_with(client, entity, args.chat, revoke)
        return ChatDeleteResult(
            effect="deleted", chat_id=args.chat, name=name, action=action, revoked=revoked
        )
    count = await clear_history_with(client, entity, args.chat, revoke)
    return ChatDeleteResult(
        effect="deleted" if count else "noop",
        chat_id=args.chat,
        name=name,
        action=action,
        deleted_count=count,
        own_messages_only=own_only,
        revoked=revoked,
    )


async def _delete_chat(ctx: Ctx, args: ChatDeleteArgs) -> ChatDeleteResult:
    async with get_client(ctx) as client:
        return await delete_chat_with(client, args)


def delete_chat(args: ChatDeleteArgs, ctx: Ctx) -> ChatDeleteResult:
    """Clear a chat's history, or remove the chat completely with --remove."""
    with logging_to(ctx):
        return run_async(_delete_chat(ctx, args))


# ── messages delete ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MessagesDeleteArgs:
    chat: ChatId | None = Flag(default=None, description="Chat or group ID")
    message: tuple[MessageId, ...] = Flag(
        default=(), description="Message ID to delete, repeatable"
    )
    input_file: Path | None = Flag(default=None, description=INPUT_FILE_DESCRIPTION)
    just_me: bool = Flag(default=False, description=_JUST_ME)
    dry_run: bool = Flag(default=False, description=_DRY_RUN)

    def __post_init__(self) -> None:
        if self.input_file is not None and self.chat is not None:
            raise ParseError(
                "--input-file replaces --chat and --message",
                context={"field": "input-file"},
                suggestion="Pass either --chat with --message, or --input-file.",
            )
        if (self.chat is None) != (not self.message):
            raise ParseError(
                "--chat and --message go together",
                context={"field": "message" if self.chat is not None else "chat"},
                suggestion="Pass both --chat and --message, or neither and give JSON from "
                "`tg messages search` with --input-file or a pipe.",
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
    revoked: bool = False
    """Messages are (or would be) deleted for everyone in their chats, not only for the user"""


type Targets = dict[ChatId, list[MessageId]]


def _targets(args: MessagesDeleteArgs) -> Targets:
    """Message IDs to delete per chat, from the flags or from piped JSON"""
    if args.chat is not None:
        return {args.chat: list(args.message)}
    by_chat: defaultdict[ChatId, list[MessageId]] = defaultdict(list)
    for item in load_items(args.input_file):
        chat_id = chat_id_field(item, "chat_id")
        msg_id = message_id_field(item, "msg_id", "id")
        if chat_id is not None and msg_id is not None:
            by_chat[chat_id].append(msg_id)
    if not by_chat:
        raise Exit.ARG_ERROR(
            "No messages to delete.",
            suggestion="Pass --chat ID --message MSG_ID, or JSON from `tg messages search` with "
            "--input-file or a pipe.",
        )
    return dict(by_chat)


def _preview_text(text: str | None) -> str:
    text = (text or "").replace("\n", " ")
    if not text:
        return "(no text/service message)"
    return text if len(text) <= 50 else text[:47] + "..."


async def delete_messages_with(
    client: TelegramClient, targets: Targets, dry_run: bool, just_me: bool = False
) -> MessagesDeleteResult:
    found: list[MessageRef] = []
    not_found: list[MessageKey] = []
    entities = {chat_id: await resolve_entity(client, chat_id) for chat_id in targets}
    if just_me:
        _refuse_just_me_in_channels(entities)
    revoke = not just_me
    for chat_id, ids in targets.items():
        entity = entities[chat_id]
        # Missing messages come back as None, in request order
        messages = await client.get_messages(entity, ids=[i.value for i in ids])
        for msg_id, msg in zip(ids, messages, strict=True):
            if msg:
                found.append(MessageRef(chat_id, msg_id, _preview_text(msg.message)))
            else:
                not_found.append(MessageKey(chat_id, msg_id))

    chats = sorted({m.chat_id for m in found})
    if dry_run:
        if not found:
            summary = "Deletes nothing: none of the messages exist"
        elif not revoke:
            summary = (
                f"Deletes {len(found)} message(s) from {len(chats)} chat(s) for you only; "
                "the other side keeps theirs"
            )
        else:
            summary = (
                f"Deletes {len(found)} message(s) from {len(chats)} chat(s) for everyone "
                "(for both sides in private chats)"
            )
        affects = Affects(
            summary,
            tuple(f"chat/{m.chat_id}/message/{m.msg_id}" for m in found),
            len(found),
        )
        return MessagesDeleteResult(
            "would_delete", tuple(found), tuple(not_found), None, affects, revoked=revoke
        )

    deleted = 0
    for chat_id in chats:
        raw_ids = [m.msg_id.value for m in found if m.chat_id == chat_id]
        logger.info(f"Deleting {len(raw_ids)} messages from {chat_id}...")
        deleted += _affected(
            await client.delete_messages(entities[chat_id], raw_ids, revoke=revoke),
            len(raw_ids),
        )
    effect: Literal["deleted", "noop"] = "deleted" if deleted else "noop"
    return MessagesDeleteResult(effect, tuple(found), tuple(not_found), deleted, revoked=revoke)


async def _delete_messages(
    ctx: Ctx, targets: Targets, dry_run: bool, just_me: bool
) -> MessagesDeleteResult:
    async with get_client(ctx) as client:
        return await delete_messages_with(client, targets, dry_run, just_me)


def delete_messages(args: MessagesDeleteArgs, ctx: Ctx) -> MessagesDeleteResult:
    """Delete specific messages by ID, or the messages piped in from `tg messages search`."""
    with logging_to(ctx):
        result = run_async(
            _delete_messages(ctx, _targets(args), args.dry_run, args.just_me)
        )
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
    include_others: bool = Flag(
        default=False,
        description="Also delete other people's matching messages where Telegram allows it "
        "(for both sides in private chats); by default only your own messages go",
    )
    scan_limit: int = Flag(
        default=10000,
        description="Most search hits read, yours and others', before stopping; 0 reads them all",
    )
    dry_run: bool = Flag(default=False, description=_DRY_RUN)

    def __post_init__(self) -> None:
        if self.max_matches < 1:
            raise ParseError("--max-matches must be at least 1", context={"flag": "max-matches"})
        if self.scan_limit < 0:
            raise ParseError(
                "--scan-limit cannot be negative",
                context={"flag": "scan-limit"},
                suggestion="Pass 0 to read every search hit.",
            )


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
    """Matches in scope: your own messages, or everyone's with --include-others"""
    limit_reached: bool
    """--max-matches was reached: more matches may remain, so run the cleanup again"""
    chats: tuple[ChatDeletion, ...]
    deleted_count: int | None = None
    would_affect: Affects | None = None
    include_others: bool = False
    """Other people's matches are deleted too; otherwise only the user's own messages"""
    skipped_others: int = 0
    """Matches other people sent, left in place because --include-others was not given"""
    scan_capped: bool = False
    """--scan-limit stopped the search before --max-matches: later hits were not checked"""


async def cleanup_with(
    client: TelegramClient,
    phrase: str,
    max_matches: int,
    dry_run: bool,
    include_others: bool = False,
    scan_limit: int = 0,
) -> CleanupResult:
    """``scan_limit`` caps the search hits read, in scope or not; 0 reads them all"""
    logger.info(f"Searching for matches for '{phrase}' across all chats...")
    by_chat: defaultdict[ChatId, list[int]] = defaultdict(list)
    peers: dict[ChatId, object] = {}
    matched = skipped = scanned = 0
    limit_reached = scan_capped = False
    # Telegram ignores sender filters in private chats, so check each hit's ``out`` here and
    # keep reading until max_matches messages in scope are found, the results end, or
    # scan_limit hits were read
    async for msg in client.iter_messages(None, search=phrase):
        if scan_limit and scanned >= scan_limit:
            scan_capped = True  # one more hit exists past the cap
            break
        scanned += 1
        if not msg.peer_id:
            continue
        if not (include_others or msg.out):
            skipped += 1
            continue
        chat_id = ChatId(utils.get_peer_id(msg.peer_id))
        peers[chat_id] = msg.peer_id
        by_chat[chat_id].append(msg.id)
        matched += 1
        if matched >= max_matches:
            limit_reached = True
            break

    scope = "message(s)" if include_others else "of your own message(s)"
    if dry_run:
        summary = f"Deletes {matched} {scope} matching {phrase!r} from {len(by_chat)} chat(s)"
        if include_others:
            summary += ", including other people's (for both sides in private chats)"
        elif skipped:
            summary += f"; keeps {skipped} sent by others"
        if scan_capped:
            summary += f"; stopped after reading {scan_limit} search hits"
        affects = Affects(summary, tuple(f"chat/{c}" for c in by_chat), matched)
        planned = tuple(ChatDeletion(c, len(ids)) for c, ids in by_chat.items())
        return CleanupResult(
            "would_delete", phrase, matched, limit_reached, planned, None, affects,
            include_others=include_others, skipped_others=skipped, scan_capped=scan_capped,
        )

    done: list[ChatDeletion] = []
    for chat_id, msg_ids in by_chat.items():
        logger.info(f"Deleting {len(msg_ids)} messages from {chat_id}...")
        try:
            deleted = _affected(
                await client.delete_messages(peers[chat_id], msg_ids), len(msg_ids)
            )
        except FloodWaitError:
            raise  # stop everything; get_client turns it into RATE_LIMITED
        except RPCError as exc:
            done.append(ChatDeletion(chat_id, len(msg_ids), error=type(exc).__name__))
            continue
        done.append(ChatDeletion(chat_id, len(msg_ids), deleted))

    total = sum(d.deleted or 0 for d in done)
    effect: Literal["deleted", "noop"] = "deleted" if total else "noop"
    result = CleanupResult(
        effect, phrase, matched, limit_reached, tuple(done), total,
        include_others=include_others, skipped_others=skipped, scan_capped=scan_capped,
    )
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


async def _cleanup(ctx: Ctx, args: CleanupArgs) -> CleanupResult:
    async with get_client(ctx) as client:
        return await cleanup_with(
            client,
            args.phrase,
            args.max_matches,
            args.dry_run,
            args.include_others,
            args.scan_limit,
        )


def cleanup(args: CleanupArgs, ctx: Ctx) -> CleanupResult:
    """Find your messages containing a phrase across all chats and delete them."""
    with logging_to(ctx):
        result = run_async(_cleanup(ctx, args))
        if result.limit_reached:
            ctx.warn(
                "MATCH_LIMIT_REACHED",
                f"Stopped at {args.max_matches} matches; more may remain.",
                max_matches=args.max_matches,
            )
        if result.scan_capped:
            ctx.warn(
                "SCAN_LIMIT_REACHED",
                f"Stopped after reading {args.scan_limit} search hits; later matches were not "
                "checked.",
                scan_limit=args.scan_limit,
                suggestion="Raise --scan-limit, or pass --scan-limit 0 to read every search hit.",
            )
        return result
