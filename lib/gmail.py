"""Gmail API client — multi-account, read + write operations.

All functions accept a Google ``Credentials`` object so the caller controls
which account is used.  Helper functions for parsing Gmail payloads are
cherry-picked from the original codebase and converted to standalone functions.
"""

import base64
import json
import email as email_lib
import email.utils
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from lib.crypto import decrypt_tokens, encrypt_tokens
from lib.models import Email, EmailAddress, EmailAttachment, EmailCategory


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def credentials_from_encrypted(encrypted_tokens: str) -> Credentials:
    """Build ``google.oauth2.credentials.Credentials`` from an encrypted JSON blob."""
    token_json = decrypt_tokens(encrypted_tokens)
    token_data = json.loads(token_json)
    return Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )


def refresh_if_needed(creds: Credentials) -> Tuple[Credentials, bool]:
    """Refresh the access token if expired.  Returns (creds, was_refreshed)."""
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        return creds, True
    return creds, False


def credentials_to_encrypted(creds: Credentials) -> str:
    """Serialize credentials back to an encrypted JSON blob for DB storage."""
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else None,
    }
    return encrypt_tokens(json.dumps(token_data))


def _build_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Fetch emails
# ---------------------------------------------------------------------------


def fetch_new_emails(
    creds: Credentials,
    account_id: str,
    last_history_id: Optional[str] = None,
    max_results: int = 50,
) -> Tuple[List[Email], Optional[str]]:
    """Fetch new emails for an account.

    If *last_history_id* is provided, uses the Gmail history API for an
    incremental delta.  Otherwise falls back to ``messages.list`` for the
    most recent batch.

    Returns ``(emails, new_history_id)``.
    """
    service = _build_service(creds)

    if last_history_id:
        return _fetch_via_history(service, account_id, last_history_id, max_results)
    return _fetch_recent(service, account_id, max_results)


def _fetch_via_history(
    service, account_id: str, history_id: str, max_results: int
) -> Tuple[List[Email], Optional[str]]:
    """Use history.list to get only messages added since *history_id*."""
    try:
        resp = (
            service.users()
            .history()
            .list(userId="me", startHistoryId=history_id, historyTypes=["messageAdded"])
            .execute()
        )
    except HttpError as exc:
        if exc.resp.status in (404, 500):
            # History ID expired or backend error — fall back to recent fetch
            return _fetch_recent(service, account_id, max_results)
        raise

    new_history_id = resp.get("historyId", history_id)
    message_ids: set[str] = set()
    for record in resp.get("history", []):
        for added in record.get("messagesAdded", []):
            message_ids.add(added["message"]["id"])

    emails = _fetch_messages_by_ids(service, account_id, list(message_ids)[:max_results])
    return emails, new_history_id


def _fetch_recent(
    service, account_id: str, max_results: int
) -> Tuple[List[Email], Optional[str]]:
    """Fetch the most recent messages via messages.list."""
    resp = (
        service.users()
        .messages()
        .list(userId="me", maxResults=max_results, labelIds=["INBOX"])
        .execute()
    )
    message_ids = [m["id"] for m in resp.get("messages", [])]
    emails = _fetch_messages_by_ids(service, account_id, message_ids)

    # Get current history ID for future incremental syncs
    try:
        profile = service.users().getProfile(userId="me").execute()
        history_id = str(profile.get("historyId", ""))
    except HttpError:
        history_id = None

    return emails, history_id


def _fetch_messages_by_ids(
    service, account_id: str, message_ids: List[str]
) -> List[Email]:
    import time
    emails: List[Email] = []
    retries = 0
    max_retries = 3
    i = 0
    while i < len(message_ids):
        mid = message_ids[i]
        try:
            raw = (
                service.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
            emails.append(_parse_message(raw, account_id))
            retries = 0
            i += 1
        except HttpError as exc:
            if exc.resp.status == 429 and retries < max_retries:
                retries += 1
                time.sleep(2 * retries)
                continue  # retry same message
            i += 1  # skip on non-429 or max retries exceeded
    return emails


# ---------------------------------------------------------------------------
# Send / Reply / Draft
# ---------------------------------------------------------------------------


def send_reply(
    creds: Credentials,
    to: str,
    subject: str,
    body_text: str,
    thread_id: str,
    in_reply_to_message_id: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> str:
    """Send a threaded reply.  Returns the new message ID."""
    service = _build_service(creds)

    msg = MIMEText(body_text)
    msg["To"] = to
    msg["Subject"] = subject
    msg["In-Reply-To"] = in_reply_to_message_id
    msg["References"] = in_reply_to_message_id
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        # Gmail honors Bcc from raw MIME and strips the header on delivery.
        msg["Bcc"] = ", ".join(bcc)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw, "threadId": thread_id})
            .execute()
        )
        return sent["id"]
    except HttpError as exc:
        raise RuntimeError(f"Gmail send failed: {exc.resp.status} {exc.reason}") from exc


