"""Tests for ``api.cron.check_emails._send_summary`` reauth surfacing.

Covers C1.1-C1.3:
- When N accounts have needs_reauth=True, summary includes a "need reconnect"
  block listing their addresses.
- All-healthy → no reconnect block.
- Reauth-only (no emails, no unreplied) → summary STILL sent (currently bails).
"""

from unittest.mock import MagicMock

from api.cron import check_emails


def _attr(**kw):
    """Lightweight account stand-in with needs_reauth attribute."""
    from types import SimpleNamespace
    base = {"email_address": "x@x.com", "needs_reauth": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _flatten(blocks):
    """Concatenate every text snippet in the block list for substring search."""
    out = []
    for b in blocks or []:
        t = b.get("text")
        if isinstance(t, dict):
            out.append(t.get("text", ""))
        for el in b.get("fields", []) or []:
            out.append(el.get("text", ""))
    return "\n".join(out)


def test_summary_includes_reauth_block_when_accounts_need_reconnect(monkeypatch):
    # C1.1: two accounts need reauth → summary block names both, mentions reconnect.
    accounts = [_attr(email_address="a@x.com", needs_reauth=True),
                _attr(email_address="b@y.com", needs_reauth=True),
                _attr(email_address="c@z.com", needs_reauth=False)]
    monkeypatch.setattr("lib.db.get_active_accounts", lambda: accounts)
    send_mock = MagicMock(return_value="ts-1")
    monkeypatch.setattr("lib.slack_client.send_dm", send_mock)

    check_emails._send_summary({}, total_archived=0, unreplied_emails=[])

    assert send_mock.call_count == 1
    blocks = send_mock.call_args.kwargs.get("blocks") or send_mock.call_args.args[1:][0]
    text = _flatten(blocks)
    assert "reconnect" in text.lower()
    assert "a@x.com" in text
    assert "b@y.com" in text
    assert "c@z.com" not in text  # healthy account not surfaced


def test_summary_omits_reauth_block_when_all_healthy(monkeypatch):
    # C1.2: zero accounts flagged → no reconnect text.
    # Also one new email so we don't trigger the all-empty bail.
    accounts = [_attr(email_address="a@x.com", needs_reauth=False)]
    monkeypatch.setattr("lib.db.get_active_accounts", lambda: accounts)
    send_mock = MagicMock(return_value="ts-1")
    monkeypatch.setattr("lib.slack_client.send_dm", send_mock)

    check_emails._send_summary(
        {"a@x.com": [{"subject": "Hi", "sender_email": "s@x.com", "priority": "normal", "summary": ""}]},
        total_archived=0,
        unreplied_emails=[],
    )

    assert send_mock.call_count == 1
    blocks = send_mock.call_args.kwargs.get("blocks") or send_mock.call_args.args[1:][0]
    text = _flatten(blocks).lower()
    assert "reconnect" not in text


def test_summary_sent_when_only_reauth_pending(monkeypatch):
    # C1.3: no new emails, no unreplied, but ≥1 account flagged → still send.
    # Previously the function bailed with `return 0` in this case.
    accounts = [_attr(email_address="a@x.com", needs_reauth=True)]
    monkeypatch.setattr("lib.db.get_active_accounts", lambda: accounts)
    send_mock = MagicMock(return_value="ts-1")
    monkeypatch.setattr("lib.slack_client.send_dm", send_mock)

    check_emails._send_summary({}, total_archived=0, unreplied_emails=[])

    assert send_mock.call_count == 1
