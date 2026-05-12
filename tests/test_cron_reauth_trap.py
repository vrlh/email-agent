"""Tests for ``api.cron.check_emails._process_account`` reauth handling.

Covers B2.1-B2.3:
- RefreshError raised by refresh_if_needed → mark_account_needs_reauth(True),
  no exception bubbles, returns {"error": "needs_reauth"}.
- Account where account.needs_reauth=True is skipped without ever calling
  Gmail credentials/refresh/fetch.
- Successful refresh does NOT touch the flag (regression).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from google.auth.exceptions import RefreshError

from api.cron import check_emails


def _account(**overrides) -> SimpleNamespace:
    base = {
        "id": "uuid-a",
        "email_address": "a@x.com",
        "encrypted_tokens": "enc",
        "last_history_id": None,
        "last_sync_at": None,
        "needs_reauth": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_minimal_pipeline(monkeypatch):
    """Stub out everything _process_account needs except the bits under test."""
    monkeypatch.setattr("lib.db.create_sync_log", lambda *a, **kw: "log-id")
    monkeypatch.setattr(
        "lib.db.complete_sync_log", lambda *a, **kw: None,
    )
    monkeypatch.setattr("lib.db.update_account_sync", lambda *a, **kw: None)
    monkeypatch.setattr("lib.db.update_account_tokens", lambda *a, **kw: None)
    monkeypatch.setattr("lib.db.upsert_emails", lambda *a, **kw: set())
    monkeypatch.setattr("lib.db.get_email_by_id", lambda *a, **kw: None)
    monkeypatch.setattr("lib.db.mark_email_notified", lambda *a, **kw: None)
    monkeypatch.setattr("lib.db.get_unreplied_thread_ids", lambda *a, **kw: [])
    # The existing except-block calls _notify_account_error which sends a Slack
    # DM. Stub it so the test doesn't need SLACK_BOT_TOKEN. If a test sees this
    # called it means the trap didn't fire — also informative.
    monkeypatch.setattr(check_emails, "_notify_account_error", lambda *a, **kw: None)


def test_refresh_error_marks_account_and_returns_without_raising(monkeypatch):
    # B2.1: RefreshError trapped → needs_reauth flipped True → no exception.
    _patch_minimal_pipeline(monkeypatch)

    mark_mock = MagicMock()
    monkeypatch.setattr("lib.db.mark_account_needs_reauth", mark_mock)
    monkeypatch.setattr(
        "lib.gmail.credentials_from_encrypted", lambda enc: MagicMock(name="creds"),
    )

    def boom(_creds):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr("lib.gmail.refresh_if_needed", boom)

    result = check_emails._process_account(_account())

    mark_mock.assert_called_once_with("uuid-a", True)
    assert result.get("error") == "needs_reauth"
    assert result.get("account") == "a@x.com"


def test_account_flagged_needs_reauth_is_skipped(monkeypatch):
    # B2.2: account.needs_reauth=True → no creds/refresh/fetch calls happen.
    _patch_minimal_pipeline(monkeypatch)

    mark_mock = MagicMock()
    monkeypatch.setattr("lib.db.mark_account_needs_reauth", mark_mock)

    creds_mock = MagicMock(name="credentials_from_encrypted")
    refresh_mock = MagicMock(name="refresh_if_needed")
    fetch_mock = MagicMock(name="fetch_new_emails")
    monkeypatch.setattr("lib.gmail.credentials_from_encrypted", creds_mock)
    monkeypatch.setattr("lib.gmail.refresh_if_needed", refresh_mock)
    monkeypatch.setattr("lib.gmail.fetch_new_emails", fetch_mock)

    result = check_emails._process_account(_account(needs_reauth=True))

    assert creds_mock.call_count == 0
    assert refresh_mock.call_count == 0
    assert fetch_mock.call_count == 0
    mark_mock.assert_not_called()  # we don't re-flag what's already flagged
    assert result.get("skipped") == "needs_reauth"


def test_successful_refresh_does_not_touch_needs_reauth_flag(monkeypatch):
    # B2.3: regression — healthy refresh path never calls mark_account_needs_reauth.
    _patch_minimal_pipeline(monkeypatch)

    mark_mock = MagicMock()
    monkeypatch.setattr("lib.db.mark_account_needs_reauth", mark_mock)
    monkeypatch.setattr(
        "lib.gmail.credentials_from_encrypted", lambda enc: MagicMock(name="creds"),
    )
    monkeypatch.setattr(
        "lib.gmail.refresh_if_needed", lambda c: (c, False),
    )
    monkeypatch.setattr(
        "lib.gmail.fetch_new_emails", lambda *a, **kw: ([], "history-1"),
    )

    result = check_emails._process_account(_account())

    mark_mock.assert_not_called()
    # Healthy "no new mail" path returns fetched=0.
    assert result.get("fetched") == 0
