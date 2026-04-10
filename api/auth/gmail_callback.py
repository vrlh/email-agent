"""GET /api/auth/gmail_callback — OAuth2 token exchange and account storage.

Google redirects here after the user consents.  We exchange the auth code
for tokens, encrypt them, and upsert a gmail_accounts row.
"""

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from lib.crypto import encrypt_tokens
from lib.db import create_tables, upsert_account


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        code = qs.get("code", [None])[0]
        state = qs.get("state", [None])[0]

        # Verify state matches SETUP_SECRET
        if state != os.environ.get("SETUP_SECRET") or not code:
            self._respond(400, "Invalid callback parameters.")
            return

        try:
            tokens = self._exchange_code(code)
            email_address, display_name = self._get_user_profile(tokens)

            # Encrypt and store
            encrypted = encrypt_tokens(json.dumps(tokens))
            create_tables()
            account = upsert_account(
                account_id=str(uuid.uuid4()),
                email_address=email_address,
                display_name=display_name,
                encrypted_tokens=encrypted,
            )

            # Notify Slack and auto-onboard
            try:
                from lib.slack_client import send_dm
                send_dm(f"\U0001f4e5 *New account connected: {email_address}*\nStarting email onboard (this may take a minute)...")
                from lib.onboard import run_onboard
                run_onboard(account_id=account.id)
            except Exception as onboard_exc:
                # Don't fail the OAuth flow if onboard fails
                from lib.slack_client import send_dm
                send_dm(f"\u26a0\ufe0f Account connected but onboard failed: {onboard_exc}\nType \"onboard\" in Slack to retry.")

            self._respond(
                200,
                f"<h2>Connected {email_address}</h2>"
                f"<p>Account added and email onboarding started. Check Slack for updates.</p>",
                content_type="text/html",
            )
        except Exception as exc:
            self._respond(500, f"Error: {exc}")

    def _exchange_code(self, code: str) -> dict:
        """Exchange authorization code for access + refresh tokens."""
        app_url = os.environ.get("APP_URL", "").rstrip("/")
        if not app_url:
            host = self.headers.get("x-forwarded-host") or self.headers.get("host", "")
            proto = self.headers.get("x-forwarded-proto", "https")
            app_url = f"{proto}://{host}"
        redirect_uri = f"{app_url}/api/auth/gmail_callback"

        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "scopes": data.get("scope", "").split(),
        }

    def _get_user_profile(self, tokens: dict) -> tuple[str, str]:
        """Fetch the authenticated user's email and name."""
        creds = Credentials(
            token=tokens["token"],
            refresh_token=tokens.get("refresh_token"),
            token_uri=tokens["token_uri"],
            client_id=tokens["client_id"],
            client_secret=tokens["client_secret"],
        )
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        try:
            profile = service.users().getProfile(userId="me").execute()
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch Gmail profile: {exc}") from exc
        email_address = profile.get("emailAddress", "")
        # Gmail profile doesn't return a display name; use email prefix
        display_name = email_address.split("@")[0] if email_address else ""
        return email_address, display_name

    def _respond(self, status: int, body: str, content_type: str = "text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body.encode())
