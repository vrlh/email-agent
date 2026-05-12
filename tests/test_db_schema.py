"""Tests for ``lib.db_models.GmailAccountORM`` schema changes.

Covers B3.1: ``needs_reauth`` column exists, non-nullable, defaults False.
"""

from lib.db_models import GmailAccountORM


def test_gmail_account_has_needs_reauth_column():
    # B3.1: column exists on the SQLAlchemy table.
    assert "needs_reauth" in GmailAccountORM.__table__.columns


def test_needs_reauth_column_is_not_nullable_and_defaults_false():
    # B3.1: NOT NULL with default False — backward-compatible for existing rows
    # (migration applies the default).
    col = GmailAccountORM.__table__.columns["needs_reauth"]
    assert col.nullable is False
    # SQLAlchemy stores literal defaults under .default.arg
    assert col.default is not None
    assert col.default.arg is False
