"""
Yandex Mail MCP Server

Provides email tools for Claude Desktop via MCP protocol.
Uses IMAP for reading and SMTP for sending.
"""

import imaplib
import smtplib
import email
from email import encoders
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os
import sys
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from imapclient import imap_utf7

VERSION = "0.0.1"

# Load environment variables from script's directory
SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(SCRIPT_DIR / ".env")

# Configure logging (not print - stdout is for MCP protocol)
logging.basicConfig(level=logging.INFO, filename=str(SCRIPT_DIR / "yandex_mail_mcp.log"))
logger = logging.getLogger(__name__)

# Yandex server settings
IMAP_SERVER = "imap.yandex.com"
IMAP_PORT = 993
SMTP_SERVER = "smtp.yandex.com"
SMTP_PORT = 587

# Credentials from environment
EMAIL = os.getenv("YANDEX_EMAIL")
PASSWORD = os.getenv("YANDEX_APP_PASSWORD")

# Create MCP server
mcp = FastMCP("Yandex Mail")


def decode_mime_header(header_value: str) -> str:
    """Decode MIME-encoded email header."""
    if not header_value:
        return ""
    decoded_parts = []
    for part, charset in decode_header(header_value):
        if isinstance(part, bytes):
            charset = charset or "utf-8"
            try:
                decoded_parts.append(part.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


@contextmanager
def imap_connection():
    """Context manager for IMAP connection."""
    if not EMAIL or not PASSWORD:
        raise ValueError("YANDEX_EMAIL and YANDEX_APP_PASSWORD must be set in .env")

    conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    try:
        conn.login(EMAIL, PASSWORD)
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


@contextmanager
def smtp_connection():
    """Context manager for SMTP connection."""
    if not EMAIL or not PASSWORD:
        raise ValueError("YANDEX_EMAIL and YANDEX_APP_PASSWORD must be set in .env")

    conn = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
    try:
        conn.starttls()
        conn.login(EMAIL, PASSWORD)
        yield conn
    finally:
        try:
            conn.quit()
        except Exception:
            pass


def decode_folder_name(imap_name: str) -> str:
    """Decode IMAP modified UTF-7 folder name to readable string."""
    try:
        return imap_utf7.decode(imap_name.encode())
    except Exception:
        return imap_name


def encode_folder_name(name: str) -> str:
    """
    Encode folder name to IMAP modified UTF-7 if it contains non-ASCII.

    Pass-through for ASCII input (including already-encoded UTF-7 like
    `&BCcENQRABDcENwQwBDw-`), so callers can safely pass either human-readable
    names ("Корзина") or raw imap_name values from list_folders().
    """
    if all(ord(c) < 128 for c in name):
        return name
    try:
        return imap_utf7.encode(name).decode("ascii")
    except Exception:
        return name


def _parse_folder_line(line) -> Optional[tuple[list[str], str]]:
    """
    Parse an IMAP LIST response line into (attrs, imap_name).

    Handles both quoted folder names (`"Trash"`) and unquoted atoms (`INBOX`),
    and both quoted and NIL hierarchy delimiters. Returns None if the line
    cannot be parsed.
    """
    if not isinstance(line, (bytes, bytearray)):
        return None
    decoded = line.decode("utf-8", errors="replace")

    # Attributes list: (\Attr1 \Attr2)
    if not decoded.startswith("("):
        return None
    attrs_end = decoded.find(")")
    if attrs_end < 0:
        return None
    attrs = decoded[1:attrs_end].split()

    rest = decoded[attrs_end + 1 :].strip()

    # Hierarchy delimiter: "/" or NIL
    if rest.startswith('"'):
        delim_end = rest.find('"', 1)
        if delim_end < 0:
            return None
        rest = rest[delim_end + 1 :].strip()
    elif rest[:3].upper() == "NIL":
        rest = rest[3:].strip()
    else:
        return None

    if not rest:
        return None

    # Mailbox name: quoted string or atom. Strip once more to be robust
    # against any trailing whitespace or CR the server may have left on.
    rest = rest.rstrip()

    if rest.startswith('"'):
        if len(rest) < 2 or not rest.endswith('"'):
            return None
        imap_name = rest[1:-1]
    else:
        imap_name = rest.split()[0]

    return attrs, imap_name


def _find_trash_folder(conn) -> str:
    """
    Find the Trash folder via IMAP SPECIAL-USE attribute with fallbacks.

    Preference order:
    1. Folder with \\Trash special-use attribute (RFC 6154)
    2. Folder whose decoded name matches Trash/Корзина/Deleted Items
    3. Literal "Trash" as last resort
    """
    try:
        status, folder_data = conn.list()
    except Exception:
        return "Trash"

    if status != "OK" or not folder_data:
        return "Trash"

    parsed: list[tuple[list[str], str]] = []
    for item in folder_data:
        result = _parse_folder_line(item)
        if result is not None:
            parsed.append(result)

    # Pass 1: \Trash special-use attribute
    for attrs, imap_name in parsed:
        if any(a.lower() == "\\trash" for a in attrs):
            return imap_name

    # Pass 2: known localized names
    known = {"trash", "корзина", "deleted items", "deleted messages"}
    for _attrs, imap_name in parsed:
        if decode_folder_name(imap_name).strip().casefold() in known:
            return imap_name

    return "Trash"


@mcp.tool()
def list_folders() -> list[dict]:
    """
    List all mail folders in the Yandex mailbox.

    Returns list of folders with:
    - name: Human-readable folder name (decoded from IMAP UTF-7)
    - imap_name: Raw IMAP folder name (use this for other operations like search_emails)
    - attrs: IMAP folder attributes, e.g. ["\\HasNoChildren", "\\Trash"]
    """
    with imap_connection() as conn:
        status, folder_data = conn.list()
        if status != "OK":
            raise Exception("Failed to list folders")

        folders = []
        for item in folder_data:
            parsed = _parse_folder_line(item)
            if parsed is None:
                continue
            attrs, imap_name = parsed
            folders.append({
                "name": decode_folder_name(imap_name),
                "imap_name": imap_name,
                "attrs": attrs,
            })

        return folders


def build_imap_search_criteria(query: str) -> list[str]:
    """
    Parse user-friendly query into IMAP search criteria with proper quoting.

    Handles: FROM, TO, CC, BCC, SUBJECT, BODY, TEXT
    These keywords need their values quoted for IMAP.
    """
    if not query or query.upper() == "ALL":
        return ["ALL"]

    # Keywords that need their following value quoted
    keywords_needing_quotes = {"FROM", "TO", "CC", "BCC", "SUBJECT", "BODY", "TEXT"}

    result = []
    tokens = query.split()
    i = 0

    while i < len(tokens):
        token = tokens[i]
        upper_token = token.upper()

        if upper_token in keywords_needing_quotes and i + 1 < len(tokens):
            # This keyword needs the next value quoted
            value = tokens[i + 1]
            # Remove existing quotes if any, then add proper quotes
            value = value.strip('"\'')
            result.append(upper_token)
            result.append(f'"{value}"')
            i += 2
        else:
            result.append(token)
            i += 1

    return result


@mcp.tool()
def search_emails(
    folder: str = "INBOX",
    query: str = "ALL",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """
    Search emails in a folder.

    Args:
        folder: Mailbox folder (default: INBOX). Use list_folders() to see available folders.
            Accepts either ASCII names, raw IMAP names from list_folders(), or
            human-readable non-ASCII names (e.g. "Корзина") — the latter are
            auto-encoded to IMAP modified UTF-7.
        query: IMAP search query. Examples:
            - "ALL" - all emails
            - "UNSEEN" - unread emails
            - "FROM sender@example.com" - from specific sender
            - "SUBJECT hello" - subject contains "hello"
            - "SINCE 01-Dec-2024" - emails since date
            - "BEFORE 31-Dec-2024" - emails before date
            - Can combine: "UNSEEN FROM boss@company.com"
        limit: Maximum number of emails to return (default: 20)
        offset: Number of newest-first results to skip, for pagination (default: 0)

    Returns list of email summaries with id, subject, from, date.
    """
    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Search emails with properly quoted criteria
        criteria = build_imap_search_criteria(query)

        # Use UTF-8 charset for non-ASCII queries (Cyrillic, etc.)
        has_non_ascii = any(ord(c) > 127 for c in query)
        if has_non_ascii:
            # For UTF-8 search, we need to pass criteria as a single string
            criteria_str = " ".join(criteria)
            status, message_ids = conn.search("UTF-8", criteria_str.encode("utf-8"))
        else:
            status, message_ids = conn.search(None, *criteria)

        if status != "OK":
            raise Exception(f"Search failed: {query}")

        ids = message_ids[0].split()
        # Newest-first, then paginate via offset + limit
        ids = list(reversed(ids))
        ids = ids[offset : offset + limit]

        emails = []
        for msg_id in ids:
            # Fetch headers only for performance
            status, msg_data = conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if status != "OK":
                continue

            raw_header = msg_data[0][1]
            msg = email.message_from_bytes(raw_header)

            subject = decode_mime_header(msg.get("Subject", ""))
            from_addr = decode_mime_header(msg.get("From", ""))
            date_str = msg.get("Date", "")

            emails.append({
                "id": msg_id.decode("utf-8"),
                "subject": subject,
                "from": from_addr,
                "date": date_str
            })

        return emails


@mcp.tool()
def read_email(folder: str, email_id: str) -> dict:
    """
    Read full email content by ID.

    Args:
        folder: Mailbox folder containing the email
        email_id: Email ID from search_emails() result

    Returns email with subject, from, to, date, body_text, body_html, attachments list.
    """
    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.fetch(email_id.encode(), "(RFC822)")
        if status != "OK":
            raise Exception(f"Failed to fetch email: {email_id}")

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_header(msg.get("Subject", ""))
        from_addr = decode_mime_header(msg.get("From", ""))
        to_addr = decode_mime_header(msg.get("To", ""))
        date_str = msg.get("Date", "")

        body_text = ""
        body_html = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                if "attachment" in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        attachments.append({
                            "filename": decode_mime_header(filename),
                            "content_type": content_type,
                            "size": len(part.get_payload(decode=True) or b"")
                        })
                elif content_type == "text/plain" and not body_text:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_text = payload.decode(charset, errors="replace")
                elif content_type == "text/html" and not body_html:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_html = payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                if msg.get_content_type() == "text/html":
                    body_html = payload.decode(charset, errors="replace")
                else:
                    body_text = payload.decode(charset, errors="replace")

        return {
            "id": email_id,
            "subject": subject,
            "from": from_addr,
            "to": to_addr,
            "date": date_str,
            "body_text": body_text,
            "body_html": body_html,
            "attachments": attachments
        }


@mcp.tool()
def download_attachment(
    folder: str,
    email_id: str,
    filename: str,
    save_dir: Optional[str] = None
) -> dict:
    """
    Download an email attachment to disk.

    Args:
        folder: Mailbox folder containing the email
        email_id: Email ID from search_emails() result
        filename: Attachment filename to download (from read_email attachments list)
        save_dir: Directory to save the file (default: ~/Downloads)

    Returns dict with saved file path and size.
    """
    # Default save directory
    if save_dir is None:
        save_dir = str(Path.home() / "Downloads")

    save_path = Path(save_dir)
    if not save_path.exists():
        save_path.mkdir(parents=True, exist_ok=True)

    with imap_connection() as conn:
        status, _ = conn.select(encode_folder_name(folder), readonly=True)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        status, msg_data = conn.fetch(email_id.encode(), "(RFC822)")
        if status != "OK":
            raise Exception(f"Failed to fetch email: {email_id}")

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        # Find the attachment
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" not in content_disposition:
                continue

            part_filename = part.get_filename()
            if part_filename:
                decoded_filename = decode_mime_header(part_filename)
                if decoded_filename == filename:
                    # Found the attachment
                    payload = part.get_payload(decode=True)
                    if payload:
                        # Sanitize: email-provided filenames are untrusted.
                        # Strip any path components to prevent path traversal
                        # (e.g. "../../.bashrc" → ".bashrc").
                        safe_name = Path(decoded_filename).name
                        if not safe_name or safe_name in (".", ".."):
                            raise ValueError(
                                f"Invalid attachment filename: {decoded_filename}"
                            )

                        save_root = save_path.resolve()
                        file_path = (save_root / safe_name).resolve()

                        # Defensive: ensure the resolved path stays within save_dir
                        try:
                            file_path.relative_to(save_root)
                        except ValueError:
                            raise ValueError(
                                f"Attachment path escapes save directory: {decoded_filename}"
                            )

                        with open(file_path, "wb") as f:
                            f.write(payload)

                        return {
                            "status": "downloaded",
                            "filename": safe_name,
                            "path": str(file_path),
                            "size": len(payload),
                            "content_type": part.get_content_type()
                        }

        raise Exception(f"Attachment not found: {filename}")


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    html: bool = False,
    attachments: Optional[list[str]] = None,
) -> dict:
    """
    Send an email via Yandex SMTP.

    Args:
        to: Recipient email address (comma-separated for multiple)
        subject: Email subject
        body: Email body (plain text or HTML based on html flag)
        cc: CC recipients (optional, comma-separated)
        bcc: BCC recipients (optional, comma-separated)
        html: If True, body is treated as HTML (default: False)
        attachments: Optional list of absolute file paths to attach. Each
            attachment is resolved and must be a regular file. SECURITY: this
            tool can read any file accessible to the MCP server process and
            exfiltrate it via email — the MCP client should surface every
            send_email call for user approval. All attached paths are logged
            to yandex_mail_mcp.log for audit.

    Returns confirmation with recipients and attached file names.
    """
    if not EMAIL:
        raise ValueError("YANDEX_EMAIL must be set in .env")

    body_subtype = "html" if html else "plain"
    attached_names: list[str] = []

    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(body, body_subtype, "utf-8"))
        for filepath in attachments:
            path = Path(filepath).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Attachment not found: {filepath}")
            logger.info("send_email attaching file: %s", path)
            with open(path, "rb") as f:
                data = f.read()
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            # Keyword form of add_header applies RFC 2231 encoding
            # automatically for non-ASCII filenames.
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=path.name,
            )
            msg.attach(part)
            attached_names.append(path.name)
    elif html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")

    msg["Subject"] = subject
    msg["From"] = EMAIL
    msg["To"] = to
    if cc:
        msg["Cc"] = cc

    # Build recipient list
    recipients = [addr.strip() for addr in to.split(",")]
    if cc:
        recipients.extend([addr.strip() for addr in cc.split(",")])
    if bcc:
        recipients.extend([addr.strip() for addr in bcc.split(",")])

    with smtp_connection() as conn:
        conn.send_message(msg, EMAIL, recipients)

    return {
        "status": "sent",
        "to": to,
        "subject": subject,
        "cc": cc,
        "bcc": bcc,
        "attachments": attached_names,
    }


