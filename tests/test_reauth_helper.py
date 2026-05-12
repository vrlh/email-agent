"""Tests for ``lib.reauth`` pure helpers.

The helper builds the reauth URL and the user-facing Slack message. Used by
both ``_tool_reauth`` (agent-driven) and the cron's RefreshError trap path
(proactive notification), so a single source of truth keeps wording in sync.
"""

from lib import reauth


def test_build_reauth_url_contains_secret_and_hint():
    # Behavior 1: URL carries both query params, with the hint urlencoded.
    url = reauth.build_reauth_url(
        app_url="https://example.com",
        setup_secret="s3cret",
        target_email="a@x.com",
    )
    assert "secret=s3cret" in url
    assert "hint=a%40x.com" in url
    assert url.startswith("https://example.com/api/auth/gmail_start?")


def test_build_reauth_url_strips_trailing_slash():
    # Behavior 2: matches existing _tool_reauth behavior (app_url.rstrip("/")).
    url = reauth.build_reauth_url(
        app_url="https://example.com/",
        setup_secret="s3cret",
        target_email="a@x.com",
    )
    # No double slash before the path.
    assert "https://example.com/api/auth/gmail_start?" in url
    assert "//api/auth" not in url


def test_build_reauth_message_includes_target_and_url():
    # Behavior 3: message is Slack mrkdwn, names the account, and embeds the link.
    msg = reauth.build_reauth_message(
        app_url="https://example.com",
        setup_secret="s3cret",
        target_email="a@x.com",
    )
    assert "for a@x.com" in msg
    assert "sign in as" in msg
    # The clickable Slack link syntax <url|label>.
    assert "<https://example.com/api/auth/gmail_start?" in msg
    assert "|Click here" in msg
