"""Postgres database layer — NullPool engine for serverless, session factory, queries."""

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from sqlalchemy import delete as sql_delete

from lib.db_models import (
    Base,
    EmailORM,
    GmailAccountORM,
    PendingDraftORM,
    SyncLogORM,
    UserRuleORM,
)

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            os.environ["DATABASE_URL"],
            poolclass=NullPool,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal


@contextmanager
def get_session():
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_tables():
    Base.metadata.create_all(get_engine())


# ── Gmail Accounts ──


def get_active_accounts() -> List[GmailAccountORM]:
    with get_session() as session:
        results = list(
            session.execute(
                select(GmailAccountORM).where(GmailAccountORM.is_active.is_(True))
            )
            .scalars()
            .all()
        )
        for r in results:
            session.expunge(r)
        return results


def upsert_account(
    account_id: str,
    email_address: str,
    display_name: Optional[str],
    encrypted_tokens: str,
) -> GmailAccountORM:
    with get_session() as session:
        account = session.get(GmailAccountORM, account_id)
        if account:
            account.email_address = email_address
            account.display_name = display_name
            account.encrypted_tokens = encrypted_tokens
            account.updated_at = datetime.now(timezone.utc)
        else:
            account = GmailAccountORM(
                id=account_id,
                email_address=email_address,
                display_name=display_name,
                encrypted_tokens=encrypted_tokens,
            )
            session.add(account)
        session.flush()
        # Detach before returning so it's usable outside the session
        session.expunge(account)
        return account


def update_account_sync(
    account_id: str,
    last_history_id: Optional[str] = None,
) -> None:
    with get_session() as session:
        account = session.get(GmailAccountORM, account_id)
        if account:
            account.last_sync_at = datetime.now(timezone.utc)
            if last_history_id is not None:
                account.last_history_id = last_history_id
            account.updated_at = datetime.now(timezone.utc)


def update_account_tokens(account_id: str, encrypted_tokens: str) -> None:
    with get_session() as session:
        account = session.get(GmailAccountORM, account_id)
        if account:
            account.encrypted_tokens = encrypted_tokens
            account.updated_at = datetime.now(timezone.utc)


# ── Emails ──


def upsert_emails(emails: List[EmailORM]) -> set:
    """Insert emails, skipping duplicates. Returns set of newly inserted IDs."""
    if not emails:
        return set()
    new_ids: set = set()
    with get_session() as session:
        for email_orm in emails:
            existing = session.get(EmailORM, email_orm.id)
            if existing is None:
                session.add(email_orm)
                new_ids.add(email_orm.id)
    return new_ids


