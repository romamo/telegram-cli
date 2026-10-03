"""Auth command — onboard a user via phone → OTP → optional 2FA.

Two ways in:

* ``tg auth`` on a terminal asks for the phone number, the code, and the 2FA password
* ``tg auth --phone +380...`` sends the code and saves the pending login; then
  ``tg auth --code 12345`` completes it. Neither step needs a terminal, so agents can log in
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from treaty import Ctx, Exit, Flag, ParseError

from telegram_cli.client import connected, rate_limited
from telegram_cli.config import get_settings


@dataclass(frozen=True, slots=True)
class AuthArgs:
    phone: str | None = Flag(
        default=None,
        pattern=r"\+?[0-9]{7,15}",
        description="Phone number in international format, e.g. +380XXXXXXXXX; sends the "
        "login code and exits, finish with --code",
    )
    code: str | None = Flag(
        default=None,
        pattern=r"[0-9]{4,8}",
        description="Login code Telegram sent after `tg auth --phone`",
    )
    password: str | None = Flag(default=None, description="2FA password, when the account has one")

    def __post_init__(self) -> None:
        if self.phone is not None and self.code is not None:
            raise ParseError(
                "--phone and --code are separate steps",
                context={"field": "code"},
                suggestion="Run `tg auth --phone NUMBER` first, then `tg auth --code CODE`.",
            )


@dataclass(frozen=True, slots=True)
class AuthResult:
    effect: Literal["created", "updated", "noop"]
    status: Literal["logged_in", "code_sent", "already_logged_in"]
    name: str | None
    username: str | None
    session_path: str
    next_step: str | None = None


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """A sent login code waiting for ``tg auth --code``"""

    phone: str
    phone_code_hash: str

    @staticmethod
    def path() -> Path:
        return get_settings().session_dir / "pending_login.json"

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The code hash lets anyone holding the code log in: keep it private to the user
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"phone": self.phone, "phone_code_hash": self.phone_code_hash}, f)

    @classmethod
    def load(cls) -> PendingLogin:
        path = cls.path()
        if not path.exists():
            raise Exit.PRECONDITION(
                "No login is waiting for a code.",
                code="NO_PENDING_LOGIN",
                suggestion="Run `tg auth --phone NUMBER` first.",
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(phone=data["phone"], phone_code_hash=data["phone_code_hash"])

    @classmethod
    def clear(cls) -> None:
        cls.path().unlink(missing_ok=True)


def _session_file() -> str:
    return f"{get_settings().session_path}.session"


def _logged_in(me: Any, effect: Literal["created", "noop"]) -> AuthResult:
    return AuthResult(
        effect=effect,
        status="logged_in" if effect == "created" else "already_logged_in",
        name=f"{me.first_name or ''} {me.last_name or ''}".strip(),
        username=f"@{me.username}" if me.username else None,
        session_path=_session_file(),
    )


def _rejected(what: str, exc: Exception, suggestion: str) -> Exception:
    return Exit.ARG_ERROR(
        f"Telegram rejected the {what}.", context={"error": str(exc)}, suggestion=suggestion
    )


async def _send_code(client: TelegramClient, phone: str) -> PendingLogin:
    try:
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError as exc:
        raise _rejected("phone number", exc, "Pass the number with its country code.") from exc
    return PendingLogin(phone, sent.phone_code_hash)


async def _sign_in(client: TelegramClient, pending: PendingLogin, code: str, args: AuthArgs,
                   ctx: Ctx, *, can_prompt: bool) -> None:
    try:
        await client.sign_in(pending.phone, code, phone_code_hash=pending.phone_code_hash)
    except PhoneCodeInvalidError as exc:
        raise _rejected("login code", exc, "Check the code and pass it again.") from exc
    except PhoneCodeExpiredError as exc:
        PendingLogin.clear()
        raise _rejected("expired login code", exc, "Request a new one with --phone.") from exc
    except SessionPasswordNeededError:
        password = args.password
        if password is None:
            if not can_prompt:
                # Keep the pending login: the same code works with the password added
                raise Exit.PRECONDITION(
                    "The account has two-step verification; a 2FA password is required.",
                    code="PASSWORD_REQUIRED",
                    suggestion="Rerun with --code and --password-from-env VAR "
                    "(or set TG_PASSWORD).",
                ) from None
            password = ctx.prompt("2FA password", flag="password-from-env")
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise _rejected("2FA password", exc, "Check the password and retry.") from exc


async def _do_auth(args: AuthArgs, ctx: Ctx) -> AuthResult:
    try:
        async with connected() as client:
            return await _auth_with(client, args, ctx)
    except FloodWaitError as exc:
        raise rate_limited(exc) from exc


async def _auth_with(client: TelegramClient, args: AuthArgs, ctx: Ctx) -> AuthResult:
    if await client.is_user_authorized():
        PendingLogin.clear()
        return _logged_in(await client.get_me(), "noop")

    if args.phone is not None:
        # Step 1 of 2: send the code and remember the login
        (await _send_code(client, args.phone)).save()
        return AuthResult(
            effect="updated",
            status="code_sent",
            name=None,
            username=None,
            session_path=_session_file(),
            next_step="tg auth --code CODE",
        )

    if args.code is not None:
        # Step 2 of 2: complete the saved login
        await _sign_in(client, PendingLogin.load(), args.code, args, ctx, can_prompt=False)
    else:
        phone = ctx.prompt("Phone number (e.g. +380XXXXXXXXX)", flag="phone").strip()
        pending = await _send_code(client, phone)
        pending.save()  # lets `tg auth --code` finish if this session is interrupted
        code = ctx.prompt("Enter the code", flag="code").strip()
        await _sign_in(client, pending, code, args, ctx, can_prompt=True)

    PendingLogin.clear()
    return _logged_in(await client.get_me(), "created")


def run_auth(args: AuthArgs, ctx: Ctx) -> AuthResult:
    """Authenticate as a Telegram user (phone → OTP → optional 2FA)."""
    return asyncio.run(_do_auth(args, ctx))
