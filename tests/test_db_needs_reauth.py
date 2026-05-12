"""Tests for ``lib.db`` needs_reauth flag support.

Covers behaviors B1.1-B1.4:
- _ensure_migrations runs the ALTER TABLE ADD COLUMN IF NOT EXISTS statement.
- mark_account_needs_reauth(id, True/False) toggles the flag on the ORM row.
- upsert_account sets needs_reauth=False on every successful upsert (this is
  how the OAuth-callback success path clears the flag).
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

from lib import db


class _FakeAccount:
    """Lightweight stand-in for a GmailAccountORM row."""
    def __init__(self, id_="uuid-a", email="a@x.com", needs_reauth=False):
        self.id = id_
        self.email_address = email
        self.display_name = "A"
        self.encrypted_tokens = "tok"
        self.is_active = True
        self.needs_reauth = needs_reauth


def _install_fake_session(monkeypatch, *, get_returns=None, scalar_returns=None):
    """Wire ``db.get_session()`` to yield a MagicMock session that captures
    .get() / .execute() returns and tracks .add() calls."""
    session = MagicMock()
    session.get = MagicMock(return_value=get_returns)
    # session.execute(...).scalar_one_or_none() → scalar_returns
    exec_result = MagicMock()
    exec_result.scalar_one_or_none = MagicMock(return_value=scalar_returns)
    session.execute = MagicMock(return_value=exec_result)

    @contextmanager
    def fake_session():
        yield session

    monkeypatch.setattr(db, "get_session", fake_session)
    return session


def test_ensure_migrations_adds_needs_reauth_column(monkeypatch):
    # B1.1: the ALTER TABLE for needs_reauth is executed under IF NOT EXISTS.
    executed_sql = []
    conn = MagicMock()
    conn.execute = MagicMock(side_effect=lambda stmt: executed_sql.append(str(stmt)))

    @contextmanager
    def fake_begin():
        yield conn

    fake_engine = MagicMock()
    fake_engine.begin = fake_begin
    monkeypatch.setattr(db, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(db, "_migrations_ran", False)

    db._ensure_migrations()

    joined = " ".join(executed_sql).lower()
    assert "alter table gmail_accounts" in joined
    assert "add column if not exists needs_reauth" in joined


def test_mark_account_needs_reauth_sets_flag_true(monkeypatch):
    # B1.2: set the flag to True on the matching row.
    acct = _FakeAccount(id_="uuid-a", needs_reauth=False)
    _install_fake_session(monkeypatch, get_returns=acct)

    db.mark_account_needs_reauth("uuid-a", True)

    assert acct.needs_reauth is True


def test_mark_account_needs_reauth_clears_flag(monkeypatch):
    # B1.3: set the flag to False.
    acct = _FakeAccount(id_="uuid-a", needs_reauth=True)
    _install_fake_session(monkeypatch, get_returns=acct)

    db.mark_account_needs_reauth("uuid-a", False)

    assert acct.needs_reauth is False


def test_mark_account_needs_reauth_missing_account_is_noop(monkeypatch):
    # Edge: missing account → no exception (mirrors update_account_tokens behavior).
    _install_fake_session(monkeypatch, get_returns=None)

    db.mark_account_needs_reauth("missing", True)  # must not raise


def test_upsert_account_clears_needs_reauth_on_update(monkeypatch):
    # B1.4: upserting (the OAuth-callback success path) flips needs_reauth back
    # to False, so we don't need a separate clear call in the callback.
    existing = _FakeAccount(id_="uuid-a", needs_reauth=True)
    _install_fake_session(monkeypatch, scalar_returns=existing)

    db.upsert_account(
        account_id="ignored-because-existing",
        email_address="a@x.com",
        display_name="A",
        encrypted_tokens="new-tok",
    )

    assert existing.needs_reauth is False
