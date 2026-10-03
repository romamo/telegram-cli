"""folders — manage Telegram Dialog Filters (Folders)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from telethon import TelegramClient
from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
from telethon.tl.types import DialogFilter, TextWithEntities
from treaty import Affects, Arg, Ctx, Exit, Flag, NoArgs, ParseError

from telegram_cli.client import get_client
from telegram_cli.ids import ChatId
from telegram_cli.logs import logging_to
from telegram_cli.utils import INPUT_FILE_DESCRIPTION, chat_id_field, load_items

logger = logging.getLogger(__name__)

# Folder IDs 0 and 1 are reserved by Telegram
_FIRST_FOLDER_ID = 2
_LAST_FOLDER_ID = 100


def _title(f: DialogFilter) -> str:
    return str(getattr(f.title, "text", f.title))


async def _get_folders(client: TelegramClient) -> list[DialogFilter]:
    """Fetch all dialog filters (folders) from Telegram."""
    response = await client(GetDialogFiltersRequest())
    return [f for f in response.filters if isinstance(f, DialogFilter)]


async def _find_folder(client: TelegramClient, identifier: str) -> DialogFilter | None:
    for f in await _get_folders(client):
        if str(f.id) == identifier or _title(f).lower() == identifier.lower():
            return f
    return None


def _not_found(identifier: str) -> Exception:
    return Exit.NOT_FOUND(
        f"Folder {identifier!r} was not found.",
        context={"folder": identifier},
        suggestion="Pick a name or ID from `tg folders list`.",
    )


# ── folders list ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FolderInfo:
    id: int
    title: str
    pinned: int
    included: int
    excluded: int


async def _list_folders(ctx: Ctx) -> list[FolderInfo]:
    async with get_client(ctx) as client:
        filters = await _get_folders(client)
    return [
        FolderInfo(
            id=f.id,
            title=_title(f),
            pinned=len(f.pinned_peers),
            included=len(f.include_peers),
            excluded=len(f.exclude_peers),
        )
        for f in filters
    ]


def list_folders(args: NoArgs, ctx: Ctx) -> list[FolderInfo]:
    """List all your Telegram folders."""
    with logging_to(ctx):
        return asyncio.run(_list_folders(ctx))


# ── folders create / add / remove ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FolderNameArgs:
    name: str = Arg(description="Name of the new folder")


@dataclass(frozen=True, slots=True)
class FolderPeersArgs:
    folder: str = Arg(description="Folder name or ID")
    chat: tuple[ChatId, ...] = Flag(
        default=(), description="Chat ID, repeatable; read from piped JSON when omitted"
    )
    input_file: Path | None = Flag(default=None, description=INPUT_FILE_DESCRIPTION)

    def __post_init__(self) -> None:
        if self.chat and self.input_file is not None:
            raise ParseError(
                "--input-file replaces --chat",
                context={"field": "input-file"},
                suggestion="Pass either --chat or --input-file.",
            )


@dataclass(frozen=True, slots=True)
class FolderResult:
    effect: Literal["created", "updated", "noop"]
    id: int
    title: str
    included: int


async def _create_folder_internal(
    client: TelegramClient, name: str, include_peers: list[Any] | None = None
) -> DialogFilter:
    filters = await _get_folders(client)
    for f in filters:
        if _title(f) == name:
            raise Exit.CONFLICT(
                f"Folder {name!r} already exists with ID {f.id}.",
                context={"folder": name, "id": f.id},
            )

    existing_ids = {f.id for f in filters}
    new_id = next(
        (i for i in range(_FIRST_FOLDER_ID, _LAST_FOLDER_ID + 1) if i not in existing_ids), None
    )
    if new_id is None:
        raise Exit.PRECONDITION(
            "No free folder ID is left.", suggestion="Delete a folder with `tg folders delete`."
        )

    logger.info(f"Creating folder '{name}' with ID {new_id}...")
    new_filter = DialogFilter(
        id=new_id,
        title=TextWithEntities(text=name, entities=[]),
        pinned_peers=[],
        include_peers=include_peers or [],
        exclude_peers=[],
        # Telegram refuses an empty folder: include contacts until chats are added
        contacts=not include_peers,
        non_contacts=False,
    )
    await client(UpdateDialogFilterRequest(id=new_id, filter=new_filter))
    return new_filter


async def _create_folder(ctx: Ctx, name: str) -> FolderResult:
    async with get_client(ctx) as client:
        created = await _create_folder_internal(client, name)
    return FolderResult("created", created.id, name, len(created.include_peers))


def create_folder(args: FolderNameArgs, ctx: Ctx) -> FolderResult:
    """Create a new empty folder."""
    with logging_to(ctx):
        return asyncio.run(_create_folder(ctx, args.name))


def _peer_id(peer: Any) -> int:
    for attr in ("user_id", "chat_id", "channel_id", "id"):
        if hasattr(peer, attr):
            return int(getattr(peer, attr))
    raise TypeError(f"peer without an ID: {peer!r}")


async def _mutate_folder_peers(
    ctx: Ctx, folder: str, chat_ids: list[ChatId], action: Literal["add", "remove"]
) -> FolderResult:
    """Add or remove chat IDs to/from a folder; `add` creates a missing folder."""
    async with get_client(ctx) as client:
        target = await _find_folder(client, folder)
        if target is None and action == "remove":
            raise _not_found(folder)

        # Only peers in the user's chat list can join a folder; others raise PeerInvalidError
        logger.info("Fetching dialogs to verify peers...")
        dialog_map = {ChatId(d.id): d for d in await client.get_dialogs()}
        input_peers = [dialog_map[cid].input_entity for cid in chat_ids if cid in dialog_map]
        skipped = [cid for cid in chat_ids if cid not in dialog_map]
        if skipped:
            ctx.warn(
                "CHATS_NOT_IN_DIALOGS",
                "Some chat IDs are not in your dialog list and were skipped.",
                chat_ids=[c.value for c in skipped],
            )
        if not input_peers:
            raise Exit.NOT_FOUND(
                "None of the chat IDs are in your dialog list.",
                context={"chat_ids": [c.value for c in chat_ids]},
                suggestion="Pick IDs from `tg chats list`.",
            )

        if target is None:
            logger.info(f"Folder '{folder}' not found, creating it...")
            created = await _create_folder_internal(client, folder, include_peers=input_peers)
            return FolderResult("created", created.id, folder, len(created.include_peers))

        current = list(target.include_peers)
        existing = {_peer_id(p) for p in current}
        if action == "add":
            updated = current + [p for p in input_peers if _peer_id(p) not in existing]
        else:
            dropped = {_peer_id(p) for p in input_peers}
            updated = [p for p in current if _peer_id(p) not in dropped]

        if len(updated) == len(current):
            return FolderResult("noop", target.id, _title(target), len(current))

        target.include_peers = updated
        await client(UpdateDialogFilterRequest(id=target.id, filter=target))
        return FolderResult("updated", target.id, _title(target), len(updated))


def _chat_ids(args: FolderPeersArgs) -> list[ChatId]:
    if args.chat:
        return list(dict.fromkeys(args.chat))
    ids = [
        c
        for item in load_items(args.input_file)
        if (c := chat_id_field(item, "id", "chat_id")) is not None
    ]
    if not ids:
        raise Exit.ARG_ERROR(
            "No chat IDs given.",
            suggestion="Pass --chat, or JSON from `tg search` or `tg chats list` with "
            "--input-file or a pipe.",
        )
    return list(dict.fromkeys(ids))


def add_to_folder(args: FolderPeersArgs, ctx: Ctx) -> FolderResult:
    """Add chats to a folder, creating the folder when it does not exist."""
    with logging_to(ctx):
        return asyncio.run(_mutate_folder_peers(ctx, args.folder, _chat_ids(args), "add"))


def remove_from_folder(args: FolderPeersArgs, ctx: Ctx) -> FolderResult:
    """Remove chats from a folder."""
    with logging_to(ctx):
        return asyncio.run(_mutate_folder_peers(ctx, args.folder, _chat_ids(args), "remove"))


# ── folders delete ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FolderDeleteArgs:
    folder: str = Arg(description="Folder name or ID")
    dry_run: bool = Flag(default=False, description="Preview the deletion; change nothing")


@dataclass(frozen=True, slots=True)
class FolderDeleteResult:
    effect: Literal["deleted", "would_delete"]
    id: int
    title: str
    would_affect: Affects | None = None


async def _delete_folder(ctx: Ctx, folder: str, dry_run: bool) -> FolderDeleteResult:
    async with get_client(ctx) as client:
        target = await _find_folder(client, folder)
        if target is None:
            raise _not_found(folder)
        title = _title(target)
        if dry_run:
            affects = Affects(
                f"Deletes folder {title!r} (ID {target.id}); its chats stay",
                (f"folder/{target.id}",),
                1,
            )
            return FolderDeleteResult("would_delete", target.id, title, affects)
        logger.info(f"Deleting folder '{title}' (ID {target.id})...")
        # filter=None deletes it
        await client(UpdateDialogFilterRequest(id=target.id, filter=None))
        return FolderDeleteResult("deleted", target.id, title)


def delete_folder(args: FolderDeleteArgs, ctx: Ctx) -> FolderDeleteResult:
    """Delete a folder entirely; the chats in it are kept."""
    with logging_to(ctx):
        return asyncio.run(_delete_folder(ctx, args.folder, args.dry_run))
