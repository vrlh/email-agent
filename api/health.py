"""GET /api/health — basic status check, DB bootstrap, and migrations."""

import json
from http.server import BaseHTTPRequestHandler

from lib.db import create_tables, get_engine


# Migrations: add columns that may be missing from earlier deployments.
# Each is idempotent (IF NOT EXISTS / safe to re-run).
_MIGRATIONS = [
    "ALTER TABLE emails ADD COLUMN IF NOT EXISTS replied_at TIMESTAMPTZ",
    "CREATE TABLE IF NOT EXISTS user_rules ("
    "  id TEXT PRIMARY KEY,"
    "  rule_type TEXT NOT NULL,"
    "  field TEXT NOT NULL,"
    "  operator TEXT NOT NULL,"
    "  value TEXT NOT NULL,"
    "  action TEXT NOT NULL,"
    "  enabled BOOLEAN NOT NULL DEFAULT TRUE,"
    "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    ")",
]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            # Ensure tables exist (idempotent)
            create_tables()

            # Run migrations
            _run_migrations()

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


def _run_migrations():
    from sqlalchemy import text
    engine = get_engine()
    with engine.connect() as conn:
        for sql in _MIGRATIONS:
            conn.execute(text(sql))
        conn.commit()
