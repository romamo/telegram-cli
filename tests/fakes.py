"""An in-memory stand-in for the parts of TelegramClient the commands use."""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from telethon import utils
from telethon.errors import MessageDeleteForbiddenError
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatAdminRights,
    ChatPhotoEmpty,
    PeerUser,
    User,
)
from telethon.tl.types.messages import AffectedHistory, AffectedMessages


def user(user_id: int, name: str = "Ann") -> User:
    return User(id=user_id, first_name=name)


def group(channel_id: int, *, admin: bool = False) -> Channel:
    rights = ChatAdminRights(delete_messages=True) if admin else None
    return Channel(
        id=channel_id,
        title=f"group {channel_id}",
        photo=ChatPhotoEmpty(),
        date=datetime(2024, 1, 1, tzinfo=UTC),
        megagroup=True,
        admin_rights=rights,
    )


def basic_group(chat_id: int) -> Chat:
    """A legacy (non-super) group, where revoke still decides who loses the messages"""
    return Chat(
        id=chat_id,
        title=f"basic group {chat_id}",
        photo=ChatPhotoEmpty(),
        participants_count=3,
        date=datetime(2024, 1, 1, tzinfo=UTC),
        version=1,
        creator=True,
    )


@dataclass
class Message:
    id: int
    message: str
    peer_id: Any
    out: bool = False
    date: datetime = datetime(2024, 1, 1, tzinfo=UTC)
    sender_id: int | None = None
    chat: Any = None
    """The chat Telegram sent along with the message, as Telethon caches it"""

    async def get_chat(self) -> Any:
        return self.chat


@dataclass
class Dialog:
    id: int
    name: str
    entity: Any
    message: Any = None
    unread_count: int = 0


@dataclass
class FakeClient:
    """Chats keyed by marked peer ID, as ``telethon.utils.get_peer_id`` returns it"""

    entities: dict[int, Any] = field(default_factory=dict)
    messages: dict[int, list[Message]] = field(default_factory=dict)
    dialogs: list[Dialog] = field(default_factory=list)
    forbidden: set[int] = field(default_factory=set)
    """Chats where deleting raises MESSAGE_DELETE_FORBIDDEN"""
    revokes: list[bool] = field(default_factory=list)
    """The revoke flag of every delete call, in order"""
    history_batch: int = 2
    """Messages one DeleteHistoryRequest removes before asking to be called again"""
    calls: list[str] = field(default_factory=list)

    def add_chat(self, entity: Any, *texts: str, out: bool = False) -> int:
        chat_id = utils.get_peer_id(entity)
        self.entities[chat_id] = entity
        peer = utils.get_peer(entity)
        msgs = self.messages.setdefault(chat_id, [])
        for text in texts:
            msgs.append(Message(len(msgs) + 1, text, peer, out=out, chat=entity))
        return chat_id

    async def get_entity(self, chat_id: int) -> Any:
        self.calls.append(f"get_entity:{chat_id}")
        if chat_id not in self.entities:
            raise ValueError(f"Could not find the input entity for {chat_id}")
        return self.entities[chat_id]

    async def get_messages(
        self, entity: Any, *, ids: list[int] | None = None, search: str | None = None,
        limit: int | None = None,
    ) -> list[Any]:
        if entity is None:
            hits = [
                m for msgs in self.messages.values() for m in msgs if search and search in m.message
            ]
            return hits[:limit]
        by_id = {m.id: m for m in self.messages[utils.get_peer_id(entity)]}
        return [by_id.get(i) for i in ids or []]

    async def iter_messages(self, entity: Any, from_user: str | None = None) -> AsyncIterator[Any]:
        for m in list(self.messages[utils.get_peer_id(entity)]):
            if from_user is None or (from_user == "me" and m.out):
                yield m

    async def delete_messages(
        self, entity: Any, ids: Iterable[int], *, revoke: bool = True
    ) -> list[AffectedMessages]:
        chat_id = utils.get_peer_id(entity)
        self.calls.append(f"delete_messages:{chat_id}")
        self.revokes.append(revoke)
        if chat_id in self.forbidden:
            raise MessageDeleteForbiddenError(request=None)
        wanted = set(ids)
        before = len(self.messages[chat_id])
        self.messages[chat_id] = [m for m in self.messages[chat_id] if m.id not in wanted]
        return [AffectedMessages(pts=1, pts_count=before - len(self.messages[chat_id]))]

    async def delete_dialog(self, entity: Any) -> None:
        chat_id = utils.get_peer_id(entity)
        self.calls.append(f"delete_dialog:{chat_id}")
        self.messages.pop(chat_id, None)

    def iter_dialogs(self, offset_date: datetime | None = None) -> AsyncIterator[Dialog]:
        async def gen() -> AsyncIterator[Dialog]:
            for d in self.dialogs:
                yield d

        return gen()

    async def get_dialogs(self, limit: int | None = None) -> list[Dialog]:
        return self.dialogs[:limit]

    async def __call__(self, request: Any) -> Any:
        assert isinstance(request, DeleteHistoryRequest)
        chat_id = utils.get_peer_id(request.peer)
        self.calls.append(f"delete_history:{chat_id}")
        self.revokes.append(bool(request.revoke))
        msgs = self.messages[chat_id]
        removed, self.messages[chat_id] = msgs[: self.history_batch], msgs[self.history_batch :]
        return AffectedHistory(pts=1, pts_count=len(removed), offset=len(self.messages[chat_id]))


def peer_user(user_id: int) -> PeerUser:
    return PeerUser(user_id)
