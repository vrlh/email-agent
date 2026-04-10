"""GET /api/cron/check_emails — hourly email processing pipeline.

Triggered by an external cron service (cron-job.org) with a Bearer token.

Pipeline per account:
  1. Refresh OAuth credentials
  2. Incremental fetch via Gmail history API
  3. Rule-based categorization (builtin rules)
  4. Claude AI triage for PRIMARY emails (batch)
  5. Store in Postgres
  6. Send individual Slack DMs for emails needing attention
"""

import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from lib.security import verify_cron_secret

logger = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if not verify_cron_secret(auth):
            self._respond(403, {"error": "Forbidden"})
            return

        try:
            stats = _run_pipeline()
            self._respond(200, stats)
        except Exception as exc:
            logger.error(f"Cron pipeline failed: {exc}")
            self._respond(500, {"error": str(exc)})

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())


# ======================================================================
# Pipeline
# ======================================================================


def _run_pipeline() -> dict:
    from lib.db import (
        create_tables,
        expire_stale_drafts,
        get_active_accounts,
    )

    create_tables()

    # ── Expire stale drafts ──
    expired_count = expire_stale_drafts()
    if expired_count:
        _notify_expired_drafts(expired_count)

    # ── Process each account ──
    accounts = get_active_accounts()
    if not accounts:
        return {"status": "ok", "accounts": 0, "message": "No active accounts"}

    results = []
    total_new = 0
    all_attention_emails: dict = {}  # account_email -> list of email dicts
    total_archived = 0
    total_replies_synced = 0

    for account in accounts:
        result = _process_account(account)
        results.append(result)
        total_new += result.get("new", 0)
        total_replies_synced += result.get("replies_synced", 0)

        # Collect attention emails for summary
        attention = result.get("attention_emails", [])
        if attention:
            all_attention_emails[account.email_address] = attention
        total_archived += result.get("archived", 0)

    # ── Send single summary notification (includes needs-reply reminder) ──
    from lib.db import get_needs_reply_emails
    unreplied = get_needs_reply_emails()
    notified = _send_summary(all_attention_emails, total_archived, unreplied)

    return {
        "status": "ok",
        "accounts_processed": len(results),
        "total_new_emails": total_new,
        "total_notifications_sent": notified,
        "total_replies_synced": total_replies_synced,
        "drafts_expired": expired_count,
        "details": results,
    }


def _process_account(account) -> dict:
    """Fetch, triage, store, and notify for a single Gmail account."""
    from lib.db import (
        complete_sync_log,
        create_sync_log,
        update_account_sync,
        update_account_tokens,
        upsert_emails,
    )
    from lib.db_models import EmailORM
    from lib.gmail import (
        credentials_from_encrypted,
        credentials_to_encrypted,
        fetch_new_emails,
        refresh_if_needed,
    )
    from lib.models import EmailCategory
    from lib.rules.builtin import BuiltinRules
    from lib.rules.engine import RulesEngine

    log_id = create_sync_log(account.id)

    try:
        # ── 1. Credentials ──
        creds = credentials_from_encrypted(account.encrypted_tokens)
        creds, was_refreshed = refresh_if_needed(creds)
        if was_refreshed:
            update_account_tokens(account.id, credentials_to_encrypted(creds))

        # ── 2. Fetch ──
        emails, new_history_id = fetch_new_emails(
            creds, account.id, account.last_history_id, max_results=50,
        )

        if not emails:
            update_account_sync(account.id, new_history_id)
            complete_sync_log(log_id, 0, 0)
            return {"account": account.email_address, "fetched": 0, "new": 0, "notified": 0}

        # ── 3. Rule-based categorization ──
        engine = RulesEngine()
        engine.load_rules(BuiltinRules.get_all_rules())
        emails = engine.process_emails(emails)

        # ── 4. Claude AI triage (PRIMARY emails only to save cost) ──
        primary_emails = [e for e in emails if e.category == EmailCategory.PRIMARY]
        triage_map = {}

        if primary_emails:
            triage_map = _triage_with_fallback(primary_emails)

        # ── 5. Store ──
        orm_objects = []
        for e in emails:
            triage = triage_map.get(e.id)
            orm = EmailORM(
                id=e.id,
                account_id=e.account_id,
                message_id=e.message_id,
                thread_id=e.thread_id,
                subject=e.subject,
                sender_email=e.sender.email,
                sender_name=e.sender.name,
                recipients=[{"email": a.email, "name": a.name} for a in e.recipients],
                cc=[{"email": a.email, "name": a.name} for a in e.cc],
                body_text=e.body_text,
                body_html=e.body_html,
                date=e.date,
                is_read=e.is_read,
                category=e.category.value,
                priority=e.priority.value,
                triage_score=triage.attention_score if triage else None,
                triage_decision=triage.decision.value if triage else _default_decision(e),
                summary=triage.summary if triage else None,
                needs_reply=triage.needs_reply if triage else None,
                processed_at=datetime.now(timezone.utc),
                raw_headers=e.raw_headers,
            )
            orm_objects.append(orm)

        new_ids = upsert_emails(orm_objects)

        # ── 6. Sync reply status from Gmail (BEFORE collecting notifications) ──
        replies_synced = _sync_reply_status(account, creds)

        # ── 7. Collect attention emails (for summary notification) ──
        new_emails = [e for e in emails if e.id in new_ids]
        attention_emails, archived_count = _collect_attention_emails(new_emails, triage_map)

        # Filter out emails that were just marked as replied by sync
        from lib.db import get_email_by_id
        filtered_attention = []
        for ae in attention_emails:
            orm = get_email_by_id(ae["id"])
            if orm and not orm.replied_at:
                filtered_attention.append(ae)
            else:
                archived_count += 1
        attention_emails = filtered_attention

        # Mark notified
        from lib.db import mark_email_notified
        for ae in attention_emails:
            mark_email_notified(ae["id"])

        # ── 8. Update sync state ──
        update_account_sync(account.id, new_history_id)
        complete_sync_log(log_id, len(emails), len(new_ids))

        return {
            "account": account.email_address,
            "fetched": len(emails),
            "new": len(new_ids),
            "attention_emails": attention_emails,
            "archived": archived_count,
            "replies_synced": replies_synced,
        }

    except Exception as exc:
        logger.error(f"Account {account.email_address} failed: {exc}")
        complete_sync_log(log_id, 0, 0, status="failed", error_message=str(exc))
        _notify_account_error(account.email_address, str(exc))
        return {"account": account.email_address, "error": str(exc)}


