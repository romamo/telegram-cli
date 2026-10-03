"""telegram-cli — main treaty application."""
from __future__ import annotations

from datetime import date

from treaty import App, Format

from telegram_cli import auth
from telegram_cli.client import CLIENT_EXIT_CODES
from telegram_cli.commands import chats, delete, export, folders, search, stats
from telegram_cli.ids import ChatId, MessageId
from telegram_cli.render import render_plain

app = App(
    "tg",
    version="0.1.0",
    description="A Telegram CLI tool for power users and agents",
    default_timeout=120,
)
app.format(Format.PLAIN, render=render_plain)
app.scalar(date, parse=date.fromisoformat, pattern=r"\d{4}-\d{2}-\d{2}", serialize=date.isoformat)
app.scalar(ChatId, parse=ChatId, base=int)
app.scalar(MessageId, parse=MessageId, base=int, minimum=1)

_NOT_FOUND = (*CLIENT_EXIT_CODES, "NOT_FOUND")
# Telegram calls that page through every dialog or message can take minutes
_LONG = 600

# ── Auth ─────────────────────────────────────────────────────────────────────

app.command(
    "auth",
    description="Log in as a Telegram user (phone, login code, optional 2FA password): "
    "prompts on a terminal, or runs in two steps with --phone then --code; "
    "the session is saved to ~/.telegram-cli/session.session, so it runs once",
    danger_level="mutating",
    exit_codes=("RATE_LIMITED", "UNAVAILABLE"),
    interactive=True,
    has_network_io=True,
    timeout=_LONG,
    examples=[
        ("Log in from a terminal", "tg auth"),
        ("Step 1 without a terminal: send the code", "tg auth --phone +380XXXXXXXXX"),
        ("Step 2: finish with the code", "tg auth --code 12345"),
    ],
)(auth.run_auth)

# ── Stats ────────────────────────────────────────────────────────────────────

app.command(
    "stats",
    description="Show account info and dialog statistics (total chats, unread, muted)",
    danger_level="safe",
    exit_codes=CLIENT_EXIT_CODES,
    has_network_io=True,
    timeout=_LONG,
    examples=[("Account summary", "tg stats")],
)(stats.stats)

# ── Chats ─────────────────────────────────────────────────────────────────────

chats_group = app.group("chats", description="List, review, and delete chats")

chats_group.command(
    "list",
    description="List Telegram dialogs (DMs, groups, channels), newest first",
    danger_level="safe",
    exit_codes=CLIENT_EXIT_CODES,
    has_network_io=True,
    examples=[
        ("Chats last active in 2020", "tg chats list --year 2020"),
        ("Private chats from 2019", "tg chats list --type user --year 2019"),
        ("The 50 oldest chats", "tg chats list --limit 50 --reverse"),
    ],
)(chats.list_chats)

chats_group.command(
    "delete",
    description="Clear a chat's history, or remove the chat completely (leave the group)",
    danger_level="destructive",
    exit_codes=_NOT_FOUND,
    has_network_io=True,
    timeout=_LONG,
    examples=[
        ("Preview clearing a chat", "tg chats delete --chat 12345 --dry-run"),
        ("Leave a group", "tg chats delete --chat -100123 --remove --confirm-destructive"),
    ],
)(delete.delete_chat)

chats_group.command(
    "review",
    description="For a person at a terminal: review chats one by one and delete or remove "
    "each; agents use `chats delete` instead. Exits 4 (INPUT_REQUIRED) when no one can answer",
    danger_level="mutating",
    interactive=True,
    exit_codes=_NOT_FOUND,
    has_network_io=True,
    timeout=None,
    examples=[
        ("Review two chats", "tg chats review --chat 111 --chat 222"),
        (
            "Review chats matching a name",
            'tg chats review --input-file <(tg chats list --query "Publa" --format json)',
        ),
    ],
)(chats.review_chats)

# ── Search ────────────────────────────────────────────────────────────────────

app.command(
    "search",
    description="Search chats by name: your dialogs, then Telegram's global directory",
    danger_level="safe",
    exit_codes=CLIENT_EXIT_CODES,
    has_network_io=True,
    examples=[
        ("Find a chat by name", 'tg search "quarterly report"'),
        ("Only groups", 'tg search "crypto" --type group'),
    ],
)(search.search)

