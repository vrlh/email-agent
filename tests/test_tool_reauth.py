"""Tests for ``api.slack.events._tool_reauth``.

Covers behaviors 9-11 from the plan:
- With email + env vars set → message includes target email and reauth URL.
- Without email → returns error listing active accounts; no link sent.
- With email but APP_URL unset → returns config error (regression guard).
"""

from unittest.mock import MagicMock

from api.slack import events


def _patch_accounts(monkeypatch, accounts):
    monkeypatch.setattr("lib.db.get_active_accounts", lambda: list(accounts))


def test_with_email_sends_message_naming_the_account(monkeypatch, make_account):
    # Behavior 9: email + env → _reply called with text containing `for <email>` and URL.
    monkeypatch.setenv("APP_URL", "https://example.com")
    monkeypatch.setenv("SETUP_SECRET", "s3cret")
    _patch_accounts(monkeypatch, [make_account("uuid-a", "a@x.com")])
    reply_mock = MagicMock()
    monkeypatch.setattr(events, "_reply", reply_mock)

    result = events._tool_reauth({"email": "a@x.com"})

    assert reply_mock.call_count == 1
    sent_text = reply_mock.call_args.args[0]
    assert "for" in sent_text
    assert "a@x.com" in sent_text
    assert "https://example.com/api/auth/gmail_start?secret=s3cret" in sent_text
    assert "[Already displayed to user]" in result


def test_without_email_returns_error_and_does_not_send(monkeypatch, make_account):
    # Behavior 10: no email → returns error listing accounts; _reply never called.
    monkeypatch.setenv("APP_URL", "https://example.com")
    monkeypatch.setenv("SETUP_SECRET", "s3cret")
    _patch_accounts(
        monkeypatch,
        [make_account("uuid-a", "a@x.com"), make_account("uuid-b", "b@y.com")],
    )
    reply_mock = MagicMock()
    monkeypatch.setattr(events, "_reply", reply_mock)

    result = events._tool_reauth({})

    assert reply_mock.call_count == 0
    assert "a@x.com" in result
    assert "b@y.com" in result
    # Error should clearly indicate the agent must ask the user (not silently send a generic link).
    assert "Which" in result or "which" in result


def test_email_present_but_app_url_missing_returns_config_error(monkeypatch, make_account):
    # Behavior 11: regression guard — APP_URL unset still returns the existing error message.
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.setenv("SETUP_SECRET", "s3cret")
    _patch_accounts(monkeypatch, [make_account("uuid-a", "a@x.com")])
    reply_mock = MagicMock()
    monkeypatch.setattr(events, "_reply", reply_mock)

    result = events._tool_reauth({"email": "a@x.com"})

    assert "APP_URL is not configured" in result
    assert reply_mock.call_count == 0


def test_reauth_url_carries_hint_query_param(monkeypatch, make_account):
    # A1.1: URL contains &hint=<urlencoded-email> so gmail_start can forward it
    # to Google as login_hint and the callback can verify the chosen account.
    monkeypatch.setenv("APP_URL", "https://example.com")
    monkeypatch.setenv("SETUP_SECRET", "s3cret")
    _patch_accounts(monkeypatch, [make_account("uuid-a", "a@x.com")])
    reply_mock = MagicMock()
    monkeypatch.setattr(events, "_reply", reply_mock)

    events._tool_reauth({"email": "a@x.com"})

    sent_text = reply_mock.call_args.args[0]
    assert "hint=a%40x.com" in sent_text


def test_reauth_url_still_carries_secret(monkeypatch, make_account):
    # A1.2: regression — adding hint must not displace the secret param.
    monkeypatch.setenv("APP_URL", "https://example.com")
    monkeypatch.setenv("SETUP_SECRET", "s3cret")
    _patch_accounts(monkeypatch, [make_account("uuid-a", "a@x.com")])
    reply_mock = MagicMock()
    monkeypatch.setattr(events, "_reply", reply_mock)

    events._tool_reauth({"email": "a@x.com"})

    sent_text = reply_mock.call_args.args[0]
    assert "secret=s3cret" in sent_text
