"""Tests for ``lib.db.search_contacts``.

Covers behaviors 1-3 from the plan:
- ILIKE-based SQL is executed with the right pattern.
- Min-length guard (< 2 chars) returns [] without DB access.
- Row-to-dict mapping shape.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from lib import db


def _install_fake_session(monkeypatch, rows=None):
    """Replace db.get_session() with a MagicMock that captures execute() calls
    and yields the provided rows."""
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.all = MagicMock(return_value=list(rows or []))
    session.execute = MagicMock(return_value=exec_result)

    @contextmanager
    def fake_session():
        yield session

    monkeypatch.setattr(db, "get_session", fake_session)
    return session


def test_search_contacts_runs_ilike_query_with_pattern(monkeypatch):
    # Behavior 1: builds a SQL statement with ILIKE and the right pattern arg.
    session = _install_fake_session(monkeypatch, rows=[])

    db.search_contacts("jeffrey", limit=5)

    assert session.execute.call_count == 1
    args, kwargs = session.execute.call_args
    # First positional arg is the SQLAlchemy text() statement.
    stmt = args[0]
    sql_text = str(stmt).lower()
    assert "ilike" in sql_text
    assert "group by" in sql_text
    # Second positional arg is the param dict (or kwargs).
    params = args[1] if len(args) > 1 else kwargs
    assert params.get("pattern") == "%jeffrey%"
    assert params.get("limit") == 5


def test_search_contacts_min_length_guard(monkeypatch):
    # Behavior 2: empty or 1-char query returns [] and never touches the DB.
    session = _install_fake_session(monkeypatch, rows=[("a@x.com", "A", None, 1)])

    assert db.search_contacts("") == []
    assert db.search_contacts(" ") == []
    assert db.search_contacts("a") == []

    session.execute.assert_not_called()


def test_search_contacts_maps_rows_to_dicts(monkeypatch):
    # Behavior 3: a row with the four named fields becomes the expected dict.
    when = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)
    fake_row = SimpleNamespace(
        email="a@x.com", name="Jeffrey A", last_seen=when, frequency=3,
    )
    _install_fake_session(monkeypatch, rows=[fake_row])

    result = db.search_contacts("jeff")

    assert result == [{
        "email": "a@x.com",
        "name": "Jeffrey A",
        "last_seen": when,
        "frequency": 3,
    }]
