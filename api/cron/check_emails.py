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
    total_notified = 0

    for account in accounts:
        result = _process_account(account)
        results.append(result)
        total_new += result.get("new", 0)
        total_notified += result.get("notified", 0)

    return {
        "status": "ok",
        "accounts_processed": len(results),
        "total_new_emails": total_new,
        "total_notifications_sent": total_notified,
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

        # ── 6. Notify (only for newly inserted emails) ──
        new_emails = [e for e in emails if e.id in new_ids]
        notified = _send_notifications(account.email_address, new_emails, triage_map)

        # ── 7. Update sync state ──
        update_account_sync(account.id, new_history_id)
        complete_sync_log(log_id, len(emails), len(new_ids))

        return {
            "account": account.email_address,
            "fetched": len(emails),
            "new": len(new_ids),
            "notified": notified,
        }

    except Exception as exc:
        logger.error(f"Account {account.email_address} failed: {exc}")
        complete_sync_log(log_id, 0, 0, status="failed", error_message=str(exc))
        _notify_account_error(account.email_address, str(exc))
        return {"account": account.email_address, "error": str(exc)}


# ======================================================================
# Helpers
# ======================================================================


def _triage_with_fallback(emails) -> dict:
    """Try LLM triage, fall back to rule-based."""
    try:
        from lib.llm import triage_emails
        results = triage_emails(emails)
        return {r.email_id: r for r in results}
    except Exception as exc:
        logger.warning(f"Claude triage failed, using rule-based: {exc}")
        from lib.triage import triage_emails_rule_based
        results = triage_emails_rule_based(emails)
        return {r.email_id: r for r in results}


def _default_decision(email) -> str:
    """Default triage decision for non-PRIMARY emails (already categorized by rules)."""
    from lib.models import EmailCategory
    if email.category in (EmailCategory.PROMOTIONS, EmailCategory.SOCIAL, EmailCategory.UPDATES):
        return "auto_archived"
    if email.category == EmailCategory.SPAM:
        return "noise"
    return "needs_attention"


def _send_notifications(account_email: str, emails, triage_map: dict) -> int:
    """Send individual Slack DMs for emails that need attention. Returns count."""
    from lib.models import TriageDecision
    from lib.slack_client import build_email_notification_blocks, send_dm

    notified = 0
    for e in emails:
        triage = triage_map.get(e.id)

        # Determine if this email needs a notification
        if triage:
            if triage.decision != TriageDecision.NEEDS_ATTENTION:
                continue
            summary = triage.summary
            suggested = triage.suggested_action
        else:
            # Non-primary emails: only notify if urgent priority
            if e.priority.value != "urgent":
                continue
            summary = e.subject
            suggested = None

        blocks = build_email_notification_blocks(e, account_email, summary, suggested)
        send_dm(f"New email: {e.subject}", blocks=blocks)

        # Mark as notified so we don't re-send on next run
        from lib.db import mark_email_notified
        mark_email_notified(e.id)

        notified += 1

    return notified


def _notify_expired_drafts(count: int):
    from lib.slack_client import send_dm
    send_dm(f"\u23f0 {count} pending draft(s) expired.")


def _notify_account_error(account_email: str, error: str):
    from lib.slack_client import send_dm
    send_dm(f"\u26a0\ufe0f Sync failed for *{account_email}*: {error}")