def send_new_email(
    creds: Credentials,
    to: str,
    subject: str,
    body_text: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> str:
    """Send a brand-new email (not a reply).  Returns the new message ID."""
    service = _build_service(creds)

    msg = MIMEText(body_text)
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return sent["id"]
    except HttpError as exc:
        raise RuntimeError(f"Gmail send failed: {exc.resp.status} {exc.reason}") from exc


def create_gmail_draft(
    creds: Credentials,
    to: str,
    subject: str,
    body_text: str,
    thread_id: Optional[str] = None,
    in_reply_to_message_id: Optional[str] = None,
) -> str:
    """Create a draft in Gmail.  Returns the draft ID."""
    service = _build_service(creds)

    msg = MIMEText(body_text)
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to_message_id:
        msg["In-Reply-To"] = in_reply_to_message_id
        msg["References"] = in_reply_to_message_id

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body: Dict[str, Any] = {"message": {"raw": raw}}
    if thread_id:
        body["message"]["threadId"] = thread_id

    try:
        draft = service.users().drafts().create(userId="me", body=body).execute()
        return draft["id"]
    except HttpError as exc:
        raise RuntimeError(f"Gmail draft failed: {exc.resp.status} {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Reply detection
# ---------------------------------------------------------------------------


def check_thread_replied(
    creds: Credentials, thread_id: str, account_email: str, after_msg_id: str = ""
) -> bool:
    """Check if the account owner has already handled this email thread.

    Returns True if:
    - The email being checked is FROM the owner (they sent it, ball is in other court)
    - The owner sent a real reply AFTER the email in question
    Skips calendar auto-responses.
    """
    service = _build_service(creds)
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "Subject", "Content-Type"],
        ).execute()

        account_lower = account_email.lower()
        messages = thread.get("messages", [])

        # Find the email we're checking
        start_idx = 1
        if after_msg_id:
            for i, msg in enumerate(messages):
                if msg["id"] == after_msg_id:
                    # Check if this email itself is FROM the owner
                    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                    from_addr = headers.get("From", "").lower()
                    if account_lower in from_addr:
                        return True  # Owner sent this email — no reply needed

                    start_idx = i + 1
                    break

        # Check if owner sent a reply after this email
        for msg in messages[start_idx:]:
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_addr = headers.get("From", "").lower()

            if account_lower not in from_addr:
                continue

            # Skip calendar auto-responses
            subject = headers.get("Subject", "").lower()
            if any(kw in subject for kw in ["accepted", "declined", "tentative"]):
                continue

            content_type = headers.get("Content-Type", "").lower()
            if "text/calendar" in content_type:
                continue

            return True

        return False
    except HttpError:
        return False


def fetch_inbox_emails_since(
    creds: Credentials,
    account_id: str,
    since_days: int = 90,
    max_results: int = 500,
) -> Tuple[List[Email], Optional[str]]:
    """Fetch inbox emails from the last N days for onboarding/backfill."""
    import time
    service = _build_service(creds)

    since_epoch = int(time.time()) - (since_days * 86400)
    query = f"after:{since_epoch}"

    all_message_ids: List[str] = []
    page_token = None

    while len(all_message_ids) < max_results:
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                labelIds=["INBOX"],
                maxResults=min(100, max_results - len(all_message_ids)),
                pageToken=page_token,
            )
            .execute()
        )
        msgs = resp.get("messages", [])
        all_message_ids.extend(m["id"] for m in msgs)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    emails = _fetch_messages_by_ids(service, account_id, all_message_ids[:max_results])

    try:
        profile = service.users().getProfile(userId="me").execute()
        history_id = str(profile.get("historyId", ""))
    except HttpError:
        history_id = None

    return emails, history_id


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def archive_email(creds: Credentials, email_id: str) -> bool:
    service = _build_service(creds)
    try:
        service.users().messages().modify(
            userId="me", id=email_id, body={"removeLabelIds": ["INBOX"]}
        ).execute()
        return True
    except HttpError:
        return False


