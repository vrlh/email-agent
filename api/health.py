"""GET /api/health — basic status check and DB bootstrap."""

import json
from http.server import BaseHTTPRequestHandler

from lib.db import create_tables, get_engine


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            # Ensure tables exist (idempotent)
            create_tables()

            # Quick connectivity check
            with get_engine().connect() as conn:
                conn.execute(__import__("sqlalchemy").text("SELECT 1"))

            body = json.dumps({"status": "ok"})
            self.send_response(200)
        except Exception as exc:
            body = json.dumps({"status": "error", "detail": str(exc)})
            self.send_response(500)

        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())
