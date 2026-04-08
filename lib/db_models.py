"""SQLAlchemy ORM table definitions for Neon Postgres."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class GmailAccountORM(Base):
    __tablename__ = "gmail_accounts"

    id = Column(String, primary_key=True)
    email_address = Column(String, nullable=False, unique=True, index=True)
    display_name = Column(String)
    encrypted_tokens = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    last_sync_at = Column(DateTime(timezone=True))
    last_history_id = Column(String)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class EmailORM(Base):
    __tablename__ = "emails"

    id = Column(String, primary_key=True)
    account_id = Column(
        String, ForeignKey("gmail_accounts.id"), nullable=False, index=True
    )
    message_id = Column(String, nullable=False)
    thread_id = Column(String, index=True)
    subject = Column(Text, nullable=False)
    sender_email = Column(String, nullable=False)
    sender_name = Column(String)
    recipients = Column(JSONB)
    cc = Column(JSONB)
    body_text = Column(Text)
    body_html = Column(Text)
    date = Column(DateTime(timezone=True), nullable=False)
    is_read = Column(Boolean, nullable=False, default=False)
    category = Column(String, nullable=False, default="primary")
    priority = Column(String, nullable=False, default="normal")
    triage_score = Column(Float)
    triage_decision = Column(String)
    summary = Column(Text)
    needs_reply = Column(Boolean)
    replied_at = Column(DateTime(timezone=True))
    notified_at = Column(DateTime(timezone=True))
    processed_at = Column(DateTime(timezone=True))
    raw_headers = Column(JSONB)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_emails_account_date", "account_id", date.desc()),
    )


class PendingDraftORM(Base):
    __tablename__ = "pending_draft"

    id = Column(String, primary_key=True)
    account_id = Column(
        String, ForeignKey("gmail_accounts.id"), nullable=False
    )
    reply_to_email_id = Column(String)
    thread_id = Column(String)
    to_addresses = Column(JSONB, nullable=False)
    cc_addresses = Column(JSONB)
    subject = Column(Text, nullable=False)
    body_text = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="pending", index=True)
    slack_message_ts = Column(String)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)


class UserRuleORM(Base):
    __tablename__ = "user_rules"

    id = Column(String, primary_key=True)
    rule_type = Column(String, nullable=False)  # "ignore" or "priority"
    field = Column(String, nullable=False)  # "sender", "sender_domain", "subject"
    operator = Column(String, nullable=False)  # "contains", "equals", "regex"
    value = Column(String, nullable=False)
    action = Column(String, nullable=False)  # "auto_archive" or "boost"
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SyncLogORM(Base):
    __tablename__ = "sync_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        String, ForeignKey("gmail_accounts.id"), nullable=False
    )
    emails_fetched = Column(Integer, default=0)
    emails_new = Column(Integer, default=0)
    status = Column(String, nullable=False, default="running")
    error_message = Column(Text)
    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at = Column(DateTime(timezone=True))