# ── Messages ──────────────────────────────────────────────────────────────────

messages_group = app.group("messages", description="Search, export, and delete messages")

messages_group.command(
    "search",
    description="Search inside message content across all chats, or in one chat (server-side)",
    danger_level="safe",
    exit_codes=_NOT_FOUND,
    has_network_io=True,
    examples=[
        ("Search every chat", 'tg messages search "quarterly report"'),
        ("Search one chat", 'tg messages search "invoice" --chat 12345'),
    ],
)(search.search_messages)

messages_group.command(
    "delete",
    description="Delete specific messages by ID, or the messages piped from `tg messages search`",
    danger_level="destructive",
    exit_codes=_NOT_FOUND,
    has_network_io=True,
    examples=[
        (
            "Preview deleting two messages",
            "tg messages delete --chat 12345 --message 111 --message 222",
        ),
        (
            "Delete search hits",
            'tg messages search "oops" | tg messages delete --confirm-destructive',
        ),
    ],
)(delete.delete_messages)

messages_group.command(
    "cleanup",
    description="Find messages containing a phrase across all chats and delete them",
    danger_level="destructive",
    exit_codes=(*CLIENT_EXIT_CODES, "PARTIAL_FAILURE"),
    has_network_io=True,
    timeout=_LONG,
    examples=[
        ("Preview a cleanup", 'tg messages cleanup "old phone number" --dry-run'),
        ("Delete up to 5000 matches", 'tg messages cleanup "spam" --max-matches 5000 '
         "--confirm-destructive"),
    ],
)(delete.cleanup)

messages_group.command(
    "export",
    description="Export messages from a chat or group, one event per message, newest first",
    danger_level="safe",
    exit_codes=_NOT_FOUND,
    has_network_io=True,
    streaming=True,
    renderers={Format.PLAIN: export.render_message},
    examples=[
        ("Export a whole chat", "tg messages export --chat 12345 --format jsonl"),
        ("The last 500 messages", "tg messages export --chat 12345 --limit 500"),
        ("Messages since a date", "tg messages export --chat 12345 --since 2025-01-01"),
    ],
)(export.export_messages)

# ── Folders ──────────────────────────────────────────────────────────────────

folders_group = app.group("folders", description="Manage Telegram folders (dialog filters)")

folders_group.command(
    "list",
    description="List all your Telegram folders",
    danger_level="safe",
    exit_codes=CLIENT_EXIT_CODES,
    has_network_io=True,
    paginated=False,  # Telegram allows a handful of folders
    examples=[("All folders", "tg folders list")],
)(folders.list_folders)

folders_group.command(
    "create",
    description="Create a new empty folder",
    danger_level="mutating",
    exit_codes=CLIENT_EXIT_CODES,
    has_network_io=True,
    examples=[("Create a folder", 'tg folders create "Work"')],
)(folders.create_folder)

folders_group.command(
    "add",
    description="Add chats to a folder (by --chat or piped JSON), creating the folder if needed",
    danger_level="mutating",
    exit_codes=_NOT_FOUND,
    has_network_io=True,
    examples=[
        ("Add two chats", 'tg folders add "Work" --chat 111 --chat 222'),
        ("Add search hits", 'tg search "flutter" | tg folders add "Work"'),
    ],
)(folders.add_to_folder)

folders_group.command(
    "remove",
    description="Remove chats from a folder (by --chat or piped JSON)",
    danger_level="mutating",
    exit_codes=_NOT_FOUND,
    has_network_io=True,
    examples=[("Remove a chat", 'tg folders remove "Work" --chat 111')],
)(folders.remove_from_folder)

folders_group.command(
    "delete",
    description="Delete a folder entirely; the chats in it are kept",
    danger_level="destructive",
    exit_codes=_NOT_FOUND,
    has_network_io=True,
    examples=[("Delete a folder", 'tg folders delete "Work" --confirm-destructive')],
)(folders.delete_folder)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    app.main()


if __name__ == "__main__":
    main()
