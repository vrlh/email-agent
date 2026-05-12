"""Tests for ``api.slack.events._tool_find_contact``.

Covers behaviors 9-14 from the plan:
- DB hit → no Gmail fallback.
- DB miss → Gmail fallback per active account, skipping needs_reauth.
- 1 hit / multi hit / 0 hit output shapes.
- Min-length guard.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from api.slack import events


def test_db_hit_skips_gmail_fallback(monkeypatch):
    # Behavior 9: DB returns ≥1 → search_addresses must not be called.
    db_hit = [{"email": "a@x.com", "name": "Alex", "last_seen": None, "frequency": 7}]
    monkeypatch.setattr("lib.db.search_contacts", lambda q, limit=10: db_hit)
    gmail_mock = MagicMock()
    monkeypatch.setattr("lib.gmail.search_addresses", gmail_mock)

    result = events._tool_find_contact({"query": "alex"})

    gmail_mock.assert_not_called()
    assert "inbox history" in result
    assert "a@x.com" in result


def test_db_miss_falls_back_to_gmail_skipping_needs_reauth(monkeypatch, make_account):
    # Behavior 10: DB empty → search_addresses called per active account,
    # except those flagged needs_reauth.
    monkeypatch.setattr("lib.db.search_contacts", lambda q, limit=10: [])

    healthy = make_account("uuid-a", "a@x.com")
    healthy.encrypted_tokens = "enc-a"
    healthy.needs_reauth = False

    broken = make_account("uuid-b", "b@y.com")
    broken.encrypted_tokens = "enc-b"
    broken.needs_reauth = True

    monkeypatch.setattr("lib.db.get_active_accounts", lambda: [healthy, broken])
    monkeypatch.setattr("lib.gmail.credentials_from_encrypted", lambda enc: MagicMock(name="creds"))
    monkeypatch.setattr("lib.gmail.refresh_if_needed", lambda c: (c, False))

    gmail_calls = []
    def fake_search(creds, query, max_results=20):
        gmail_calls.append(query)
        return [{"email": "jeff@x.com", "name": "Jeffrey",
                 "last_seen": datetime(2024, 5, 1, tzinfo=timezone.utc), "frequency": 1}]
    monkeypatch.setattr("lib.gmail.search_addresses", fake_search)

    result = events._tool_find_contact({"query": "jeffrey"})

    # Called exactly once — for the healthy account, not the flagged one.
    assert len(gmail_calls) == 1
    assert "full Gmail history" in result
    assert "jeff@x.com" in result


def test_single_hit_output_format(monkeypatch):
    # Behavior 11: 1 hit → "One match" output shape.
    monkeypatch.setattr(
        "lib.db.search_contacts",
        lambda q, limit=10: [
            {"email": "jeff@x.com", "name": "Jeffrey", "last_seen": None, "frequency": 3}
        ],
    )
    monkeypatch.setattr("lib.db.get_active_accounts", lambda: [])

    result = events._tool_find_contact({"query": "jeffrey"})

    assert result.startswith("One match")
    assert "<jeff@x.com>" in result


def test_multi_hit_output_lists_candidates_numbered(monkeypatch):
    # Behavior 12: ≥2 hits → numbered list with last-seen + frequency.
    hits = [
        {"email": "jeff@x.com", "name": "Jeffrey L", "last_seen": datetime(2024, 5, 1, tzinfo=timezone.utc), "frequency": 12},
        {"email": "jeffrey@y.com", "name": "Jeffrey A", "last_seen": datetime(2024, 4, 1, tzinfo=timezone.utc), "frequency": 3},
    ]
    monkeypatch.setattr("lib.db.search_contacts", lambda q, limit=10: hits)
    monkeypatch.setattr("lib.db.get_active_accounts", lambda: [])

    result = events._tool_find_contact({"query": "jeffrey"})

    assert result.startswith("Found 2 matches")
    assert "1. Jeffrey L" in result
    assert "2. Jeffrey A" in result
    assert "12 email" in result


def test_zero_hits_tells_agent_to_ask_user(monkeypatch):
    # Behavior 13: DB empty + Gmail returns nothing → asks the user for the address.
    monkeypatch.setattr("lib.db.search_contacts", lambda q, limit=10: [])
    monkeypatch.setattr("lib.db.get_active_accounts", lambda: [])

    result = events._tool_find_contact({"query": "nobody"})

    assert "No contacts found" in result
    assert "Ask the user" in result


def test_min_length_guard_returns_message_without_db_call(monkeypatch):
    # Behavior 14: query shorter than 2 → guard message, no DB call.
    search_mock = MagicMock()
    monkeypatch.setattr("lib.db.search_contacts", search_mock)

    result = events._tool_find_contact({"query": "a"})

    assert "at least 2 characters" in result
    search_mock.assert_not_called()