def mark_read(creds: Credentials, email_id: str) -> bool:
    service = _build_service(creds)
    try:
        service.users().messages().modify(
            userId="me", id=email_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return True
    except HttpError:
        return False


def archive_emails(creds: Credentials, email_ids: List[str]) -> int:
    """Archive multiple emails.  Returns number successfully archived."""
    return sum(1 for eid in email_ids if archive_email(creds, eid))


# ---------------------------------------------------------------------------
# Gmail message parsing (cherry-picked from original connector)
# ---------------------------------------------------------------------------


def _parse_message(message: Dict[str, Any], account_id: str) -> Email:
    headers = {
        h["name"]: h["value"] for h in message["payload"].get("headers", [])
    }

    message_id = headers.get("Message-ID", message["id"])
    thread_id = message.get("threadId")
    subject = headers.get("Subject", "(No Subject)")

    sender = _parse_email_address(headers.get("From", ""))
    recipients = _parse_email_addresses(headers.get("To", ""))
    cc = _parse_email_addresses(headers.get("Cc", ""))

    date_str = headers.get("Date")
    date = _parse_date(date_str) if date_str else datetime.now(timezone.utc)

    body_text, body_html, attachments = _extract_content(message["payload"])

    is_read = "UNREAD" not in message.get("labelIds", [])
    category = _infer_category(message.get("labelIds", []))

    return Email(
        id=message["id"],
        account_id=account_id,
        message_id=message_id,
        thread_id=thread_id,
        subject=subject,
        sender=sender,
        recipients=recipients,
        cc=cc,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        date=date,
        is_read=is_read,
        category=category,
        raw_headers=headers,
    )


def _parse_email_address(addr_str: str) -> EmailAddress:
    if not addr_str:
        return EmailAddress(email="", name=None)
    try:
        parsed = email_lib.utils.parseaddr(addr_str)
        name = parsed[0] if parsed[0] else None
        addr = parsed[1] if parsed[1] else addr_str
        return EmailAddress(email=addr, name=name)
    except Exception:
        return EmailAddress(email=addr_str, name=None)


def _parse_email_addresses(addrs_str: str) -> List[EmailAddress]:
    if not addrs_str:
        return []
    try:
        addresses = email_lib.utils.getaddresses([addrs_str])
        return [
            EmailAddress(email=a[1], name=a[0] if a[0] else None)
            for a in addresses
            if a[1]
        ]
    except Exception:
        return [EmailAddress(email=addrs_str, name=None)]


def _parse_date(date_str: str) -> datetime:
    try:
        return email_lib.utils.parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _extract_content(
    payload: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], List[EmailAttachment]]:
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    attachments: List[EmailAttachment] = []

    def process_part(part: Dict[str, Any]) -> None:
        nonlocal body_text, body_html

        mime_type = part.get("mimeType", "")

        if mime_type == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                body_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        elif mime_type == "text/html":
            data = part.get("body", {}).get("data")
            if data:
                body_html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        elif part.get("filename"):
            attachments.append(
                EmailAttachment(
                    filename=part["filename"],
                    content_type=mime_type,
                    size=part.get("body", {}).get("size", 0),
                )
            )

        for sub_part in part.get("parts", []):
            process_part(sub_part)

    process_part(payload)
    return body_text, body_html, attachments


def _infer_category(label_ids: List[str]) -> EmailCategory:
    if "CATEGORY_SOCIAL" in label_ids:
        return EmailCategory.SOCIAL
    elif "CATEGORY_PROMOTIONS" in label_ids:
        return EmailCategory.PROMOTIONS
    elif "CATEGORY_UPDATES" in label_ids:
        return EmailCategory.UPDATES
    elif "CATEGORY_FORUMS" in label_ids:
        return EmailCategory.FORUMS
    elif "SPAM" in label_ids:
        return EmailCategory.SPAM
    return EmailCategory.PRIMARY
