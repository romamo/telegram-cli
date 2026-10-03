"""What kind of dialog an entity is."""
from __future__ import annotations

from enum import StrEnum

from telethon.tl.types import Channel, Chat, User


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
