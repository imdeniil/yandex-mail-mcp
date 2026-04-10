# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP (Model Context Protocol) server for Yandex Mail. Provides 28 email tools via IMAP/SMTP that can be used by any MCP-compatible client.

## Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Safe tests only — unit tests + read-only integration (default)
pytest

# Verbose
pytest -v

# Unit tests only (no network at all)
pytest test_helpers.py

# Read-only integration only (requires .env credentials)
pytest test_integration.py

# DESTRUCTIVE + SEND tests — actually modifies the mailbox and sends mail.
# Opt in explicitly with --run-destructive. Tests use unique markers and
# clean up after themselves, but there's always some residual state risk.
pytest --run-destructive              # everything, including writes/sends
pytest -m destructive --run-destructive  # only destructive (folders, flags, moves)
pytest -m send --run-destructive         # only actual SMTP sends

# Single test class / case
pytest test_helpers.py::TestBodystructureParser -v
pytest test_integration.py::TestGetFolderStatus::test_has_counts -v

# Test server directly
python -c "from server import list_folders; print(list_folders())"
```

## Architecture

Single-file MCP server (`server.py`) using FastMCP framework. 28 `@mcp.tool()` functions + ~18 private helpers.

**Tool categories:**
- **Read**: `list_folders`, `search_emails`, `read_email`, `download_attachment`, `get_folder_status`, `get_unread_summary`, `inspect_email`, `fetch_part`
- **Write**: `move_email`, `delete_email`, `set_flags` + `mark_read`/`mark_unread`/`mark_flagged`/`mark_answered`, `create_folder`/`rename_folder`/`delete_folder`, `empty_trash`
- **Bulk**: `bulk_set_flags`, `bulk_mark_read`/`mark_unread`/`mark_flagged`, `bulk_move`, `bulk_delete`
- **Send**: `send_email`, `reply_email`, `forward_email`

**Connection helpers**: `imap_connection()` / `smtp_connection()` context managers. IMAP refreshes `CAPABILITY` after login to detect MOVE extension.

**Encoding**: `encode_folder_name` / `decode_folder_name` for IMAP UTF-7 (Cyrillic folder names). `_quote_folder_for_command` for raw `_simple_command` paths.

**BODYSTRUCTURE parser**: `_tokenize_bodystructure` → `_parse_bodystructure_list` → `_walk_bodystructure` → `parse_bodystructure` (public). Part number assignment respects RFC 3501 §7.4.2 — including the message/rfc822 special case where disposition is at index 11 (not 8).

**Key implementation details:**
- `.env` loaded from script directory, not CWD, so MCP clients can launch from anywhere
- Logging to file (`yandex_mail_mcp.log`) because stdout is reserved for MCP JSON-RPC
- All commands use IMAP UIDs (stable) rather than sequence numbers (unstable after EXPUNGE)
- Non-ASCII search takes a two-phase path: legacy `conn.search()` → seq numbers → UID FETCH translation, because `conn._simple_command` with bytes args concatenates rather than sending literals
- Atomic `UID MOVE` (RFC 6851) is preferred where capability is advertised, with graceful fallback to `COPY+STORE+EXPUNGE`

## Testing

### Three test files

| File | Category | Network? | Safe? |
|---|---|---|---|
| `test_server.py` | Legacy integration (21 tests) | Yes | Read-only, safe |
| `test_helpers.py` | Pure unit (~125 tests) | No | Always safe |
| `test_integration.py` | Full integration | Yes | Marker-gated |

### Test markers (in `test_integration.py`)

- **(unmarked)** — read-only. Runs by default. Never modifies mailbox state.
- **`@pytest.mark.destructive`** — creates/renames/deletes folders, changes flags, moves messages, empties trash. Requires `--run-destructive`.
- **`@pytest.mark.send`** — actually sends mail via SMTP (self-addressed with unique markers). Requires `--run-destructive`.

Test isolation: each destructive test uses a unique run_id + UUID and cleans up in `finally` blocks. The `sandbox_folder` fixture creates a unique MCP-Test-* folder per test and deletes it after.

### Integration test requirements

- `.env` with `YANDEX_EMAIL` + `YANDEX_APP_PASSWORD` — tests skip cleanly if missing
- Yandex rate-limits aggressively after many rapid connections. A full `pytest --run-destructive` run (21 tests, each opening 4-6 IMAP/SMTP sessions) will typically trip the limit partway through and subsequent tests fail with `ConnectionRefusedError: [Errno 111]` or `socket.gaierror`. The rate limit clears in ~10-30 minutes. Workarounds:
  - Run destructive tests in small subsets: `pytest test_integration.py::TestFolderManagement --run-destructive`
  - Space out runs by 30 minutes
  - `imap_connection()` / `smtp_connection()` now retry 3x on transient network errors, which covers brief DNS flakes but not sustained refusal

## Sieve and server-side filters

**NOTE:** Yandex does NOT expose ManageSieve (port 4190 closed, no SIEVE capability). Managing user mail filters ("правила обработки писем") via MCP is **impossible** — users must create them in the web UI at https://mail.yandex.ru/#setup/filters. Client-side automation (MCP + LLM deciding when to move/flag/delete) replaces server-side rules functionally but is not reactive.

## SORT/THREAD

Yandex does NOT implement SORT (RFC 5256) or THREAD. All three variants (`UID SORT`, `UID THREAD REFERENCES`, `UID THREAD ORDEREDSUBJECT`) return `BAD Command syntax error`. Client-side sorting was considered but dropped — if you need sorted results, sort the output of `search_emails` yourself.

## Release

Use `/version X.Y.Z` command to release a new version. It updates `VERSION` in server.py, creates CHANGELOG.md entry, commits, tags, and pushes.
