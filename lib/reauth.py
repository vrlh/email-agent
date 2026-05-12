"""Pure helpers for building Gmail reauth links and the Slack message
that wraps them.

Used by both ``api.slack.events._tool_reauth`` (agent-driven, when the user
asks to reconnect an account) and ``api.cron.check_emails._process_account``
(proactive, when a token refresh fails). Centralizing the wording keeps the
two surfaces in sync so the user sees the same instructions either way.
"""

from urllib.parse import urlencode


def build_reauth_url(app_url: str, setup_secret: str, target_email: str) -> str:
    """Return the gmail_start URL with the account hint embedded.

    The ``hint`` query param flows through ``api/auth/gmail_start.py`` into
    Google's ``login_hint`` so the right account is preselected, and into
    OAuth ``state`` so the callback can verify the user signed in as the
    requested account.
    """
    base = app_url.rstrip("/")
    return f"{base}/api/auth/gmail_start?{urlencode({'secret': setup_secret, 'hint': target_email})}"


def build_reauth_message(app_url: str, setup_secret: str, target_email: str) -> str:
    """Return the Slack-mrkdwn message that wraps the reauth link."""
    url = build_reauth_url(app_url, setup_secret, target_email)
    return (
        f"\U0001f511 *Reconnect Gmail for {target_email}*\n"
        f"<{url}|Click here to re-authenticate>, then sign in as *{target_email}* "
        "to refresh the tokens. Existing emails and rules are preserved."
    )
