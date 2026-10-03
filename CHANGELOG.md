# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor version
may break the command line.

## [Unreleased]

## [0.2.0] - 2026-10-03

The CLI moved from agentyper to [treaty](https://pypi.org/project/treaty/). Commands now
return JSON envelopes (`ok`, `data`, `error`, `meta`) when piped, exit with typed codes,
preview every destructive command with `--dry-run`, and refuse destructive work without
`--confirm-destructive`. The release is not additive: see Breaking and the migration notes below.

### Migrating from 0.1.0

- Replace `--id` with `--chat` on `chats delete`
- Replace `--ids` with a repeated `--chat` on `folders add`, `folders remove`, and `chats review`, and with a repeated `--message` on `messages delete`. The old flags exit 2
- Pass chats to `chats review` with `--chat` or `--input-file` instead of piping them in: stdin stays the terminal
- Log in with `tg auth --phone +NUMBER`, then `tg auth --code CODE` (plus `--password-from-env VAR` for 2FA), or run `tg auth` alone in a terminal
- Read `messages export` output as one JSON object per line
- Add `--include-others` to `messages cleanup` to keep deleting other people's matches

### Breaking

- `--id`/`--ids` became `--chat`/`--message` on every command
- `chats review` reads its chats from `--chat` or `--input-file`, not stdin, and refuses with `INPUT_REQUIRED` (exit 4) when no person can answer
- `tg auth` has a two-step `--phone` / `--code` flow for agents and scripts
- `messages export` streams one JSON line per message
- `messages cleanup` deletes only your own messages by default; `--include-others` also deletes other people's matches (#2)
- `--format plain` prints nested values as dotted keys (`would_affect.summary: ...`) and no longer truncates `key: value` lines (#4)

### Added

- `--just-me` on `chats delete` and `messages delete` deletes only your copy; it refuses in channels and supergroups (#3)
- `revoked` in `chats delete` and `messages delete` results, and `include_others`, `skipped_others`, and `scan_capped` in `messages cleanup` results (#2, #3)
- `--scan-limit` on `messages cleanup` caps the search hits it reads (default 10000) and warns `SCAN_LIMIT_REACHED` when it stops there (#2)
- `--input-file` on `messages delete`, `messages export`, `folders add`, and `folders remove`, for exec lines and MCP calls, which get an empty stdin
- An `mcp` extra and README section for serving every command as an MCP tool with `treaty-mcp` (#9)
- A conformance profile in `conformance/tg.json` (#10)
- `tg search` matches the usernames of existing chats
- Credentials load from `~/.telegram-cli/.env` too, so `tg` works from any directory
- A session lock, so two runs never share the session file
- CI on Linux and macOS running ruff, mypy, and pytest (#6)
- An MIT license and package metadata (#5)

### Changed

- Previews say how far a deletion reaches: for both sides in private chats, for everyone in groups and channels (#3)
- SIGINT and SIGTERM cancel the running Telegram call, disconnect, and release the session lock before exiting (#10)
- An unreachable Telegram exits 12 `UNAVAILABLE` (`TELEGRAM_UNREACHABLE`) instead of crashing (#10)
- Delete counts are capped at the messages each call named, and unmapped Telegram errors name the error class
- `chats review` reports how many messages each deletion removed
- Progress logs go to stderr through treaty, and only warnings and errors name their severity

### Fixed

- The session directory is created `700` and the session file `600`; a wider session file is tightened, and an open directory or `.env` file triggers a warning (#1)
- Every line of plain output ends with a newline

### Security

- `messages cleanup` no longer deletes other people's messages for both sides by default (#2)
- The session file, which holds the account's auth key, is no longer readable by other users (#1)

[Unreleased]: https://github.com/romamo/telegram-cli/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/romamo/telegram-cli/releases/tag/v0.2.0
