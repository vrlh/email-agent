"""Tests for ``api.auth.gmail_start._build_oauth_redirect``.

Covers behaviors A2.1-A2.3:
- With hint, login_hint is in the URL and state is ``<secret>::<hint>``.
- Without hint, no login_hint and state is the bare secret (backward compat).
"""

from urllib.parse import parse_qs, urlparse

from api.auth import gmail_start


def _query_params(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_redirect_with_hint_includes_login_hint_param():
    # A2.1: hint=a@x.com → URL contains login_hint=a@x.com (urlencoded).
    url = gmail_start._build_oauth_redirect(
        secret="s3cret",
        hint="a@x.com",
        redirect_uri="https://app.example.com/api/auth/gmail_callback",
        client_id="client-123",
    )
    params = _query_params(url)
    assert params.get("login_hint") == "a@x.com"


def test_redirect_with_hint_encodes_state_with_double_colon():
    # A2.2: state decodes to <secret>::<hint>.
    url = gmail_start._build_oauth_redirect(
        secret="s3cret",
        hint="a@x.com",
        redirect_uri="https://app.example.com/api/auth/gmail_callback",
        client_id="client-123",
    )
    params = _query_params(url)
    assert params.get("state") == "s3cret::a@x.com"


def test_redirect_without_hint_has_bare_state_and_no_login_hint():
    # A2.3: backward compat — no hint means no login_hint, state is bare secret.
    url = gmail_start._build_oauth_redirect(
        secret="s3cret",
        hint=None,
        redirect_uri="https://app.example.com/api/auth/gmail_callback",
        client_id="client-123",
    )
    params = _query_params(url)
    assert "login_hint" not in params
    assert params.get("state") == "s3cret"


def test_redirect_targets_google_oauth_endpoint():
    # Regression guard: URL still points at Google's OAuth endpoint with the right scopes.
    url = gmail_start._build_oauth_redirect(
        secret="s3cret",
        hint=None,
        redirect_uri="https://app.example.com/api/auth/gmail_callback",
        client_id="client-123",
    )
    parsed = urlparse(url)
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"
    params = _query_params(url)
    assert params.get("client_id") == "client-123"
    assert params.get("redirect_uri") == "https://app.example.com/api/auth/gmail_callback"
    assert "gmail.send" in params.get("scope", "")