# ======================================================================
# Helpers
# ======================================================================


def _triage_with_fallback(emails, chunk_size: int = 8) -> dict:
    """Try LLM triage in chunks, fall back to rule-based."""
    all_results = {}
    for i in range(0, len(emails), chunk_size):
        chunk = emails[i:i + chunk_size]
        try:
            from lib.llm import triage_emails
            results = triage_emails(chunk)
            for r in results:
                all_results[r.email_id] = r
        except Exception as exc:
            logger.warning(f"LLM triage failed for chunk: {exc}")
            from lib.triage import triage_emails_rule_based
            results = triage_emails_rule_based(chunk)
            for r in results:
                all_results[r.email_id] = r
    return all_results


def _default_decision(email) -> str:
    """Default triage decision for non-PRIMARY emails (already categorized by rules)."""
    from lib.models import EmailCategory
    if email.category in (EmailCategory.PROMOTIONS, EmailCategory.SOCIAL, EmailCategory.UPDATES):
        return "auto_archived"
    if email.category == EmailCategory.SPAM:
        return "noise"
    return "needs_attention"


def _collect_attention_emails(emails, triage_map: dict) -> tuple:
    """Collect emails needing attention into dicts for the summary. Returns (attention_list, archived_count)."""
    from lib.models import TriageDecision

    attention = []
    archived = 0
    for e in emails:
        triage = triage_map.get(e.id)
        if triage:
            if triage.decision != TriageDecision.NEEDS_ATTENTION:
                archived += 1
                continue
            attention.append({
                "id": e.id,
                "subject": e.subject,
                "sender_email": e.sender.email,
                "priority": triage.decision.value if hasattr(triage, "decision") else "normal",
                "summary": triage.summary,
            })
        else:
            if e.priority.value == "urgent":
                attention.append({
                    "id": e.id,
                    "subject": e.subject,
                    "sender_email": e.sender.email,
                    "priority": e.priority.value,
                    "summary": e.subject,
                })
            else:
                archived += 1
    return attention, archived


def _sync_reply_status(account, creds) -> int:
    """Check Gmail threads to see if unreplied emails have been answered. Returns count synced."""
    from lib.db import get_unreplied_thread_ids, mark_email_replied
    from lib.gmail import check_thread_replied

    unreplied = get_unreplied_thread_ids(account.id)
    synced = 0
    for item in unreplied:
        try:
            if check_thread_replied(creds, item["thread_id"], account.email_address, after_msg_id=item["email_id"]):
                mark_email_replied(item["email_id"])
                synced += 1
        except Exception:
            continue
    return synced


def _send_summary(emails_by_account: dict, total_archived: int, unreplied_emails=None) -> int:
    """Send a single summary message to the channel. Returns count of emails notified."""
    from lib.slack_client import build_cron_summary_blocks, send_dm

    total = sum(len(v) for v in emails_by_account.values())
    unreplied_count = len(unreplied_emails) if unreplied_emails else 0

    if total == 0 and unreplied_count == 0:
        return 0

    blocks = build_cron_summary_blocks(emails_by_account, total_archived)

    # Append needs-reply reminder if there are outstanding items
    if unreplied_count > 0:
        lines = [f"\n\u23f3 *{unreplied_count} email(s) still need your reply:*\n"]
        for e in unreplied_emails[:10]:
            lines.append(f"\u2022 {e.subject} \u2014 {e.sender_email}")
        if unreplied_count > 10:
            lines.append(f"_...and {unreplied_count - 10} more. Type \"needs reply\" to see all._")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    if total == 0 and unreplied_count > 0:
        send_dm(f"{unreplied_count} email(s) still need your reply", blocks=blocks)
    else:
        send_dm(f"{total} new email(s) need attention", blocks=blocks)
    return total


def _notify_expired_drafts(count: int):
    from lib.slack_client import send_dm
    send_dm(f"\u23f0 {count} pending draft(s) expired.")


def _notify_account_error(account_email: str, error: str):
    from lib.slack_client import send_dm
    send_dm(f"\u26a0\ufe0f Sync failed for *{account_email}*: {error}")
