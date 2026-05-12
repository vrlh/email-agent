"""GET /api/auth/gmail_start — initiate Gmail OAuth2 flow.

Protected by SETUP_SECRET query param.  Redirects the browser to Google's
consent screen requesting offline access (refresh token) with send scope.

Accepts an optional ``hint=<email>`` query param. When present it is forwarded
to Google as ``login_hint`` so the right account is preselected, and embedded
in OAuth ``state`` (``<secret>::<hint>``) so the callback can verify the user
signed in as the requested account.
"""

import os
from http.server import BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def _build_oauth_redirect(
    secret: str,
    hint: Optional[str],
    redirect_uri: str,
    client_id: str,
) -> str:
    """Build the Google OAuth consent URL.

    ``hint`` (when provided) controls two things:
    - ``login_hint`` query param so Google preselects that account.
    - ``state`` encoded as ``<secret>::<hint>`` so the callback can verify
      the user signed in as the requested account.

    When ``hint`` is None, ``state`` is the bare secret (backward compat with
    the initial-add flow where no specific account is targeted).
    """
    state = f"{secret}::{hint}" if hint else secret
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    if hint:
        params["login_hint"] = hint
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        secret = qs.get("secret", [None])[0]

        if secret != os.environ.get("SETUP_SECRET"):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        # Build the redirect URI from APP_URL env var (must match Google Console)
        app_url = os.environ.get("APP_URL", "").rstrip("/")
        if not app_url:
            host = self.headers.get("x-forwarded-host") or self.headers.get("host", "")
            proto = self.headers.get("x-forwarded-proto", "https")
            app_url = f"{proto}://{host}"
        redirect_uri = f"{app_url}/api/auth/gmail_callback"

        hint = qs.get("hint", [None])[0]
        auth_url = _build_oauth_redirect(
            secret=secret,
            hint=hint,
            redirect_uri=redirect_uri,
            client_id=os.environ["GOOGLE_CLIENT_ID"],
        )

        self.send_response(302)
        self.send_header("Location", auth_url)
        self.end_headers()
