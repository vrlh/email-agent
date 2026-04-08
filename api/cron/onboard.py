"""GET /api/cron/onboard — backfill last 3 months of inbox emails.

Protected by CRON_SECRET. Optional ?account_id= to onboard a single account.
"""

import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from lib.security import verify_cron_secret

logger = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if not verify_cron_secret(auth):
            self._respond(403, {"error": "Forbidden"})
            return

        qs = parse_qs(urlparse(self.path).query)
        account_id = qs.get("account_id", [None])[0]

        try:
            from lib.onboard import run_onboard
            stats = run_onboard(account_id=account_id)
            self._respond(200, stats)
        except Exception as exc:
            logger.error(f"Onboard failed: {exc}")
            self._respond(500, {"error": str(exc)})

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())