def get_emails_for_account(
    account_id: str,
    unread_only: bool = False,
    limit: int = 50,
) -> List[EmailORM]:
    with get_session() as session:
        stmt = (
            select(EmailORM)
            .where(EmailORM.account_id == account_id)
            .order_by(EmailORM.date.desc())
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(EmailORM.is_read.is_(False))
        results = list(session.execute(stmt).scalars().all())
        for r in results:
            session.expunge(r)
        return results


def get_attention_emails(
    account_id: str,
    unread_only: bool = False,
    limit: int = 50,
) -> List[EmailORM]:
    """Get emails that need attention (filtered out noise/auto-archived)."""
    with get_session() as session:
        stmt = (
            select(EmailORM)
            .where(EmailORM.account_id == account_id)
            .where(EmailORM.triage_decision == "needs_attention")
            .order_by(EmailORM.date.desc())
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(EmailORM.is_read.is_(False))
        results = list(session.execute(stmt).scalars().all())
        for r in results:
            session.expunge(r)
        return results


def get_unnotified_attention_emails() -> List[EmailORM]:
    """Get emails that need attention and haven't been notified yet."""
    with get_session() as session:
        stmt = (
            select(EmailORM)
            .where(EmailORM.triage_decision == "needs_attention")
            .where(EmailORM.notified_at.is_(None))
            .order_by(EmailORM.date.desc())
        )
        results = list(session.execute(stmt).scalars().all())
        for r in results:
            session.expunge(r)
        return results


def mark_email_notified(email_id: str) -> None:
    with get_session() as session:
        email = session.get(EmailORM, email_id)
        if email:
            email.notified_at = datetime.now(timezone.utc)


def mark_email_replied(email_id: str) -> None:
    with get_session() as session:
        email = session.get(EmailORM, email_id)
        if email:
            email.replied_at = datetime.now(timezone.utc)
            email.needs_reply = False


def get_needs_reply_emails() -> List[EmailORM]:
    """Get emails that need a reply and haven't been replied to."""
    with get_session() as session:
        stmt = (
            select(EmailORM)
            .where(EmailORM.needs_reply.is_(True))
            .where(EmailORM.replied_at.is_(None))
            .order_by(EmailORM.date.desc())
        )
        results = list(session.execute(stmt).scalars().all())
        for r in results:
            session.expunge(r)
        return results


def reset_needs_reply(account_id: str) -> int:
    """Reset needs_reply to NULL for all primary emails so they get re-triaged."""
    with get_session() as session:
        stmt = (
            update(EmailORM)
            .where(EmailORM.account_id == account_id)
            .where(EmailORM.category == "primary")
            .where(EmailORM.replied_at.is_(None))  # don't reset already-replied emails
            .values(needs_reply=None)
        )
        result = session.execute(stmt)
        return result.rowcount


def get_untriaged_emails(account_id: str, limit: int = 200) -> List[EmailORM]:
    """Get emails where needs_reply has never been set (NULL)."""
    with get_session() as session:
        stmt = (
            select(EmailORM)
            .where(EmailORM.account_id == account_id)
            .where(EmailORM.needs_reply.is_(None))
            .where(EmailORM.category == "primary")
            .order_by(EmailORM.date.desc())
            .limit(limit)
        )
        results = list(session.execute(stmt).scalars().all())
        for r in results:
            session.expunge(r)
        return results


def bulk_update_needs_reply(updates: List[dict]) -> None:
    """Batch update needs_reply and summary for emails. Each dict: {id, needs_reply, summary}."""
    with get_session() as session:
        for u in updates:
            email = session.get(EmailORM, u["id"])
            if email:
                email.needs_reply = u.get("needs_reply", False)
                if u.get("summary"):
                    email.summary = u["summary"]


def get_unreplied_thread_ids(account_id: str) -> List[dict]:
    """Get thread IDs of emails needing reply for reply-sync checking."""
    with get_session() as session:
        stmt = (
            select(EmailORM.id, EmailORM.thread_id)
            .where(EmailORM.account_id == account_id)
            .where(EmailORM.needs_reply.is_(True))
            .where(EmailORM.replied_at.is_(None))
            .where(EmailORM.thread_id.isnot(None))
        )
        rows = session.execute(stmt).all()
        return [{"email_id": r[0], "thread_id": r[1]} for r in rows]


def get_email_by_id(email_id: str) -> Optional[EmailORM]:
    with get_session() as session:
        email = session.get(EmailORM, email_id)
        if email:
            session.expunge(email)
        return email


def get_recent_emails(limit: int = 20) -> List[EmailORM]:
    """Get recent emails across all accounts."""
    with get_session() as session:
        stmt = (
            select(EmailORM)
            .order_by(EmailORM.date.desc())
            .limit(limit)
        )
        results = list(session.execute(stmt).scalars().all())
        for r in results:
            session.expunge(r)
        return results


# ── Pending Drafts ──


def get_pending_draft() -> Optional[PendingDraftORM]:
    with get_session() as session:
        stmt = select(PendingDraftORM).where(PendingDraftORM.status == "pending")
        draft = session.execute(stmt).scalar_one_or_none()
        if draft:
            session.expunge(draft)
        return draft


def create_pending_draft(
    account_id: str,
    to_addresses: list,
    subject: str,
    body_text: str,
    reply_to_email_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    cc_addresses: Optional[list] = None,
    slack_message_ts: Optional[str] = None,
) -> PendingDraftORM:
    with get_session() as session:
        # Cancel any existing pending draft
        session.execute(
            update(PendingDraftORM)
            .where(PendingDraftORM.status == "pending")
            .values(status="cancelled")
        )
        draft = PendingDraftORM(
            id=str(uuid.uuid4()),
            account_id=account_id,
            reply_to_email_id=reply_to_email_id,
            thread_id=thread_id,
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            subject=subject,
            body_text=body_text,
            status="pending",
            slack_message_ts=slack_message_ts,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session.add(draft)
        session.flush()
        session.expunge(draft)
        return draft


def update_draft_status(draft_id: str, status: str) -> None:
    with get_session() as session:
        draft = session.get(PendingDraftORM, draft_id)
        if draft:
            draft.status = status


def update_draft_slack_ts(draft_id: str, slack_ts: str) -> None:
    with get_session() as session:
        draft = session.get(PendingDraftORM, draft_id)
        if draft:
            draft.slack_message_ts = slack_ts


def update_draft_body(draft_id: str, body_text: str) -> None:
    with get_session() as session:
        draft = session.get(PendingDraftORM, draft_id)
        if draft:
            draft.body_text = body_text


def expire_stale_drafts() -> int:
    """Mark pending drafts past their expiry as expired. Returns count."""
    now = datetime.now(timezone.utc)
    with get_session() as session:
        stmt = (
            update(PendingDraftORM)
            .where(PendingDraftORM.status == "pending")
            .where(PendingDraftORM.expires_at < now)
            .values(status="expired")
        )
        result = session.execute(stmt)
        return result.rowcount


# ── Sync Log ──


def create_sync_log(account_id: str) -> int:
    with get_session() as session:
        log = SyncLogORM(account_id=account_id)
        session.add(log)
        session.flush()
        log_id = log.id
        return log_id


def complete_sync_log(
    log_id: int,
    emails_fetched: int,
    emails_new: int,
    status: str = "completed",
    error_message: Optional[str] = None,
) -> None:
    with get_session() as session:
        log = session.get(SyncLogORM, log_id)
        if log:
            log.emails_fetched = emails_fetched
            log.emails_new = emails_new
            log.status = status
            log.error_message = error_message
            log.completed_at = datetime.now(timezone.utc)


# ── User Rules ──


def create_user_rule(
    rule_type: str,
    field: str,
    operator: str,
    value: str,
    action: str,
) -> UserRuleORM:
    with get_session() as session:
        rule = UserRuleORM(
            id=str(uuid.uuid4()),
            rule_type=rule_type,
            field=field,
            operator=operator,
            value=value,
            action=action,
        )
        session.add(rule)
        session.flush()
        session.expunge(rule)
        return rule


def get_user_rules(enabled_only: bool = True) -> List[UserRuleORM]:
    with get_session() as session:
        stmt = select(UserRuleORM)
        if enabled_only:
            stmt = stmt.where(UserRuleORM.enabled.is_(True))
        stmt = stmt.order_by(UserRuleORM.created_at)
        results = list(session.execute(stmt).scalars().all())
        for r in results:
            session.expunge(r)
        return results


def delete_user_rule(rule_id: str) -> bool:
    with get_session() as session:
        rule = session.get(UserRuleORM, rule_id)
        if rule:
            session.delete(rule)
            return True
        return False
