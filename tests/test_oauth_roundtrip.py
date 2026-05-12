"""Integration test for Phase A — agent → start → callback state round-trip.

No internal modules are mocked. Only ``_reply``, env vars, and ``get_active_accounts``
are stubbed (they're external to the OAuth contract). The point is to prove the
state encoding produced by ``gmail_start._build_oauth_redirect`` is decoded
correctly by ``gmail_callback._parse_state`` AND that the agent's reauth link
feeds the hint into start.
"""

from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

from api.auth import gmail_callback, gmail_start
from api.slack import events


def test_agent_link_carries_hint_into_state_then_callback_extracts_it(monkeypatch, make_account):
    # The full chain: agent emits link → start would build the OAuth URL with
    # state=<secret>::<hint> → callback decodes the state and recovers the hint.
    monkeypatch.setenv("APP_URL", "https://app.example.com")
    monkeypatch.setenv("SETUP_SECRET", "s3cret")
    monkeypatch.setattr(
        "lib.db.get_active_accounts",
        lambda: [make_account("uuid-a", "a@x.com")],
    )
    reply_mock = MagicMock()
    monkeypatch.setattr(events, "_reply", reply_mock)

    # Step 1: agent emits the Slack message with the reauth URL.
    events._tool_reauth({"email": "a@x.com"})
    sent_text = reply_mock.call_args.args[0]

    # Extract the URL the user would click (between < and |).
    start_url = sent_text.split("<", 1)[1].split("|", 1)[0]
    start_qs = {k: v[0] for k, v in parse_qs(urlparse(start_url).query).items()}
    assert start_qs["secret"] == "s3cret"
    assert start_qs["hint"] == "a@x.com"

    # Step 2: gmail_start (real code path — pure function) builds the Google OAuth URL.
    google_url = gmail_start._build_oauth_redirect(
        secret=start_qs["secret"],
        hint=start_qs["hint"],
        redirect_uri="https://app.example.com/api/auth/gmail_callback",
        client_id="client-123",
    )
    google_qs = {k: v[0] for k, v in parse_qs(urlparse(google_url).query).items()}
    assert google_qs["login_hint"] == "a@x.com"

    # Step 3: callback receives the state from Google and decodes it.
    ok, recovered_hint = gmail_callback._parse_state(google_qs["state"], "s3cret")
    assert ok is True
    assert recovered_hint == "a@x.com"


def test_initial_add_flow_round_trips_without_hint(monkeypatch):
    # Backward compat: when no hint is in the agent-side URL (e.g. /help link
    # for adding a brand-new account), state must round-trip as bare secret
    # and the callback must accept it.
    google_url = gmail_start._build_oauth_redirect(
        secret="s3cret",
        hint=None,
        redirect_uri="https://app.example.com/api/auth/gmail_callback",
        client_id="client-123",
    )
    state = {k: v[0] for k, v in parse_qs(urlparse(google_url).query).items()}["state"]

    ok, hint = gmail_callback._parse_state(state, "s3cret")
    assert ok is True
    assert hint is None
