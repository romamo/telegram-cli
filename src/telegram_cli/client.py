"""Telethon client factory — async context manager for all commands."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from telethon import TelegramClient
from telethon.errors import ChatAdminRequiredError, FloodWaitError, ForbiddenError, RPCError
from treaty import Exit

from telegram_cli.config import get_settings
from telegram_cli.ids import ChatId
from telegram_cli.session_lock import session_lock

# Exit codes every command talking to Telegram through get_client() may raise
CLIENT_EXIT_CODES = ("AUTH_REQUIRED", "PERMISSION_DENIED", "RATE_LIMITED", "UNAVAILABLE")


def new_client() -> TelegramClient:
    cfg = get_settings()
    return TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash)


def rate_limited(exc: FloodWaitError) -> Exception:
    return Exit.RATE_LIMITED(
        f"Telegram asked to wait {exc.seconds} seconds before the next request.",
        retry_after_ms=exc.seconds * 1000,
        suggestion=f"Wait {exc.seconds} seconds, then retry.",
    )


def rpc_failure(exc: RPCError) -> Exception:
    """The typed exit for a Telegram error a command did not handle itself"""
    context = {"telegram_error": type(exc).__name__}
    if isinstance(exc, FloodWaitError):
        return rate_limited(exc)
    if isinstance(exc, (ForbiddenError, ChatAdminRequiredError)):
        return Exit.PERMISSION_DENIED(
            f"Telegram refused the request: {exc.message}.",
            context=context,
            suggestion="Only admins can do this in that chat; act on your own messages instead.",
        )
    return Exit.GENERAL_ERROR(
        f"Telegram returned an error: {exc.message}.", code="TELEGRAM_ERROR", context=context
    )


@asynccontextmanager
async def connected() -> AsyncIterator[TelegramClient]:
    """A connected client holding the session lock, logged in or not; disconnects after"""
    async with session_lock(get_settings().lock_path):
        client = new_client()
        await client.connect()
        try:
            yield client
        finally:
            await client.disconnect()


@asynccontextmanager
async def get_client() -> AsyncIterator[TelegramClient]:
    """Yield an authorized Telethon client, then cleanly disconnect.

    Raises AUTH_REQUIRED when no user is logged in, UNAVAILABLE (SESSION_BUSY) when another
    run holds the session, and turns Telegram errors raised in the body into typed exits:
    RATE_LIMITED, PERMISSION_DENIED, or GENERAL_ERROR.
    """
    async with connected() as client:
        if not await client.is_user_authorized():
            raise Exit.AUTH_REQUIRED(
                "Not authenticated with Telegram.",
                suggestion="Run `tg auth` first.",
            )
        try:
            yield client
        except RPCError as exc:
            raise rpc_failure(exc) from exc


async def resolve_entity(client: TelegramClient, chat_id: ChatId) -> Any:
    """The user, chat, or channel behind ``chat_id``; NOT_FOUND when Telegram cannot resolve it"""
    try:
        return await client.get_entity(chat_id.value)
    except ValueError as exc:
        raise Exit.NOT_FOUND(
            f"Chat {chat_id} was not found.",
            context={"chat_id": chat_id.value},
            suggestion="Pick an ID from `tg chats list`.",
        ) from exc
