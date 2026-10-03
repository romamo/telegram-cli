"""Common utilities for the telegram-cli."""
from __future__ import annotations

import json
import sys
from collections.abc import Callable

from treaty import Exit, PageRequest

from telegram_cli.ids import ChatId, MessageId

type JsonItem = dict[str, object]


def fetch_count(page: PageRequest | None) -> int | None:
    """How many items a list handler fetches: one past the page, so the framework can tell
    whether another page follows; None fetches everything"""
    return None if page is None or page.limit is None else page.limit + 1


def read_piped_items() -> list[JsonItem]:
    """JSON objects piped into stdin, e.g. from `tg search ... | tg folders add Work`.

    Accepts treaty envelopes (one per line, `data` unwrapped), a bare JSON array, or a bare
    object. Returns [] when stdin is a terminal or empty.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return []
    return parse_piped_items(sys.stdin.read())


def parse_piped_items(raw: str) -> list[JsonItem]:
    """The JSON objects in piped text; see ``read_piped_items``"""
    if not raw.strip():
        return []
    try:
        documents = [json.loads(raw)]
    except json.JSONDecodeError:
        # JSON Lines, such as the envelopes of a streamed `tg messages export`
        try:
            documents = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise Exit.ARG_ERROR(
                "Piped input is not JSON.",
                context={"line": exc.lineno, "column": exc.colno},
                suggestion="Pipe the JSON output of another tg command, e.g. "
                "`tg search phrase --format json`.",
            ) from exc
    items: list[JsonItem] = []
    for doc in documents:
        if isinstance(doc, dict) and "data" in doc and "meta" in doc:
            doc = doc["data"]  # a treaty envelope
        if doc is None:
            continue  # a stream's closing envelope, or a failed run
        for item in doc if isinstance(doc, list) else [doc]:
            if not isinstance(item, dict):
                raise Exit.ARG_ERROR(
                    "Piped JSON must hold objects.",
                    context={"item": repr(item)[:80]},
                    suggestion="Pipe the JSON output of another tg command.",
                )
            items.append(item)
    return items


def _int_field(item: JsonItem, *names: str) -> tuple[str, int] | None:
    """The first of ``names`` present in ``item`` and its value, which must be an integer"""
    for name in names:
        value = item.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise Exit.ARG_ERROR(
                f"Piped field {name!r} must be an integer.",
                context={"field": name, "value": repr(value)[:80]},
                suggestion="Pipe the JSON output of another tg command.",
            )
        return name, value
    return None


def _id_field[T](item: JsonItem, vo: Callable[[int], T], names: tuple[str, ...]) -> T | None:
    found = _int_field(item, *names)
    if found is None:
        return None
    name, value = found
    try:
        return vo(value)
    except ValueError as exc:
        raise Exit.ARG_ERROR(
            f"Piped field {name!r} is not a valid ID: {exc}.",
            context={"field": name, "value": value},
            suggestion="Pipe the JSON output of another tg command.",
        ) from exc


def chat_id_field(item: JsonItem, *names: str) -> ChatId | None:
    """The chat ID under the first of ``names`` present in ``item``"""
    return _id_field(item, ChatId, names)


def message_id_field(item: JsonItem, *names: str) -> MessageId | None:
    """The message ID under the first of ``names`` present in ``item``"""
    return _id_field(item, MessageId, names)
