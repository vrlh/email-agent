"""POST /api/slack/events — Slack Events API + interactive actions.

Single entry point for all Slack communication:
  - URL verification challenge
  - DM message events (natural language commands)
  - Interactive button payloads (draft send / cancel)

Slack 3-second timeout strategy:
  Process synchronously.  If Slack retries (X-Slack-Retry-Num header),
  return 200 immediately — the first invocation posts results via the
  Web API independently.
"""

import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

from lib.security import is_owner, verify_slack_signature

logger = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # ── Retry dedup ──
        if self.headers.get("X-Slack-Retry-Num"):
            self._ok()
            return

        # ── Signature verification ──
        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")
        if not verify_slack_signature(timestamp, body, signature):
            self._respond(403, "Invalid signature")
            return

        # Route: interactive payload (button click) or event callback
        content_type = self.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" in content_type:
            self._handle_interactive(body)
        else:
            self._handle_event(body)

    # ------------------------------------------------------------------
    # Event callback (JSON body)
    # ------------------------------------------------------------------

    def _handle_event(self, body: bytes):
        data = json.loads(body)

        # URL verification challenge
        if data.get("type") == "url_verification":
            self._respond(200, json.dumps({"challenge": data["challenge"]}), "application/json")
            return

        if data.get("type") != "event_callback":
            self._ok()
            return

        event = data.get("event", {})

        # Ignore bot messages and non-message events
        if event.get("bot_id") or event.get("subtype"):
            self._ok()
            return

        # Owner check
        user_id = event.get("user", "")
        if not is_owner(user_id):
            self._ok()
            return

        text = event.get("text", "").strip()
        if not text:
            self._ok()
            return

        # Process the command — results are posted via Slack Web API
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        try:
            _process_command(text, channel, thread_ts)
        except Exception as exc:
            logger.error(f"Command processing error: {exc}")
            _send_error(str(exc), channel, thread_ts)

        self._ok()

    # ------------------------------------------------------------------
    # Interactive payload (button clicks)
    # ------------------------------------------------------------------

    def _handle_interactive(self, body: bytes):
        global _reply_channel, _reply_thread_ts
        form = parse_qs(body.decode())
        payload = json.loads(form.get("payload", ["{}"])[0])

        user_id = payload.get("user", {}).get("id", "")
        if not is_owner(user_id):
            self._ok()
            return

        # Set reply channel and thread from the interactive payload
        _reply_channel = payload.get("channel", {}).get("id", "")
        msg = payload.get("message", {})
        _reply_thread_ts = msg.get("thread_ts") or msg.get("ts", "")

        actions = payload.get("actions", [])
        if not actions:
            self._ok()
            return

        action = actions[0]
        action_id = action.get("action_id", "")
        draft_id = action.get("value", "")

        try:
            if action_id == "confirm_send":
                _handle_send(draft_id)
            elif action_id == "cancel_draft":
                _handle_cancel(draft_id)
        except Exception as exc:
            logger.error(f"Interactive action error: {exc}")
            _send_error(str(exc), _reply_channel)

        self._ok()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _ok(self):
        self._respond(200, "")

    def _respond(self, status: int, body: str, content_type: str = "text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body.encode())


# ======================================================================
# Command processing (runs after 200 is queued)
# ======================================================================

def _send_error(detail: str, channel: str = "", thread_ts: str = ""):
    from lib.slack_client import send_dm
    send_dm(f"\u26a0\ufe0f Something went wrong: {detail}", channel=channel, thread_ts=thread_ts)


# Simple keyword matching as fallback when LLM is unavailable
_KEYWORD_INTENTS = {
    "status": "status",
    "help": "help",
    "send": "send",
    "cancel": "cancel",
    "list": "list",
    "list unread": "list",
    "list all": "list",
    "rules": "list_rules",
    "list rules": "list_rules",
    "needs reply": "needs_reply",
    "list needs reply": "needs_reply",
    "what needs a reply": "needs_reply",
    "unreplied": "needs_reply",
    "onboard": "onboard",
    "scan my emails": "onboard",
    "scan emails": "onboard",
    "debug triage": "debug_triage",
}


_reply_channel: str = ""
_reply_thread_ts: str = ""


def _reply(text: str, blocks=None) -> str:
    """Send a threaded reply to the user's message."""
    from lib.slack_client import send_dm
    return send_dm(text, blocks=blocks, channel=_reply_channel, thread_ts=_reply_thread_ts)


def _process_command(text: str, channel: str = "", thread_ts: str = ""):
    """Parse intent and route to the appropriate handler."""
    global _reply_channel, _reply_thread_ts
    _reply_channel = channel
    _reply_thread_ts = thread_ts

    # Try simple keyword match first (free, instant, no LLM needed)
    text_lower = text.strip().lower()
    intent = _KEYWORD_INTENTS.get(text_lower)

    if not intent:
        # Fall back to LLM parsing for complex commands
        try:
            from lib.llm import parse_command
            context = _build_context()
            cmd = parse_command(text, context)
            intent = cmd.intent
            params = cmd.params
        except Exception as exc:
            logger.error(f"LLM parse failed: {exc}")
            _reply(
                f"\u26a0\ufe0f LLM parsing failed: {exc}\n\n"
                "Simple commands still work: status, list, help, send, cancel"
            )
            return
    else:
        params = {}

    routes = {
        "list": _cmd_list,
        "summarize": _cmd_summarize,
        "reply": _cmd_reply,
        "archive": _cmd_archive,
        "send": _cmd_send,
        "cancel": _cmd_cancel,
        "edit": _cmd_edit,
        "status": _cmd_status,
        "help": _cmd_help,
        "ignore": _cmd_ignore,
        "priority_sender": _cmd_priority_sender,
        "list_rules": _cmd_list_rules,
        "delete_rule": _cmd_delete_rule,
        "needs_reply": _cmd_needs_reply,
        "onboard": _cmd_onboard,
        "debug_triage": _cmd_debug_triage,
    }

    handler_fn = routes.get(intent)
    if handler_fn:
        handler_fn(params)
    else:
        _reply(
            "I didn't understand that. Try:\n"
            "\u2022 \"list unread\"\n"
            "\u2022 \"summarize #3\"\n"
            "\u2022 \"reply to #1 saying ...\"\n"
            "\u2022 \"archive #2\"\n"
            "\u2022 \"status\""
        )




def _build_context() -> str:
    """Build a context string with recent emails so Claude can resolve #N refs."""
    from lib.db import get_recent_emails, get_pending_draft

    emails = get_recent_emails(limit=20)
    draft = get_pending_draft()

    lines = []
    for i, e in enumerate(emails, 1):
        lines.append(f"#{i}: id={e.id} from={e.sender_email} subject=\"{e.subject}\" account={e.account_id}")

    if draft:
        lines.append(f"\nPending draft: id={draft.id} status={draft.status} subject=\"{draft.subject}\"")

    return "\n".join(lines) if lines else ""


# ======================================================================
# Command handlers
# ======================================================================


def _cmd_list(params: dict):
    from lib.db import get_active_accounts, get_attention_emails, get_emails_for_account
    from lib.slack_client import build_email_list_blocks

    accounts = get_active_accounts()
    if not accounts:
        _reply("No Gmail accounts connected. Add one at /api/auth/gmail_start")
        return

    filter_type = params.get("filter", "unread")
    show_all = filter_type == "all"
    unread_only = filter_type in ("unread", "urgent")

    all_emails = []
    account_map = {}
    for acct in accounts:
        account_map[acct.id] = acct.email_address
        if show_all:
            emails = get_emails_for_account(acct.id, limit=50)
        else:
            emails = get_attention_emails(acct.id, unread_only=unread_only, limit=50)
        all_emails.extend(emails)

    # Sort by date descending
    all_emails.sort(key=lambda e: e.date, reverse=True)
    all_emails = all_emails[:50]

    # Convert ORM objects to model-like dicts for block builder
    from lib.models import Email, EmailAddress, EmailCategory, EmailPriority

    model_emails = []
    for e in all_emails:
        model_emails.append(Email(
            id=e.id,
            account_id=e.account_id,
            message_id=e.message_id or e.id,
            subject=e.subject,
            sender=EmailAddress(email=e.sender_email, name=e.sender_name),
            date=e.date,
            is_read=e.is_read,
            category=EmailCategory(e.category) if e.category else EmailCategory.PRIMARY,
            priority=EmailPriority(e.priority) if e.priority else EmailPriority.NORMAL,
        ))

    blocks = build_email_list_blocks(model_emails, account_map)
    _reply(f"{len(model_emails)} email(s)", blocks=blocks)


def _cmd_summarize(params: dict):
    from lib.llm import summarize_email
    from lib.slack_client import build_summary_blocks

    email, model_email = _resolve_email_ref(params.get("ref", "#1"))
    if not email:
        _reply("Couldn't find that email.")
        return

    summary = summarize_email(model_email)
    blocks = build_summary_blocks(model_email, summary)
    _reply(f"Summary: {model_email.subject}", blocks=blocks)


def _cmd_reply(params: dict):
    from lib.llm import generate_draft
    from lib.db import create_pending_draft, get_active_accounts
    from lib.slack_client import build_draft_review_blocks

    email_orm, model_email = _resolve_email_ref(params.get("ref", "#1"))
    if not email_orm:
        _reply("Couldn't find that email.")
        return

    message = params.get("message", "")
    if not message:
        _reply("What should the reply say? e.g. \"reply to #1 saying I'll be there\"")
        return

    # Find the account email
    accounts = get_active_accounts()
    account_email = ""
    for a in accounts:
        if a.id == email_orm.account_id:
            account_email = a.email_address
            break

    draft_content = generate_draft(model_email, message, account_email)

    # Store as pending draft
    to_list = [{"email": addr.email, "name": addr.name} for addr in draft_content.to_addresses]
    draft = create_pending_draft(
        account_id=email_orm.account_id,
        to_addresses=to_list,
        subject=draft_content.subject,
        body_text=draft_content.body_text,
        reply_to_email_id=email_orm.id,
        thread_id=email_orm.thread_id,
    )

    blocks = build_draft_review_blocks(
        draft_id=draft.id,
        account_email=account_email,
        to=str(model_email.sender),
        subject=draft_content.subject,
        body=draft_content.body_text,
    )
    ts = _reply(f"Draft reply to {model_email.sender.email}", blocks=blocks)

    # Store the Slack message ts so we can update it later
    from lib.db import update_draft_slack_ts
    update_draft_slack_ts(draft.id, ts)


def _cmd_archive(params: dict):
    from lib.db import get_active_accounts, get_email_by_id
    from lib.gmail import archive_email, credentials_from_encrypted, refresh_if_needed

    refs = params.get("refs", [])
    if isinstance(refs, str):
        refs = [refs]

    if not refs:
        _reply("Which email(s)? e.g. \"archive #2\" or \"archive #2 through #5\"")
        return

    email_ids = _resolve_refs_to_ids(refs)
    if not email_ids:
        _reply("Couldn't find those emails.")
        return

    accounts = {a.id: a for a in get_active_accounts()}
    archived = 0
    for eid in email_ids:
        email_orm = get_email_by_id(eid)
        if not email_orm:
            continue
        acct = accounts.get(email_orm.account_id)
        if not acct:
            continue
        creds = credentials_from_encrypted(acct.encrypted_tokens)
        creds, _ = refresh_if_needed(creds)
        if archive_email(creds, eid):
            archived += 1

    _reply(f"\U0001f5c4\ufe0f Archived {archived} email(s).")


def _cmd_send(params: dict):
    """Confirm and send the pending draft (also reachable via button)."""
    from lib.db import get_pending_draft
    draft = get_pending_draft()
    if not draft:
        _no_pending_draft()
        return
    _handle_send(draft.id)


def _cmd_cancel(params: dict):
    from lib.db import get_pending_draft
    draft = get_pending_draft()
    if not draft:
        _no_pending_draft()
        return
    _handle_cancel(draft.id)


def _cmd_edit(params: dict):
    from lib.llm import edit_draft as claude_edit
    from lib.db import get_pending_draft, update_draft_body
    from lib.slack_client import build_draft_review_blocks

    draft = get_pending_draft()
    if not draft:
        _no_pending_draft()
        return

    instruction = params.get("instruction", "")
    if not instruction:
        _reply("What should I change? e.g. \"edit: change Thursday to Friday\"")
        return

    new_body = claude_edit(draft.body_text, instruction)
    update_draft_body(draft.id, new_body)

    # Find account email
    from lib.db import get_active_accounts
    accounts = {a.id: a for a in get_active_accounts()}
    acct = accounts.get(draft.account_id)
    account_email = acct.email_address if acct else ""

    to_display = draft.to_addresses[0].get("email", "") if draft.to_addresses else ""

    blocks = build_draft_review_blocks(
        draft_id=draft.id,
        account_email=account_email,
        to=to_display,
        subject=draft.subject,
        body=new_body,
    )
    _reply(f"Updated draft", blocks=blocks)


def _cmd_status(params: dict):
    from lib.db import get_active_accounts, get_pending_draft
    from lib.slack_client import build_status_blocks

    accounts = get_active_accounts()
    acct_data = []
    for a in accounts:
        acct_data.append({
            "email": a.email_address,
            "last_sync": a.last_sync_at.strftime("%b %d, %H:%M") if a.last_sync_at else "never",
        })

    draft = get_pending_draft()
    blocks = build_status_blocks(acct_data, pending_draft=draft is not None)
    _reply("Status", blocks=blocks)


def _cmd_help(params: dict):
    import os
    setup_secret = os.environ.get("SETUP_SECRET", "")
    app_url = os.environ.get("APP_URL", "https://email-agent-fawn.vercel.app").rstrip("/")
    add_url = f"{app_url}/api/auth/gmail_start?secret={setup_secret}"

    _reply(
        "*Available commands:*\n"
        "\u2022 \"list\" / \"list unread\" / \"list all\"\n"
        "\u2022 \"needs reply\" \u2014 emails you owe a response to\n"
        "\u2022 \"summarize #3\" / \"summarize the email from Sarah\"\n"
        "\u2022 \"reply to #1 saying I'll be there Thursday\"\n"
        "\u2022 \"archive #2\" / \"archive #2 through #5\"\n"
        "\u2022 \"send\" \u2014 confirm pending draft\n"
        "\u2022 \"cancel\" \u2014 cancel pending draft\n"
        "\u2022 \"edit: change Thursday to Friday\" \u2014 modify pending draft\n"
        "\u2022 \"status\" \u2014 account info and sync times\n"
        "\u2022 \"onboard\" \u2014 scan last 3 months of emails\n"
        "\u2022 \"ignore emails from linkedin.com\" \u2014 auto-archive matching emails\n"
        "\u2022 \"always notify me about emails from boss@co.com\" \u2014 always surface\n"
        "\u2022 \"rules\" \u2014 list your rules\n"
        "\u2022 \"delete rule #2\" \u2014 remove a rule\n\n"
        f"\U0001f4e7 *<{add_url}|Add another Gmail account>*"
    )


def _cmd_ignore(params: dict):
    """Create an ignore rule: auto-archive emails matching a pattern."""
    from lib.db import create_user_rule

    field = params.get("field", "sender_domain")
    operator = params.get("operator", "contains")
    value = params.get("value", "")

    if not value:
        _reply("What should I ignore? e.g. \"ignore emails from linkedin.com\"")
        return

    rule = create_user_rule(
        rule_type="ignore",
        field=field,
        operator=operator,
        value=value,
        action="auto_archive",
    )
    _reply(f"\U0001f6ab Rule created: ignore emails where {field} {operator} `{value}`")


def _cmd_priority_sender(params: dict):
    """Create a priority rule: always notify for matching emails."""
    from lib.db import create_user_rule

    field = params.get("field", "sender")
    operator = params.get("operator", "contains")
    value = params.get("value", "")

    if not value:
        _reply("Who should I prioritize? e.g. \"always notify me about emails from boss@company.com\"")
        return

    rule = create_user_rule(
        rule_type="priority",
        field=field,
        operator=operator,
        value=value,
        action="boost",
    )
    _reply(f"\u2b50 Rule created: always notify when {field} {operator} `{value}`")


def _cmd_list_rules(params: dict):
    from lib.db import get_user_rules
    from lib.slack_client import build_rules_list_blocks

    rules = get_user_rules()
    blocks = build_rules_list_blocks(rules)
    _reply("Your rules", blocks=blocks)


def _cmd_delete_rule(params: dict):
    from lib.db import get_user_rules, delete_user_rule

    ref = params.get("ref", "")
    if not ref:
        _reply("Which rule? e.g. \"delete rule #2\"")
        return

    # Resolve #N to rule ID
    try:
        idx = int(ref.replace("#", "")) - 1
        rules = get_user_rules()
        if 0 <= idx < len(rules):
            rule = rules[idx]
            delete_user_rule(rule.id)
            _reply(f"\U0001f5d1\ufe0f Deleted rule: {rule.rule_type} {rule.field} {rule.operator} `{rule.value}`")
        else:
            _reply(f"Rule #{idx + 1} not found. Use \"rules\" to see your rules.")
    except ValueError:
        _reply("Use a number, e.g. \"delete rule #2\"")


def _cmd_needs_reply(params: dict):
    from lib.db import get_needs_reply_emails, get_active_accounts

    emails = get_needs_reply_emails()
    if not emails:
        _reply("\u2705 No emails need your reply right now.")
        return

    accounts = {a.id: a.email_address for a in get_active_accounts()}
    lines = [f"\u23f3 *{len(emails)} email(s) need your reply:*\n"]
    for i, e in enumerate(emails, 1):
        acct = accounts.get(e.account_id, "")
        acct_tag = f" _({acct})_" if acct else ""
        age = ""
        if e.date:
            from datetime import datetime, timezone
            hours = (datetime.now(timezone.utc) - e.date.replace(
                tzinfo=timezone.utc if e.date.tzinfo is None else e.date.tzinfo
            )).total_seconds() / 3600
            if hours < 24:
                age = f" ({int(hours)}h ago)"
            else:
                age = f" ({int(hours / 24)}d ago)"
        lines.append(f"\u2022 *#{i}* {e.subject} \u2014 {e.sender_email}{acct_tag}{age}")

    _reply("\n".join(lines))


def _cmd_onboard(params: dict):
    _reply("\U0001f4e5 Starting email onboard (scanning last 3 months). This may take a minute...")
    from lib.onboard import run_onboard
    run_onboard()


def _cmd_debug_triage(params: dict):
    """Debug: triage 5 recent primary emails and show raw LLM output."""
    import json
    from lib.db import get_active_accounts, get_untriaged_emails, reset_needs_reply
    from lib.models import Email, EmailAddress, EmailCategory
    from lib.providers._prompts import TRIAGE_SYSTEM, build_triage_user_prompt

    accounts = get_active_accounts()
    if not accounts:
        _reply("No accounts")
        return

    acct = accounts[0]
    reset_needs_reply(acct.id)

    from lib.db import get_session
    from lib.db_models import EmailORM
    from sqlalchemy import select
    with get_session() as session:
        stmt = (
            select(EmailORM)
            .where(EmailORM.account_id == acct.id)
            .where(EmailORM.category == "primary")
            .order_by(EmailORM.date.desc())
            .limit(5)
        )
        rows = list(session.execute(stmt).scalars().all())
        for r in rows:
            session.expunge(r)

    if not rows:
        _reply("No primary emails found")
        return

    # Build model emails
    model_emails = []
    for u in rows:
        model_emails.append(Email(
            id=u.id, account_id=u.account_id, message_id=u.message_id or u.id,
            subject=u.subject, sender=EmailAddress(email=u.sender_email, name=u.sender_name),
            body_text=u.body_text, date=u.date, is_read=u.is_read,
            category=EmailCategory(u.category) if u.category else EmailCategory.PRIMARY,
        ))

    # Show what we're sending to the LLM
    prompt = build_triage_user_prompt(model_emails)
    _reply(f"*Sending to LLM ({len(model_emails)} emails):*\n```\n{prompt[:2000]}\n```")

    # Call LLM and show raw response
    try:
        from lib.providers._prompts import strip_fences
        if os.environ.get("LLM_PROVIDER", "gemini") == "gemini":
            from lib.providers.gemini import _chat
        else:
            from lib.providers.claude import _chat
        raw = _chat(TRIAGE_SYSTEM, prompt)
        _reply(f"*Raw LLM response:*\n```\n{raw[:2000]}\n```")

        # Parse and show needs_reply values
        parsed = json.loads(strip_fences(raw))
        nr_summary = []
        for i, item in enumerate(parsed):
            nr = item.get("needs_reply", "MISSING")
            subj = model_emails[i].subject if i < len(model_emails) else "?"
            nr_summary.append(f"{'YES' if nr else 'no'} | {subj}")
        _reply("*needs_reply results:*\n" + "\n".join(nr_summary))
    except Exception as exc:
        _reply(f"*LLM error:* {exc}")


import os


# ======================================================================
# Interactive action handlers (button clicks)
# ======================================================================


def _handle_send(draft_id: str):
    from lib.db import get_active_accounts, update_draft_status
    from lib.gmail import credentials_from_encrypted, refresh_if_needed, send_reply, send_new_email
    from lib.slack_client import build_sent_confirmation_blocks, update_message

    from lib.db import get_session
    from lib.db_models import PendingDraftORM
    with get_session() as session:
        draft = session.get(PendingDraftORM, draft_id)
        if not draft or draft.status != "pending":
            _reply("No pending draft to send.")
            return

        # Copy values before leaving session
        d_account_id = draft.account_id
        d_reply_to = draft.reply_to_email_id
        d_thread_id = draft.thread_id
        d_subject = draft.subject
        d_body = draft.body_text
        d_to = draft.to_addresses
        d_slack_ts = draft.slack_message_ts

    accounts = {a.id: a for a in get_active_accounts()}
    acct = accounts.get(d_account_id)
    if not acct:
        _reply("Account not found.")
        return

    creds = credentials_from_encrypted(acct.encrypted_tokens)
    creds, _ = refresh_if_needed(creds)

    to_email = d_to[0].get("email", "") if d_to else ""

    if d_reply_to and d_thread_id:
        # Threaded reply — need the original message's Message-ID header
        from lib.db import get_email_by_id
        orig = get_email_by_id(d_reply_to)
        in_reply_to = orig.message_id if orig else d_reply_to
        send_reply(creds, to_email, d_subject, d_body, d_thread_id, in_reply_to)
    else:
        send_new_email(creds, to_email, d_subject, d_body)

    update_draft_status(draft_id, "sent")

    # Update the Slack message to show confirmation
    blocks = build_sent_confirmation_blocks(acct.email_address, to_email, d_subject)
    if d_slack_ts:
        try:
            update_message(d_slack_ts, f"Email sent to {to_email}", blocks=blocks)
        except Exception:
            _reply(f"\u2705 Email sent to {to_email}", blocks=blocks)
    else:
        _reply(f"\u2705 Email sent to {to_email}", blocks=blocks)


def _handle_cancel(draft_id: str):
    from lib.db import update_draft_status
    from lib.slack_client import build_cancelled_blocks, update_message

    from lib.db import get_session
    from lib.db_models import PendingDraftORM
    with get_session() as session:
        draft = session.get(PendingDraftORM, draft_id)
        if not draft or draft.status != "pending":
            _reply("No pending draft to cancel.")
            return
        slack_ts = draft.slack_message_ts

    update_draft_status(draft_id, "cancelled")

    blocks = build_cancelled_blocks()
    if slack_ts:
        try:
            update_message(slack_ts, "Draft cancelled.", blocks=blocks)
        except Exception:
            _reply("Draft cancelled.", blocks=blocks)
    else:
        _reply("Draft cancelled.", blocks=blocks)


# ======================================================================
# Helpers
# ======================================================================


def _no_pending_draft():
    _reply("No pending draft. Use \"reply to #N saying ...\" to create one.")


def _resolve_email_ref(ref: str):
    """Resolve a reference like '#3' to an (EmailORM, Email) tuple."""
    from lib.db import get_email_by_id, get_recent_emails
    from lib.models import Email, EmailAddress, EmailCategory, EmailPriority

    if not ref:
        return None, None

    # Try #N index into recent emails
    if ref.startswith("#"):
        try:
            idx = int(ref[1:]) - 1
            recent = get_recent_emails(limit=20)
            if 0 <= idx < len(recent):
                orm = recent[idx]
                model = _orm_to_model(orm)
                return orm, model
        except (ValueError, IndexError):
            pass

    # Try direct ID lookup
    orm = get_email_by_id(ref)
    if orm:
        return orm, _orm_to_model(orm)

    return None, None


def _resolve_refs_to_ids(refs: list) -> list:
    """Resolve a list of references to email IDs."""
    from lib.db import get_recent_emails

    recent = get_recent_emails(limit=20)
    ids = []
    for ref in refs:
        if isinstance(ref, str) and ref.startswith("#"):
            try:
                idx = int(ref[1:]) - 1
                if 0 <= idx < len(recent):
                    ids.append(recent[idx].id)
            except ValueError:
                pass
    return ids


def _orm_to_model(orm):
    """Convert an EmailORM to a Pydantic Email model."""
    from lib.models import Email, EmailAddress, EmailCategory, EmailPriority

    return Email(
        id=orm.id,
        account_id=orm.account_id,
        message_id=orm.message_id or orm.id,
        thread_id=orm.thread_id,
        subject=orm.subject,
        sender=EmailAddress(email=orm.sender_email, name=orm.sender_name),
        recipients=orm.recipients or [],
        cc=orm.cc or [],
        body_text=orm.body_text,
        body_html=orm.body_html,
        date=orm.date,
        is_read=orm.is_read,
        category=EmailCategory(orm.category) if orm.category else EmailCategory.PRIMARY,
        priority=EmailPriority(orm.priority) if orm.priority else EmailPriority.NORMAL,
        summary=orm.summary,
        raw_headers=orm.raw_headers or {},
    )
