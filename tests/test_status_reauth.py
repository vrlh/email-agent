"""Tests for status surfacing of needs_reauth in ``build_status_blocks`` and
``_tool_status``.

Covers C2.1-C2.2.
"""

from unittest.mock import MagicMock

from lib import slack_client


def _flatten(blocks):
    out = []
    for b in blocks or []:
        t = b.get("text")
        if isinstance(t, dict):
            out.append(t.get("text", ""))
    return "\n".join(out)


def test_build_status_blocks_marks_account_needing_reauth():
    # C2.1: account with needs_reauth=True → block text contains a reconnect marker.
    blocks = slack_client.build_status_blocks(
        accounts=[
            {"email": "a@x.com", "last_sync": "Jan 01, 12:00", "needs_reauth": True},
            {"email": "b@y.com", "last_sync": "Jan 01, 12:00", "needs_reauth": False},
        ],
        pending_draft=False,
    )

    text = _flatten(blocks).lower()
    # The flagged account should have a reconnect indicator near its address.
    assert "needs reconnect" in text or "reconnect" in text
    # Find the line for a@x.com and check it contains the marker.
    flat = _flatten(blocks)
    a_line = next(line for line in flat.splitlines() if "a@x.com" in line)
    assert "reconnect" in a_line.lower()


def test_build_status_blocks_omits_marker_when_all_healthy():
    # C2.2: no accounts flagged → no reconnect marker.
    blocks = slack_client.build_status_blocks(
        accounts=[{"email": "a@x.com", "last_sync": "Jan 01, 12:00", "needs_reauth": False}],
        pending_draft=False,
    )
    text = _flatten(blocks).lower()
    assert "reconnect" not in text


def test_tool_status_passes_needs_reauth_into_block_builder(monkeypatch, make_account):
    # _tool_status threads needs_reauth from the ORM into the dict passed to
    # build_status_blocks, so the marker can render.
    from api.slack import events

    a = make_account("uuid-a", "a@x.com")
    a.last_sync_at = None
    a.needs_reauth = True

    monkeypatch.setattr("lib.db.get_active_accounts", lambda: [a])
    monkeypatch.setattr("lib.db.get_pending_draft", lambda: None)
    reply_mock = MagicMock()
    monkeypatch.setattr(events, "_reply", reply_mock)

    events._tool_status({})

    blocks = reply_mock.call_args.kwargs.get("blocks") or reply_mock.call_args.args[1]
    text = _flatten(blocks).lower()
    assert "reconnect" in text
