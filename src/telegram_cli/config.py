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

DEFAULT_DIR_NAME = ".telegram-cli"


def user_env_file() -> Path:
    return Path.home() / DEFAULT_DIR_NAME / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TG_", env_file_encoding="utf-8")

    api_id: int
    api_hash: str
    session_dir: Path = Path.home() / DEFAULT_DIR_NAME
    session_name: str = "session"

    @property
    def session_path(self) -> str:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        return str(self.session_dir / self.session_name)

    @property
    def lock_path(self) -> Path:
        """Held while a client uses the session file, so runs never share it"""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        return self.session_dir / f"{self.session_name}.lock"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the settings singleton (loaded lazily on first call)."""
    env_files = (user_env_file(), Path(".env"))  # later files win
    try:
        return Settings(_env_file=env_files)  # type: ignore[call-arg]
    except ValidationError as exc:
        fields = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
        raise Exit.PRECONDITION(
            "Telegram API credentials are missing or invalid.",
            context={"fields": fields, "env_files": [str(f) for f in env_files]},
            suggestion=f"Set TG_API_ID and TG_API_HASH in {user_env_file()} or the environment "
            "(get them at https://my.telegram.org/apps).",
        ) from exc
