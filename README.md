# Yandex Mail MCP Server

MCP (Model Context Protocol) server for Yandex Mail. Enables Claude Desktop and other MCP clients to read, search, and send emails via Yandex Mail.

## Features

- **List folders** — with decoded Russian folder names
- **Search emails** — by sender, subject, date, or custom IMAP queries (supports Cyrillic)
- **Read emails** — full content with text/HTML body
- **Download attachments** — save to disk
- **Send emails** — plain text or HTML
- **Move/Delete emails** — organize your mailbox

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/yandex-mail-mcp.git
cd yandex-mail-mcp

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your Yandex email and app password
```

## Yandex Setup

1. Go to [Yandex ID](https://id.yandex.ru/)
2. Enable **Two-Factor Authentication** (required for app passwords)
3. Go to **Security → App Passwords**
4. Create new app password for "Mail"
5. Copy the generated password to `.env`

## Claude Desktop Configuration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "yandex-mail": {
      "command": "/path/to/yandex-mail-mcp/.venv/bin/python",
      "args": ["/path/to/yandex-mail-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop after configuration.

## Available Tools

| Tool | Description |
|------|-------------|
| `list_folders()` | List all mailbox folders |
| `search_emails(folder, query, limit)` | Search emails with IMAP queries |
| `read_email(folder, email_id)` | Read full email content |
| `download_attachment(folder, email_id, filename, save_dir)` | Download attachment to disk |
| `send_email(to, subject, body, cc, bcc, html)` | Send an email |
| `move_email(folder, email_id, destination)` | Move email to another folder |
| `delete_email(folder, email_id)` | Delete email (move to Trash) |

## Search Query Examples

```
ALL                          # All emails
UNSEEN                       # Unread emails
FROM sender@example.com      # From specific sender
SUBJECT hello                # Subject contains "hello"
SINCE 01-Dec-2024            # Emails since date
UNSEEN FROM boss@company.com # Combined query
```

## Running Tests

```bash
source .venv/bin/activate
pytest test_server.py -v
```

## Security Notes

- **`send_email` attachments can read any file accessible to the server process.** The `attachments` parameter accepts absolute file paths, so in principle an LLM could be prompt-injected (e.g. via the body of an incoming email read through `read_email`) into attaching sensitive files such as `~/.ssh/id_rsa` to an outgoing message. This is inherent to exposing a filesystem-reading primitive over MCP.

  **Mitigations:**
  - Every `send_email` call must be approved by you in the MCP client (Claude Desktop shows tool calls before executing them — **always read which files are being attached before approving**).
  - Every attachment path is written to `yandex_mail_mcp.log` for audit.
  - Run the server as a user that only has access to files you are willing to send by email.

- **`download_attachment` sanitises filenames** from received email (strips path components, asserts the resolved path stays within `save_dir`) so a malicious sender cannot write outside the target directory.

- **Credentials** (`YANDEX_EMAIL`, `YANDEX_APP_PASSWORD`) are loaded from `.env` in the server's own directory, never from the MCP client's CWD. Keep `.env` out of version control.

## License

MIT
