"""Configuration — reads TG_* vars from the environment and .env files.

Precedence, highest first: environment variables, ``./.env`` in the working directory,
then ``~/.telegram-cli/.env``, so `tg` works from any directory once the user file exists.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from treaty import Exit

from telegram_cli.privacy import make_private_dir

DEFAULT_DIR_NAME = ".telegram-cli"


def user_env_file() -> Path:
    return Path.home() / DEFAULT_DIR_NAME / ".env"


def env_files() -> tuple[Path, Path]:
    """The ``.env`` files settings load, lowest precedence first"""
    return user_env_file(), Path(".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TG_", env_file_encoding="utf-8")

    api_id: int
    api_hash: str
    session_dir: Path = Path.home() / DEFAULT_DIR_NAME
    session_name: str = "session"

    @property
    def session_path(self) -> str:
        """The session's path without Telethon's ``.session`` suffix"""
        make_private_dir(self.session_dir)
        return str(self.session_dir / self.session_name)

    @property
    def session_file(self) -> Path:
        """The SQLite file Telethon keeps the session (and its auth key) in"""
        return Path(f"{self.session_path}.session")

    @property
    def lock_path(self) -> Path:
        """Held while a client uses the session file, so runs never share it"""
        make_private_dir(self.session_dir)
        return self.session_dir / f"{self.session_name}.lock"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the settings singleton (loaded lazily on first call)."""
    files = env_files()  # later files win
    try:
        return Settings(_env_file=files)  # type: ignore[call-arg]
    except ValidationError as exc:
        fields = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
        raise Exit.PRECONDITION(
            "Telegram API credentials are missing or invalid.",
            context={"fields": fields, "env_files": [str(f) for f in files]},
            suggestion=f"Set TG_API_ID and TG_API_HASH in {user_env_file()} or the environment "
            "(get them at https://my.telegram.org/apps).",
        ) from exc
