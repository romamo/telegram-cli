"""Value objects for Telegram identifiers."""
from __future__ import annotations

from dataclasses import dataclass


def _check_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an integer, got {value!r}")
    return value


@dataclass(frozen=True, slots=True, order=True)
class ChatId:
    """A peer in Telegram's marked ID space: users positive, groups and channels negative"""

    value: int

    def __post_init__(self) -> None:
        if _check_int(self.value, "chat ID") == 0:
            raise ValueError("chat ID cannot be 0")

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True, order=True)
class MessageId:
    """A message's ID within its chat"""

    value: int

    def __post_init__(self) -> None:
        if _check_int(self.value, "message ID") < 1:
            raise ValueError("message ID must be positive")

    def __str__(self) -> str:
        return str(self.value)
