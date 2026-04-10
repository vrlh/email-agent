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


# Request-scoped globals. Safe for single-tenant on Vercel (one user, sequential
# Slack messages). Would need refactoring for multi-tenant or concurrent access.
_reply_channel: str = ""
_reply_thread_ts: str = ""
_last_displayed_emails: list = []


def _reply(text: str, blocks=None) -> str:
    """Send a threaded reply to the user's message."""
    from lib.slack_client import send_dm
    return send_dm(text, blocks=blocks, channel=_reply_channel, thread_ts=_reply_thread_ts)


def _process_command(text: str, channel: str = "", thread_ts: str = ""):
    """Run the tool-calling agent to handle the user's message."""
    global _reply_channel, _reply_thread_ts
    _reply_channel = channel
    _reply_thread_ts = thread_ts

    # Quick keyword shortcuts (no LLM cost)
    text_lower = text.strip().lower()
    if text_lower == "help":
        _cmd_help({})
        return

    try:
        from lib.agent import run_agent
        context = _build_context()

        # If in a thread, fetch conversation history for context
        thread_history = _get_thread_history(channel, thread_ts)
        if thread_history:
            context = f"{context}\n\nConversation so far:\n{thread_history}"

        response = run_agent(text, context, _execute_tool)
        if response:
            _reply(response)
    except Exception as exc:
        logger.error(f"Agent failed: {exc}")
        _reply(f"\u26a0\ufe0f Agent error: {exc}")




def _get_thread_history(channel: str, thread_ts: str) -> str:
    """Fetch previous messages in a Slack thread for conversation context."""
    if not channel or not thread_ts:
        return ""

    try:
        from lib.slack_client import _get_client
        client = _get_client()
        resp = client.conversations_replies(
            channel=channel, ts=thread_ts, limit=10,
        )
        messages = resp.get("messages", [])
        if len(messages) <= 1:
            return ""  # No thread history (just the original message)

        lines = []
        for msg in messages[:-1]:  # Exclude the current message (already in user_text)
            who = "Bot" if msg.get("bot_id") else "User"
            text = msg.get("text", "")[:500]
            lines.append(f"{who}: {text}")

        return "\n".join(lines)
    except Exception:
        return ""


def _build_context() -> str:
    """Build a context string with last-displayed emails so Claude can resolve #N refs."""
    from lib.db import get_recent_emails, get_pending_draft

    emails = _last_displayed_emails if _last_displayed_emails else get_recent_emails(limit=50)
    draft = get_pending_draft()

    lines = []
    for i, e in enumerate(emails, 1):
        lines.append(f"#{i}: id={e.id} from={e.sender_email} subject=\"{e.subject}\" account={e.account_id}")

    if draft:
        lines.append(f"\nPending draft: id={draft.id} status={draft.status} subject=\"{draft.subject}\"")

    return "\n".join(lines) if lines else ""


