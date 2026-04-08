"""Onboarding logic — backfill emails for all accounts or a single account.

Called from: /api/cron/onboard (HTTP), Slack "onboard" command, OAuth callback.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def run_onboard(account_id: Optional[str] = None, notify_slack: bool = True) -> dict:
    """Onboard all active accounts, or a single account if account_id is given.

    Returns stats dict. Sends Slack summary if notify_slack is True.
    """
    from lib.db import create_tables, get_active_accounts
    from lib.db_models import GmailAccountORM

    create_tables()

    if account_id:
        from lib.db import get_session
        with get_session() as session:
            account = session.get(GmailAccountORM, account_id)
            if account:
                session.expunge(account)
        if not account:
            return {"status": "error", "message": f"Account {account_id} not found"}
        accounts = [account]
    else:
        accounts = get_active_accounts()

    if not accounts:
        return {"status": "ok", "message": "No active accounts"}

    results = []
    for account in accounts:
        result = _onboard_single_account(account)
        results.append(result)

    total_new = sum(r.get("new", 0) for r in results)
    total_needs_reply = sum(r.get("needs_reply", 0) for r in results)

    if notify_slack:
        from lib.slack_client import send_dm
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


def _onboard_single_account(account) -> dict:
    from lib.db import (
        mark_email_replied,
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

    try:
        creds = credentials_from_encrypted(account.encrypted_tokens)
        creds, was_refreshed = refresh_if_needed(creds)
        if was_refreshed:
            update_account_tokens(account.id, credentials_to_encrypted(creds))

        emails, history_id = fetch_inbox_emails_since(
            creds, account.id, since_days=90, max_results=500,
        )

        if not emails:
            return {"account": account.email_address, "fetched": 0, "new": 0, "needs_reply": 0}

        # Rules categorization
        engine = RulesEngine()
        engine.load_rules(BuiltinRules.get_all_rules())
        emails = engine.process_emails(emails)

        # AI triage for PRIMARY emails
        primary = [e for e in emails if e.category == EmailCategory.PRIMARY]
        triage_map = {}
        if primary:
            triage_map = _triage_batch(primary)

        # Build ORM objects
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

        # Backfill needs_reply for existing emails that were never triaged
        from lib.db import bulk_update_needs_reply, get_untriaged_emails
        untriaged = get_untriaged_emails(account.id, limit=200)
        if untriaged:
            # Convert ORM to model for LLM triage
            from lib.models import Email, EmailAddress
            model_emails = []
            for u in untriaged:
                model_emails.append(Email(
                    id=u.id, account_id=u.account_id, message_id=u.message_id or u.id,
                    subject=u.subject, sender=EmailAddress(email=u.sender_email, name=u.sender_name),
                    body_text=u.body_text, date=u.date, is_read=u.is_read,
                    category=EmailCategory(u.category) if u.category else EmailCategory.PRIMARY,
                ))
            backfill_triage = _triage_batch(model_emails)
            updates = []
            for u in untriaged:
                t = backfill_triage.get(u.id)
                updates.append({
                    "id": u.id,
                    "needs_reply": t.needs_reply if t else False,
                    "summary": t.summary if t else None,
                })
            bulk_update_needs_reply(updates)

        # Check reply status for all needs_reply emails (new + backfilled)
        from lib.db import get_unreplied_thread_ids
        unreplied = get_unreplied_thread_ids(account.id)
        needs_reply_count = 0
        reply_checks = 0
        for item in unreplied:
            reply_checks += 1
            if reply_checks % 10 == 0:
                time.sleep(1)
            if check_thread_replied(creds, item["thread_id"], account.email_address):
                mark_email_replied(item["email_id"])
            else:
                needs_reply_count += 1

        if history_id:
            update_account_sync(account.id, history_id)

        return {
            "account": account.email_address,
            "fetched": len(emails),
            "new": len(new_ids),
            "backfilled": len(untriaged),
            "needs_reply": needs_reply_count,
        }

    except Exception as exc:
        logger.error(f"Onboard {account.email_address} failed: {exc}")
        return {"account": account.email_address, "error": str(exc)}


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
