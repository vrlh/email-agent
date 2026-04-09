"""GET /api/auth/gmail_start — initiate Gmail OAuth2 flow.

Protected by SETUP_SECRET query param.  Redirects the browser to Google's
consent screen requesting offline access (refresh token) with send scope.
"""

import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


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

        params = urlencode({
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",  # force refresh token even if previously authorized
            "state": os.environ.get("SETUP_SECRET", ""),
        })

        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{params}"

        self.send_response(302)
        self.send_header("Location", auth_url)
        self.end_headers()