def _execute_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call from the agent. Returns result as a string."""
    executors = {
        "list_emails": _tool_list_emails,
        "get_needs_reply": _tool_needs_reply,
        "summarize_email": _tool_summarize,
        "reply_to_email": _tool_reply,
        "dismiss_emails": _tool_dismiss,
        "send_draft": _tool_send,
        "cancel_draft": _tool_cancel,
        "edit_draft": _tool_edit,
        "create_rule": _tool_create_rule,
        "delete_rule": _tool_delete_rule,
        "list_rules": _tool_list_rules,
        "get_status": _tool_status,
        "onboard": _tool_onboard,
        "check_reply_status": _tool_check_reply_status,
    }
    executor = executors.get(tool_name)
    if not executor:
        return f"Unknown tool: {tool_name}"
    return executor(tool_input)


# ======================================================================
# Tool executors (called by agent, return strings)
# ======================================================================


def _tool_list_emails(params: dict) -> str:
    from lib.db import get_active_accounts, get_attention_emails, get_emails_for_account

    accounts = get_active_accounts()
    if not accounts:
        return "No Gmail accounts connected."

    filter_type = params.get("filter", "attention")
    show_all = filter_type == "all"
    unread_only = filter_type == "unread"

    all_emails = []
    account_map = {}
    for acct in accounts:
        account_map[acct.id] = acct.email_address
        if show_all:
            emails = get_emails_for_account(acct.id, limit=50)
        else:
            emails = get_attention_emails(acct.id, unread_only=unread_only, limit=50)
        all_emails.extend(emails)

    all_emails.sort(key=lambda e: e.date, reverse=True)
    all_emails = all_emails[:50]

    global _last_displayed_emails
    _last_displayed_emails = all_emails

    if not all_emails:
        return "No emails found."

    # Build display and send via Slack blocks
    from lib.models import Email, EmailAddress, EmailCategory, EmailPriority
    from lib.slack_client import build_email_list_blocks

    model_emails = []
    for e in all_emails:
        model_emails.append(Email(
            id=e.id, account_id=e.account_id, message_id=e.message_id or e.id,
            subject=e.subject, sender=EmailAddress(email=e.sender_email, name=e.sender_name),
            date=e.date, is_read=e.is_read,
            category=EmailCategory(e.category) if e.category else EmailCategory.PRIMARY,
            priority=EmailPriority(e.priority) if e.priority else EmailPriority.NORMAL,
        ))

    blocks = build_email_list_blocks(model_emails, account_map)
    _reply(f"{len(model_emails)} email(s)", blocks=blocks)
    return f"[Already displayed to user] {len(model_emails)} emails shown."


def _tool_needs_reply(params: dict) -> str:
    from lib.db import get_needs_reply_emails, get_active_accounts, mark_email_replied
    from lib.gmail import check_thread_replied, credentials_from_encrypted, refresh_if_needed
    from datetime import datetime, timezone

    emails = get_needs_reply_emails()
    if not emails:
        return "No emails need a reply right now."

    # Live-check Gmail for replies before showing the list
    accounts_map = {a.id: a for a in get_active_accounts()}
    verified_emails = []
    for e in emails:
        acct = accounts_map.get(e.account_id)
        if acct and e.thread_id:
            try:
                creds = credentials_from_encrypted(acct.encrypted_tokens)
                creds, _ = refresh_if_needed(creds)
                if check_thread_replied(creds, e.thread_id, acct.email_address, after_msg_id=e.id):
                    mark_email_replied(e.id)
                    continue  # Skip — already replied
            except Exception:
                pass  # If check fails, keep it in the list to be safe
        verified_emails.append(e)

    emails = verified_emails
    if not emails:
        return "No emails need a reply right now. (Some were cleared — you already replied!)"

    global _last_displayed_emails
    _last_displayed_emails = emails

    accounts = {a.id: a.email_address for a in accounts_map.values()}
    lines = []
    for i, e in enumerate(emails, 1):
        acct = accounts.get(e.account_id, "")
        acct_tag = f" ({acct})" if acct else ""
        age = ""
        if e.date:
            hours = (datetime.now(timezone.utc) - e.date.replace(
                tzinfo=timezone.utc if e.date.tzinfo is None else e.date.tzinfo
            )).total_seconds() / 3600
            if hours < 24:
                age = f" ({int(hours)}h ago)"
            else:
                age = f" ({int(hours / 24)}d ago)"
        lines.append(f"#{i} {e.subject} — {e.sender_email}{acct_tag}{age}")

    _reply(f"\u23f3 *{len(emails)} email(s) need your reply:*\n\n" + "\n".join(
        f"\u2022 *#{i+1}* {lines[i].split(' ', 1)[1]}" for i in range(len(lines))
    ))
    return f"[Already displayed to user] {len(emails)} emails need reply."


def _tool_summarize(params: dict) -> str:
    from lib.llm import summarize_email
    from lib.slack_client import build_summary_blocks

    email_orm, model_email = _resolve_email_ref(params.get("ref", "#1"))
    if not email_orm:
        return "Couldn't find that email."

    summary = summarize_email(model_email)
    blocks = build_summary_blocks(model_email, summary)
    _reply(f"Summary: {model_email.subject}", blocks=blocks)
    return f"[Already displayed to user] Summary of '{model_email.subject}'"


def _tool_reply(params: dict) -> str:
    from lib.llm import generate_draft
    from lib.db import create_pending_draft, get_active_accounts, update_draft_slack_ts
    from lib.slack_client import build_draft_review_blocks

    email_orm, model_email = _resolve_email_ref(params.get("ref", "#1"))
    if not email_orm:
        return "Couldn't find that email."

    message = params.get("message", "")
    if not message:
        return "No reply message provided."

    accounts = get_active_accounts()
    account_email = ""
    for a in accounts:
        if a.id == email_orm.account_id:
            account_email = a.email_address
            break

    draft_content = generate_draft(model_email, message, account_email)
    to_list = [{"email": addr.email, "name": addr.name} for addr in draft_content.to_addresses]
    draft = create_pending_draft(
        account_id=email_orm.account_id, to_addresses=to_list,
        subject=draft_content.subject, body_text=draft_content.body_text,
        reply_to_email_id=email_orm.id, thread_id=email_orm.thread_id,
    )

    blocks = build_draft_review_blocks(
        draft_id=draft.id, account_email=account_email,
        to=str(model_email.sender), subject=draft_content.subject,
        body=draft_content.body_text,
    )
    ts = _reply(f"Draft reply to {model_email.sender.email}", blocks=blocks)
    update_draft_slack_ts(draft.id, ts)
    return f"Draft created for reply to {model_email.sender.email}. Showing Send/Cancel buttons."


def _tool_dismiss(params: dict) -> str:
    from lib.db import get_active_accounts, get_email_by_id, mark_email_replied
    from lib.gmail import mark_read, credentials_from_encrypted, refresh_if_needed

    refs = params.get("refs", [])
    if not refs:
        return "No email references provided."

    email_ids = _resolve_refs_to_ids(refs)
    if not email_ids:
        return "Couldn't find those emails."

    accounts = {a.id: a for a in get_active_accounts()}
    dismissed = 0
    for eid in email_ids:
        email_orm = get_email_by_id(eid)
        if not email_orm:
            continue
        acct = accounts.get(email_orm.account_id)
        if not acct:
            continue
        creds = credentials_from_encrypted(acct.encrypted_tokens)
        creds, _ = refresh_if_needed(creds)
        mark_read(creds, eid)
        mark_email_replied(eid)
        dismissed += 1

    _reply(f"\u2705 Dismissed {dismissed} email(s) — marked as read.")
    return f"Dismissed {dismissed} email(s)."


def _tool_send(params: dict) -> str:
    from lib.db import get_pending_draft
    draft = get_pending_draft()
    if not draft:
        return "No pending draft to send."
    _handle_send(draft.id)
    return "Draft sent."


def _tool_cancel(params: dict) -> str:
    from lib.db import get_pending_draft
    draft = get_pending_draft()
    if not draft:
        return "No pending draft to cancel."
    _handle_cancel(draft.id)
    return "Draft cancelled."


def _tool_edit(params: dict) -> str:
    from lib.llm import edit_draft as llm_edit
    from lib.db import get_pending_draft, update_draft_body, get_active_accounts
    from lib.slack_client import build_draft_review_blocks

    draft = get_pending_draft()
    if not draft:
        return "No pending draft to edit."

    instruction = params.get("instruction", "")
    if not instruction:
        return "No edit instruction provided."

    new_body = llm_edit(draft.body_text, instruction)
    update_draft_body(draft.id, new_body)

    accounts = {a.id: a for a in get_active_accounts()}
    acct = accounts.get(draft.account_id)
    account_email = acct.email_address if acct else ""
    to_display = draft.to_addresses[0].get("email", "") if draft.to_addresses else ""

    blocks = build_draft_review_blocks(
        draft_id=draft.id, account_email=account_email,
        to=to_display, subject=draft.subject, body=new_body,
    )
    _reply("Updated draft", blocks=blocks)
    return "Draft updated."


def _tool_create_rule(params: dict) -> str:
    from lib.db import create_user_rule

    rule_type = params.get("rule_type", "ignore")
    field = params.get("field", "sender_domain")
    operator = params.get("operator", "contains")
    value = params.get("value", "")

    if not value:
        return "No value provided for the rule."

    create_user_rule(rule_type=rule_type, field=field, operator=operator, value=value,
                     action="auto_archive" if rule_type == "ignore" else "boost")

    emoji = "\U0001f6ab" if rule_type == "ignore" else "\u2b50"
    _reply(f"{emoji} Rule created: {rule_type} emails where {field} {operator} `{value}`")
    return f"Rule created: {rule_type} {field} {operator} {value}"


def _tool_delete_rule(params: dict) -> str:
    from lib.db import get_user_rules, delete_user_rule

    ref = params.get("ref", "")
    if not ref:
        return "No rule reference provided."

    try:
        idx = int(ref.replace("#", "")) - 1
        rules = get_user_rules()
        if 0 <= idx < len(rules):
            rule = rules[idx]
            delete_user_rule(rule.id)
            _reply(f"\U0001f5d1\ufe0f Deleted rule: {rule.rule_type} {rule.field} {rule.operator} `{rule.value}`")
            return f"Deleted rule: {rule.rule_type} {rule.field} {rule.operator} {rule.value}"
        return f"Rule #{idx + 1} not found."
    except ValueError:
        return "Invalid rule reference."


def _tool_list_rules(params: dict) -> str:
    from lib.db import get_user_rules
    from lib.slack_client import build_rules_list_blocks

    rules = get_user_rules()
    blocks = build_rules_list_blocks(rules)
    _reply("Your rules", blocks=blocks)
    return f"[Already displayed to user] {len(rules)} rule(s) configured."


def _tool_status(params: dict) -> str:
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
    return f"[Already displayed to user] {len(accounts)} account(s) connected."


def _tool_onboard(params: dict) -> str:
    force = params.get("force", False)
    if force:
        _reply("\U0001f4e5 Force onboarding (re-triaging ALL emails)...")
    else:
        _reply("\U0001f4e5 Onboarding (triaging new/untriaged emails)...")
    from lib.onboard import run_onboard
    result = run_onboard(force=force)
    return f"Onboard complete. {result.get('total_needs_reply', 0)} email(s) need reply."


def _tool_check_reply_status(params: dict) -> str:
    """Check Gmail thread to see if an email has been replied to, and update DB."""
    from lib.db import get_active_accounts, mark_email_replied
    from lib.gmail import check_thread_replied, credentials_from_encrypted, refresh_if_needed

    email_orm, model_email = _resolve_email_ref(params.get("ref", "#1"))
    if not email_orm:
        return "Couldn't find that email."

    if not email_orm.thread_id:
        return f"Email '{email_orm.subject}' has no thread ID — can't check replies."

    accounts = {a.id: a for a in get_active_accounts()}
    acct = accounts.get(email_orm.account_id)
    if not acct:
        return "Account not found."

    creds = credentials_from_encrypted(acct.encrypted_tokens)
    creds, _ = refresh_if_needed(creds)

    replied = check_thread_replied(creds, email_orm.thread_id, acct.email_address, after_msg_id=email_orm.id)

    if replied:
        mark_email_replied(email_orm.id)
        _reply(f"\u2705 '{email_orm.subject}' — you already replied. Cleared from needs-reply.")
        return f"Email '{email_orm.subject}' has been replied to. Marked as replied in DB."
    else:
        return f"Email '{email_orm.subject}' — no reply found in the Gmail thread."


# ======================================================================
# Help command (free, no LLM needed)
# ======================================================================


def _cmd_help(params: dict):
    import os
    setup_secret = os.environ.get("SETUP_SECRET", "")
    app_url = os.environ.get("APP_URL", "https://email-agent-fawn.vercel.app").rstrip("/")
    add_url = f"{app_url}/api/auth/gmail_start?secret={setup_secret}"

    _reply(
        "*Just talk to me naturally! Examples:*\n"
        "\u2022 \"what needs my attention?\"\n"
        "\u2022 \"needs reply\"\n"
        "\u2022 \"summarize the email from Sarah\"\n"
        "\u2022 \"reply to #1 saying I'll be there Thursday\"\n"
        "\u2022 \"dismiss #2\"\n"
        "\u2022 \"ignore emails from linkedin.com\"\n"
        "\u2022 \"rules\" / \"status\" / \"onboard\"\n\n"
        f"\U0001f4e7 *<{add_url}|Add another Gmail account>*"
    )


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
    """Resolve a reference like '#3' to an (EmailORM, Email) tuple.

    Uses the last-displayed email list so #N matches what the user saw.
    """
    from lib.db import get_email_by_id

    if not ref:
        return None, None

    # Try #N index into last-displayed emails
    if ref.startswith("#"):
        try:
            idx = int(ref[1:]) - 1
            if _last_displayed_emails and 0 <= idx < len(_last_displayed_emails):
                orm = _last_displayed_emails[idx]
                model = _orm_to_model(orm)
                return orm, model
            # Fallback to recent emails if no list was displayed
            from lib.db import get_recent_emails
            recent = get_recent_emails(limit=50)
            if 0 <= idx < len(recent):
                orm = recent[idx]
                return orm, _orm_to_model(orm)
        except (ValueError, IndexError):
            pass

    # Try direct ID lookup
    orm = get_email_by_id(ref)
    if orm:
        return orm, _orm_to_model(orm)

    return None, None


def _resolve_refs_to_ids(refs: list) -> list:
    """Resolve a list of references to email IDs."""
    ids = []
    for ref in refs:
        if isinstance(ref, str) and ref.startswith("#"):
            try:
                idx = int(ref[1:]) - 1
                if _last_displayed_emails and 0 <= idx < len(_last_displayed_emails):
                    ids.append(_last_displayed_emails[idx].id)
                else:
                    from lib.db import get_recent_emails
                    recent = get_recent_emails(limit=50)
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