@mcp.tool()
def move_email(folder: str, email_id: str, destination: str) -> dict:
    """
    Move an email to another folder.

    Args:
        folder: Source folder containing the email
        email_id: Email ID to move
        destination: Destination folder name

    Returns confirmation of move.
    """
    encoded_source = encode_folder_name(folder)
    encoded_dest = encode_folder_name(destination)

    with imap_connection() as conn:
        status, _ = conn.select(encoded_source)
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Copy to destination
        status, _ = conn.copy(email_id.encode(), encoded_dest)
        if status != "OK":
            raise Exception(f"Failed to copy email to: {destination}")

        # Mark original as deleted
        status, _ = conn.store(email_id.encode(), "+FLAGS", "\\Deleted")
        if status != "OK":
            raise Exception("Failed to mark original as deleted")

        # Expunge to actually delete
        conn.expunge()

        return {
            "status": "moved",
            "email_id": email_id,
            "from_folder": folder,
            "to_folder": destination
        }


@mcp.tool()
def delete_email(folder: str, email_id: str) -> dict:
    """
    Delete an email (move to Trash).

    Trash folder is discovered via the IMAP \\Trash SPECIAL-USE attribute
    (RFC 6154) with fallbacks to common localized names. If no trash folder
    is found or copy fails, the email is permanently deleted.

    Args:
        folder: Folder containing the email
        email_id: Email ID to delete

    Returns confirmation of deletion.
    """
    with imap_connection() as conn:
        # Resolve trash folder before selecting the source
        trash_folder = _find_trash_folder(conn)

        status, _ = conn.select(encode_folder_name(folder))
        if status != "OK":
            raise Exception(f"Failed to select folder: {folder}")

        # Don't try to copy INTO the folder we're already in. Compare the
        # decoded human-readable forms with casefold so "trash" == "Trash"
        # and different but equivalent UTF-7 encodings match.
        folder_key = decode_folder_name(encode_folder_name(folder)).strip().casefold()
        trash_key = decode_folder_name(trash_folder).strip().casefold()
        same_folder = folder_key == trash_key

        if not same_folder:
            status, _ = conn.copy(email_id.encode(), trash_folder)
            if status != "OK":
                # If Trash copy doesn't work, fall back to permanent delete
                status, _ = conn.store(email_id.encode(), "+FLAGS", "\\Deleted")
                if status != "OK":
                    raise Exception("Failed to delete email")
                conn.expunge()
                return {
                    "status": "deleted_permanently",
                    "email_id": email_id,
                    "folder": folder,
                }

            conn.store(email_id.encode(), "+FLAGS", "\\Deleted")
            conn.expunge()
            return {
                "status": "moved_to_trash",
                "email_id": email_id,
                "folder": folder,
                "trash_folder": decode_folder_name(trash_folder),
            }

        # Already in Trash — permanent delete
        status, _ = conn.store(email_id.encode(), "+FLAGS", "\\Deleted")
        if status != "OK":
            raise Exception("Failed to delete email")
        conn.expunge()
        return {
            "status": "deleted_permanently",
            "email_id": email_id,
            "folder": folder,
        }


if __name__ == "__main__":
    mcp.run(transport="stdio")
