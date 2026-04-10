"""Security utilities: Slack signature verification, cron auth, owner check."""

import hashlib
import hmac
import os
import time


def verify_slack_signature(timestamp: str, body: bytes, signature: str) -> bool:
    """Verify a Slack request using the signing secret.

    Rejects requests older than 5 minutes to prevent replay attacks.
    """
    signing_secret = os.environ["SLACK_SIGNING_SECRET"]

    if abs(time.time() - int(timestamp)) > 300:
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = "v0=" + hmac.new(
        signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def verify_cron_secret(authorization_header: str) -> bool:
    """Check that the Authorization header carries the correct cron secret."""
    expected = os.environ["CRON_SECRET"]
    return authorization_header == f"Bearer {expected}"


def is_owner(slack_user_id: str) -> bool:
    """Return True if the Slack user is the configured owner."""
    return slack_user_id == os.environ["OWNER_SLACK_USER_ID"]
