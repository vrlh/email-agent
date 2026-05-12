"""Tests for ``api.slack.events._build_context``.

Covers behaviors 1-8 from the plan:
- Header lists connected account email addresses.
- Email lines show ``account=<email-address>`` (not the UUID).
- Email's ``id=<uuid>`` is preserved.
- ``_last_displayed_emails`` short-circuits ``get_recent_emails``.
- Pending draft line appended when present.
- Empty world → empty string.
- Orphan account_id falls back to UUID.
"""

from api.slack import events


def _patch_db(monkeypatch, accounts=None, recent=None, draft=None):
    """Patch the three lazy db imports used by ``_build_context``."""
    monkeypatch.setattr("lib.db.get_active_accounts", lambda: list(accounts or []))
    monkeypatch.setattr("lib.db.get_pending_draft", lambda: draft)
    recent_calls = []

    def fake_recent(limit: int = 50):
        recent_calls.append(limit)
        return list(recent or [])

    monkeypatch.setattr("lib.db.get_recent_emails", fake_recent)
    return recent_calls


def test_header_lists_connected_account_addresses(monkeypatch, make_account, make_email):
    # Behavior 1: header `Connected accounts: a@x.com, b@y.com` is line 1.
    a1 = make_account("uuid-a", "a@x.com")
    a2 = make_account("uuid-b", "b@y.com")
    e1 = make_email("e1", "uuid-a", "sender@x.com", "Hi")
    _patch_db(monkeypatch, accounts=[a1, a2], recent=[e1])
    monkeypatch.setattr(events, "_last_displayed_emails", [])

    ctx = events._build_context()

    first_line = ctx.splitlines()[0]
    assert first_line == "Connected accounts: a@x.com, b@y.com"


def test_email_line_shows_account_email_not_uuid(monkeypatch, make_account, make_email):
    # Behavior 2: each email line uses account=<email_address>, resolved via the account map.
    a1 = make_account("uuid-a", "a@x.com")
    e1 = make_email("email-id-1", "uuid-a", "sender@x.com", "Hi")
    _patch_db(monkeypatch, accounts=[a1], recent=[e1])
    monkeypatch.setattr(events, "_last_displayed_emails", [])

    ctx = events._build_context()

    assert "account=a@x.com" in ctx
    assert "account=uuid-a" not in ctx


def test_email_id_is_preserved(monkeypatch, make_account, make_email):
    # Behavior 3: email's id=<uuid> survives unchanged (used downstream for ref resolution).
    a1 = make_account("uuid-a", "a@x.com")
    e1 = make_email("email-id-xyz", "uuid-a", "sender@x.com", "Hi")
    _patch_db(monkeypatch, accounts=[a1], recent=[e1])
    monkeypatch.setattr(events, "_last_displayed_emails", [])

    ctx = events._build_context()

    assert "id=email-id-xyz" in ctx


def test_last_displayed_emails_short_circuits_recent(monkeypatch, make_account, make_email):
    # Behavior 4: when _last_displayed_emails is non-empty, get_recent_emails is NOT called.
    a1 = make_account("uuid-a", "a@x.com")
    displayed = make_email("displayed-id", "uuid-a", "s@x.com", "Displayed")
    other = make_email("other-id", "uuid-a", "s@x.com", "Other")
    recent_calls = _patch_db(monkeypatch, accounts=[a1], recent=[other])
    monkeypatch.setattr(events, "_last_displayed_emails", [displayed])

    ctx = events._build_context()

    assert "displayed-id" in ctx
    assert "other-id" not in ctx
    assert recent_calls == []


def test_empty_last_displayed_falls_back_to_recent(monkeypatch, make_account, make_email):
    # Behavior 5: empty _last_displayed_emails → get_recent_emails(limit=50) is called.
    a1 = make_account("uuid-a", "a@x.com")
    e1 = make_email("recent-id", "uuid-a", "s@x.com", "Recent")
    recent_calls = _patch_db(monkeypatch, accounts=[a1], recent=[e1])
    monkeypatch.setattr(events, "_last_displayed_emails", [])

    ctx = events._build_context()

    assert "recent-id" in ctx
    assert recent_calls == [50]


def test_pending_draft_line_appended(monkeypatch, make_account, make_email, make_draft):
    # Behavior 6: pending draft → trailing line.
    a1 = make_account("uuid-a", "a@x.com")
    e1 = make_email("e1", "uuid-a", "s@x.com", "Hi")
    d = make_draft("draft-id-1", "pending", "Re: project")
    _patch_db(monkeypatch, accounts=[a1], recent=[e1], draft=d)
    monkeypatch.setattr(events, "_last_displayed_emails", [])

    ctx = events._build_context()

    assert "Pending draft: id=draft-id-1 status=pending" in ctx
    assert 'subject="Re: project"' in ctx


def test_zero_accounts_zero_emails_no_draft_returns_empty(monkeypatch):
    # Behavior 7: empty world → empty string (preserves current contract).
    _patch_db(monkeypatch, accounts=[], recent=[], draft=None)
    monkeypatch.setattr(events, "_last_displayed_emails", [])

    assert events._build_context() == ""


def test_orphan_account_id_falls_back_to_uuid(monkeypatch, make_account, make_email):
    # Behavior 8: email whose account_id doesn't match any active account → account=<uuid> literal.
    a1 = make_account("uuid-active", "a@x.com")
    e_orphan = make_email("e1", "uuid-gone", "s@x.com", "Orphaned")
    _patch_db(monkeypatch, accounts=[a1], recent=[e_orphan])
    monkeypatch.setattr(events, "_last_displayed_emails", [])

    ctx = events._build_context()

    assert "account=uuid-gone" in ctx
