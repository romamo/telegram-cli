# telegram-cli

A command-line Telegram client for power users and agents, inspired by [vysheng/tg](https://github.com/vysheng/tg).

Built with **[Telethon](https://telethon.dev)** (user-mode MTProto client) and **[treaty](https://pypi.org/project/treaty/)** (CLI framework implementing the [CLI Agent Spec](https://github.com/cli-agent-spec/cli-agent-spec)).

## Features

- 🔐 Authenticate as a real Telegram **user** (phone → OTP → 2FA)
- 📋 **List** all dialogs (DMs, groups, channels)
- 🔍 **Search** chats by name or message content
- 🗑️ **Delete** specific messages or entire chat histories
- 🤖 **Agent-friendly**: JSON envelopes when piped, typed exit codes, `tg manifest`, `--schema`, and dry runs for every destructive command

## Setup

### 1. Get API credentials

Go to [my.telegram.org/apps](https://my.telegram.org/apps) and create an application to get your `api_id` and `api_hash`.

### 2. Configure

Put the credentials in `~/.telegram-cli/.env` so `tg` works from any directory:

```bash
mkdir -p ~/.telegram-cli
cp .env.example ~/.telegram-cli/.env
# Edit it and set TG_API_ID and TG_API_HASH
```

A `.env` in the current directory overrides that file, and environment variables override both.

### 3. Install

```bash
uv sync
```

### 4. Authenticate

In a terminal, `tg auth` asks for everything:

```bash
uv run tg auth
# Phone number (e.g. +380XXXXXXXXX): +380XXXXXXXXX
# Enter the code: 12345
```

Without a terminal (scripts, agents), log in in two steps:

```bash
uv run tg auth --phone +380XXXXXXXXX   # sends the code
uv run tg auth --code 12345            # add --password-from-env VAR for 2FA
```

## Usage

```bash
# List all chats (--limit and --cursor page through them)
uv run tg chats list
uv run tg chats list --limit 50 --reverse
uv run tg chats list --type user --year 2019

# Search chat names (your dialogs + global directory)
uv run tg search "quarterly report"

# Search message content, everywhere or inside one chat
uv run tg messages search "invoice"
uv run tg messages search "invoice" --chat 12345

# Export a chat (one JSON line per message)
uv run tg messages export --chat 12345 --since 2025-01-01 > chat.jsonl

# Delete specific messages: preview first, then confirm
uv run tg messages delete --chat 12345 --ids 111 --ids 222
uv run tg messages delete --chat 12345 --ids 111 --ids 222 --confirm-destructive

# Clear a chat's history, or leave/remove it with --remove
uv run tg chats delete --id 12345 --dry-run
uv run tg chats delete --id 12345 --confirm-destructive

# Commands that take IDs also read piped JSON from other tg commands
uv run tg messages search "old number" | uv run tg messages delete --confirm-destructive
uv run tg search "flutter" | uv run tg folders add "Work"
uv run tg chats list --query "Publa" | uv run tg chats review
```

### Output and agents

- Output is `plain` text in a terminal and a JSON envelope (`ok`, `data`, `error`, `meta`) when piped; force one with `--format plain|json|jsonl|tsv`
- `uv run tg manifest` prints every command, flag, and exit code; `uv run tg <command> --schema` prints one command's parameters and output schema
- Destructive commands (`chats delete`, `messages delete`, `messages cleanup`, `folders delete`) only preview unless given `--confirm-destructive`
- Repeat a flag to pass several values: `--ids 111 --ids 222`
- Exit codes are typed: `2` bad arguments, `3` partial failure, `5` not found, `7` permission denied, `8` not authenticated (run `tg auth`), `11` rate limited
- Repeating a delete is safe: once nothing is left it reports `effect: "noop"` instead of failing
- Filters on `chats list` and `search` scan up to `--scan-limit` dialogs (default 3000, `0` for all) and warn with `SCAN_LIMIT_REACHED` when they stop early; `messages cleanup` does the same with `--max-matches` and `MATCH_LIMIT_REACHED`

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TG_API_ID` | ✅ | Your Telegram application ID |
| `TG_API_HASH` | ✅ | Your Telegram application hash |
| `TG_SESSION_DIR` | ❌ | Session storage directory (default: `~/.telegram-cli`) |
| `TG_SESSION_NAME` | ❌ | Session file name (default: `session`) |

## Session

The session is stored in `~/.telegram-cli/session.session`. You only need to run `tg auth` once. The session persists across terminal sessions.

Commands sharing a session take turns: a second `tg` waits up to 30 seconds for the first to finish, then exits `12` with `SESSION_BUSY` (retryable). A long `messages export` holds the session for its whole run.

## Development

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
```

Tests never touch Telegram: commands run through `app.run`, and the delete logic runs against the in-memory client in `tests/fakes.py`.
