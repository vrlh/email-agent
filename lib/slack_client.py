"""Slack Web API wrapper — send DMs, update messages, Block Kit formatting."""

import os
from typing import Any, Dict, List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from lib.models import Email, EmailCategory, EmailPriority, TriageDecision


def _get_client() -> WebClient:
    return WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def _owner_dm_channel() -> str:
    """Open (or retrieve) the DM channel with the owner."""
    client = _get_client()
    resp = client.conversations_open(
        users=[os.environ["OWNER_SLACK_USER_ID"]]
    )
    return resp["channel"]["id"]


# ---------------------------------------------------------------------------
# Core messaging
# ---------------------------------------------------------------------------


def send_dm(
    text: str,
    blocks: Optional[List[Dict[str, Any]]] = None,
    channel: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> str:
    """Send a DM to the owner.  Returns the message timestamp (ts).

    If *channel* is provided, posts there directly.  Otherwise uses
    conversations_open to find/create the DM channel.
    If *thread_ts* is provided, replies in that thread.
    """
    client = _get_client()
    ch = channel or _owner_dm_channel()
    kwargs: Dict[str, Any] = {"channel": ch, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = client.chat_postMessage(**kwargs)
    return resp["ts"]


def update_message(
    ts: str,
    text: str,
    blocks: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Update an existing Slack message by its timestamp."""
    client = _get_client()
    channel = _owner_dm_channel()
    client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)


def reply_in_thread(
    thread_ts: str,
    text: str,
    blocks: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Reply in a thread.  Returns the new message ts."""
    client = _get_client()
    channel = _owner_dm_channel()
    resp = client.chat_postMessage(
        channel=channel, text=text, blocks=blocks, thread_ts=thread_ts,
    )
    return resp["ts"]


# ---------------------------------------------------------------------------
# Block Kit builders
# ---------------------------------------------------------------------------


_PRIORITY_EMOJI = {
    "urgent": "\U0001f534",   # red circle
    "high": "\U0001f7e0",     # orange circle
    "normal": "\U0001f4e7",   # envelope
    "low": "\u26aa",          # white circle
}

_CATEGORY_LABEL = {
    "primary": "",
    "social": " [Social]",
    "promotions": " [Promo]",
    "updates": " [Update]",
    "forums": " [Forum]",
    "spam": " [Spam]",
}


def build_email_notification_blocks(
    email: Email,
    account_email: str,
    summary: str,
    suggested_action: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build Block Kit blocks for a single email notification DM."""
    emoji = _PRIORITY_EMOJI.get(email.priority.value, "\U0001f4e7")
    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{account_email}* \u2014 New email needing attention",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:* {email.sender}"},
                {"type": "mrkdwn", "text": f"*Priority:* {email.priority.value.title()}"},
                {"type": "mrkdwn", "text": f"*Subject:* {email.subject}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"> {summary}",
            },
        },
    ]

    if suggested_action:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"\U0001f4a1 *Suggested action:* {suggested_action}"},
            ],
        })

    return blocks


def build_email_list_blocks(
    emails: List[Email],
    account_emails: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Build Block Kit blocks for a numbered email list.

    *account_emails* maps account_id -> email address for display.
    """
    if not emails:
        return [_text_block("\U0001f4ed No emails found.")]

    blocks: List[Dict[str, Any]] = [
        _text_block(f"\U0001f4ec *{len(emails)} email(s):*"),
    ]

    lines = []
    for i, e in enumerate(emails, 1):
        emoji = _PRIORITY_EMOJI.get(e.priority.value, "\U0001f4e7")
        cat_label = _CATEGORY_LABEL.get(e.category.value, "")
        acct = account_emails.get(e.account_id, "")
        acct_tag = f" _({acct})_" if acct else ""
        line = f"{emoji} *#{i}* {e.subject}{cat_label}{acct_tag} \u2014 {e.sender.email}"
        lines.append(line)

    # Slack blocks have a 3000-char text limit; chunk if needed
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 2900:
            blocks.append(_text_block(chunk))
            chunk = ""
        chunk += line + "\n"
    if chunk:
        blocks.append(_text_block(chunk))

    return blocks


def build_draft_review_blocks(
    draft_id: str,
    account_email: str,
    to: str,
    subject: str,
    body: str,
) -> List[Dict[str, Any]]:
    """Build Block Kit blocks for a draft review message with Send/Cancel buttons."""
    body_preview = body[:1500]
    return [
        _text_block(f"\u270f\ufe0f *Draft reply* from {account_email}"),
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*To:* {to}"},
                {"type": "mrkdwn", "text": f"*Subject:* {subject}"},
            ],
        },
        _text_block(f"```\n{body_preview}\n```"),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u2705 Send"},
                    "style": "primary",
                    "action_id": "confirm_send",
                    "value": draft_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u274c Cancel"},
                    "style": "danger",
                    "action_id": "cancel_draft",
                    "value": draft_id,
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": 'Type "edit: <instruction>" to modify, or click a button.',
                },
            ],
        },
    ]


def build_sent_confirmation_blocks(
    account_email: str, to: str, subject: str,
) -> List[Dict[str, Any]]:
    """Blocks shown after a draft is sent."""
    return [
        _text_block(
            f"\u2705 *Email sent* from {account_email}\n"
            f"*To:* {to}\n*Subject:* {subject}"
        ),
    ]


def build_cancelled_blocks() -> List[Dict[str, Any]]:
    return [_text_block("\u274c Draft cancelled.")]


def build_expired_blocks() -> List[Dict[str, Any]]:
    return [_text_block("\u23f0 Draft expired (1 hour limit).")]


def build_summary_blocks(email: Email, summary: str) -> List[Dict[str, Any]]:
    """Blocks for an email summary response."""
    return [
        _text_block(
            f"\U0001f4cb *Summary: {email.subject}*\n"
            f"From: {email.sender} \u2014 {email.date.strftime('%b %d, %H:%M')}\n\n"
            f"{summary}"
        ),
    ]


def build_status_blocks(
    accounts: List[Dict[str, Any]],
    pending_draft: bool,
) -> List[Dict[str, Any]]:
    """Blocks for a status response."""
    lines = ["\U0001f4ca *Status*\n"]
    for acct in accounts:
        sync = acct.get("last_sync", "never")
        lines.append(f"\u2022 *{acct['email']}* \u2014 last sync: {sync}")
    lines.append(f"\nPending draft: {'Yes' if pending_draft else 'None'}")
    return [_text_block("\n".join(lines))]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_block(text: str) -> Dict[str, Any]:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }
