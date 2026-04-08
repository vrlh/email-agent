"""GET /api/cron/onboard — backfill last 3 months of inbox emails.

One-time (or occasional) endpoint to import historical emails, triage them,
and flag ones that still need replies.  Protected by CRON_SECRET.

This is heavier than the regular cron — may take 60-120s with many emails.
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
            stats = _run_onboard()
            self._respond(200, stats)
        except Exception as exc:
            logger.error(f"Onboard failed: {exc}")
            self._respond(500, {"error": str(exc)})

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())


def _run_onboard() -> dict:
    from lib.db import (
        create_tables,
        get_active_accounts,
        update_account_sync,
        update_account_tokens,
        upsert_emails,
    )
    from lib.db_models import EmailORM
    from lib.gmail import (
        check_thread_replied,
        credentials_from_encrypted,
        credentials_to_encrypted,
        fetch_inbox_emails_since,
        refresh_if_needed,
    )
    from lib.models import EmailCategory
    from lib.rules.builtin import BuiltinRules
    from lib.rules.engine import RulesEngine
    from lib.slack_client import send_dm

    create_tables()

    accounts = get_active_accounts()
    if not accounts:
        return {"status": "ok", "message": "No active accounts"}

    results = []

    for account in accounts:
        try:
            # ── Credentials ──
            creds = credentials_from_encrypted(account.encrypted_tokens)
            creds, was_refreshed = refresh_if_needed(creds)
            if was_refreshed:
                update_account_tokens(account.id, credentials_to_encrypted(creds))

            # ── Fetch last 3 months ──
            emails, history_id = fetch_inbox_emails_since(
                creds, account.id, since_days=90, max_results=500,
            )

            if not emails:
                results.append({"account": account.email_address, "fetched": 0, "new": 0, "needs_reply": 0})
                continue

            # ── Rules categorization ──
            engine = RulesEngine()
            engine.load_rules(BuiltinRules.get_all_rules())
            emails = engine.process_emails(emails)

            # ── AI triage for PRIMARY emails ──
            primary = [e for e in emails if e.category == EmailCategory.PRIMARY]
            triage_map = {}
            if primary:
                triage_map = _triage_batch(primary)

            # ── Build ORM objects ──
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
                    needs_reply=triage.needs_reply if triage else False,
                    processed_at=datetime.now(timezone.utc),
                    raw_headers=e.raw_headers,
                )
                orm_objects.append(orm)

            new_ids = upsert_emails(orm_objects)

            # ── Check reply status for needs_reply emails ──
            from lib.db import mark_email_replied
            needs_reply_count = 0
            for e in emails:
                if e.id not in new_ids:
                    continue
                triage = triage_map.get(e.id)
                if triage and triage.needs_reply and e.thread_id:
                    if check_thread_replied(creds, e.thread_id, account.email_address):
                        mark_email_replied(e.id)
                    else:
                        needs_reply_count += 1

            # Update history ID
            if history_id:
                update_account_sync(account.id, history_id)

            results.append({
                "account": account.email_address,
                "fetched": len(emails),
                "new": len(new_ids),
                "needs_reply": needs_reply_count,
            })

        except Exception as exc:
            logger.error(f"Onboard {account.email_address} failed: {exc}")
            results.append({"account": account.email_address, "error": str(exc)})

    # Send summary to Slack
    total_new = sum(r.get("new", 0) for r in results)
    total_needs_reply = sum(r.get("needs_reply", 0) for r in results)
    send_dm(
        f"\U0001f4e5 *Onboarding complete*\n"
        f"Processed {total_new} emails from the last 3 months.\n"
        f"{total_needs_reply} email(s) need your reply.\n"
        f"Type \"needs reply\" to see them."
    )

    return {
        "status": "ok",
        "accounts": len(results),
        "total_new": total_new,
        "total_needs_reply": total_needs_reply,
        "details": results,
    }


def _triage_batch(emails) -> dict:
    try:
        from lib.llm import triage_emails
        results = triage_emails(emails)
        return {r.email_id: r for r in results}
    except Exception as exc:
        logger.warning(f"LLM triage failed, using rule-based: {exc}")
        from lib.triage import triage_emails_rule_based
        results = triage_emails_rule_based(emails)
        return {r.email_id: r for r in results}


def _default_decision(email) -> str:
    from lib.models import EmailCategory
    if email.category in (EmailCategory.PROMOTIONS, EmailCategory.SOCIAL, EmailCategory.UPDATES):
        return "auto_archived"
    if email.category == EmailCategory.SPAM:
        return "noise"
    return "needs_attention"
