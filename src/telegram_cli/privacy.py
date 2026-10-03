"""Keep the session and the credentials private to the user.

The session file holds the account's auth key: anyone who reads it is logged in as the
user. `tg` creates its session directory 0700 and the session file 0600, and tightens an
existing session file it finds wider. Files the user owns and placed themselves (a chosen
``TG_SESSION_DIR``, the ``.env`` files) are never changed: a run warns about them instead.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from treaty import Ctx

PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600
_SHARED = stat.S_IRWXG | stat.S_IRWXO
"""Any permission for the group or for others"""

# Request IDs of the runs already warned: one run may connect several times (chats review)
_warned_runs: set[str] = set()


@dataclass(frozen=True, slots=True)
class Exposure:
    """A file or directory the group or others can access"""

    path: Path
    mode: int
    """Its permission bits as found"""
    fixed: bool = False
    """Whether `tg` already tightened it"""

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.mode)

    def warn(self, ctx: Ctx) -> None:
        bits = stat.S_IMODE(self.mode)
        context: dict[str, object] = {"path": str(self.path), "mode": f"{bits:o}"}
        if self.fixed:
            ctx.warn(
                "SESSION_FILE_TIGHTENED",
                f"The session file {self.path} was readable by other users (mode {bits:o}); "
                "it is now 600.",
                **context,
                suggestion="Anyone who copied it could use the account: if that may have "
                "happened, end the session in Telegram (Settings > Devices) and run `tg auth`.",
            )
            return
        private = PRIVATE_DIR if self.is_dir else PRIVATE_FILE
        what = "session directory" if self.is_dir else "file"
        ctx.warn(
            "PERMISSIONS_TOO_OPEN",
            f"The {what} {self.path} is accessible to other users (mode {bits:o}).",
            **context,
            suggestion=f"Run `chmod {private:o} {self.path}`.",
        )


def _exposed(path: Path) -> Exposure | None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    return Exposure(path, mode) if mode & _SHARED else None


def make_private_dir(path: Path) -> None:
    """Create ``path`` with mode 0700 when missing; an existing directory is left as it is.

    Missing parents get the default mode: only the directory `tg` owns is made private.
    """
    if path.is_dir():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=PRIVATE_DIR)
    except FileExistsError:
        return  # a concurrent run created it first, and made it private
    path.chmod(PRIVATE_DIR)  # mkdir's mode is masked by the umask


def make_private_session(session_file: Path) -> list[Exposure]:
    """Create the session file 0600 before Telethon opens it, and tighten an existing one.

    SQLite gives its journal the database file's mode; a journal left by an interrupted
    run is tightened too. Returns what was found wider, already fixed.
    """
    fd = os.open(session_file, os.O_RDWR | os.O_CREAT, PRIVATE_FILE)
    os.close(fd)
    found = []
    for path in (session_file, session_file.with_name(f"{session_file.name}-journal")):
        exposure = _exposed(path)
        if exposure is not None:
            path.chmod(PRIVATE_FILE)
            found.append(Exposure(path, exposure.mode, fixed=True))
    return found


def exposures(session_dir: Path, env_files: Iterable[Path]) -> list[Exposure]:
    """The session directory and the loaded ``.env`` files other users can access"""
    found = (_exposed(path) for path in (session_dir, *env_files))
    return [exposure for exposure in found if exposure is not None]


def warn_exposed(ctx: Ctx, found: Iterable[Exposure]) -> None:
    """Add a warning per exposure to the run's response, once per run"""
    if ctx.request_id in _warned_runs:
        return
    _warned_runs.add(ctx.request_id)
    for exposure in found:
        exposure.warn(ctx)
