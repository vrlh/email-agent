"""Tests for ``lib.gmail.search_addresses``.

Covers behaviors 4-8 from the plan: q-construction, quote-stripping,
aggregation, filtering, and empty-result handling.
"""

from unittest.mock import MagicMock

from lib import gmail


def _service(list_response=None, message_responses=None):
    """Build a fake googleapiclient discovery service.

    *list_response*: dict like ``{"messages": [{"id": "m1"}, ...]}``.
    *message_responses*: dict mapping message id -> headers dict.
    """
    service = MagicMock()
    messages = service.users.return_value.messages.return_value

    list_chain = MagicMock()
    list_chain.execute.return_value = list_response or {"messages": []}
    messages.list.return_value = list_chain

    def get_side_effect(userId, id, format, metadataHeaders):
        chain = MagicMock()
        chain.execute.return_value = {
            "payload": {
                "headers": [
                    {"name": k, "value": v}
                    for k, v in (message_responses or {}).get(id, {}).items()
                ]
            }
        }
        return chain

    messages.get.side_effect = get_side_effect
    return service


def test_search_addresses_calls_list_with_from_to_cc_query(monkeypatch):
    # Behavior 4: q contains from:, to:, cc: clauses with the query.
    fake = _service(list_response={"messages": []})
    monkeypatch.setattr(gmail, "_build_service", lambda creds: fake)

    gmail.search_addresses(creds=MagicMock(), query="jeffrey", max_results=5)

    list_call = fake.users.return_value.messages.return_value.list
    q_arg = list_call.call_args.kwargs.get("q", "")
    assert 'from:"jeffrey"' in q_arg
    assert 'to:"jeffrey"' in q_arg
    assert 'cc:"jeffrey"' in q_arg
    assert list_call.call_args.kwargs.get("maxResults") == 5


def test_search_addresses_strips_quotes_from_query(monkeypatch):
    # Behavior 5: raw " in the query is removed before building q (injection guard).
    fake = _service(list_response={"messages": []})
    monkeypatch.setattr(gmail, "_build_service", lambda creds: fake)

    gmail.search_addresses(creds=MagicMock(), query='foo"OR x')

    q_arg = fake.users.return_value.messages.return_value.list.call_args.kwargs["q"]
    # The substring inside the from:"..." quotes must not contain a raw quote.
    assert 'from:"fooOR x"' in q_arg


def test_search_addresses_aggregates_duplicates(monkeypatch):
    # Behavior 6: two messages with the same From → one contact with frequency=2.
    fake = _service(
        list_response={"messages": [{"id": "m1"}, {"id": "m2"}]},
        message_responses={
            "m1": {"From": "Jeffrey <jeff@x.com>", "Date": "Tue, 1 May 2024 10:00:00 +0000"},
            "m2": {"From": "Jeffrey <jeff@x.com>", "Date": "Wed, 2 May 2024 10:00:00 +0000"},
        },
    )
    monkeypatch.setattr(gmail, "_build_service", lambda creds: fake)

    result = gmail.search_addresses(creds=MagicMock(), query="jeffrey")

    assert len(result) == 1
    assert result[0]["email"] == "jeff@x.com"
    assert result[0]["frequency"] == 2
    assert result[0]["name"] == "Jeffrey"


def test_search_addresses_filters_non_matching_headers(monkeypatch):
    # Behavior 7: a message with both Geoff (From) and Jeffrey (To) → only
    # Jeffrey survives when searching "jeffrey".
    fake = _service(
        list_response={"messages": [{"id": "m1"}]},
        message_responses={
            "m1": {
                "From": "Geoff <geoff@x.com>",
                "To": "Jeffrey <jeff@y.com>",
                "Date": "Mon, 1 Jan 2024 10:00:00 +0000",
            },
        },
    )
    monkeypatch.setattr(gmail, "_build_service", lambda creds: fake)

    result = gmail.search_addresses(creds=MagicMock(), query="jeffrey")

    emails = [c["email"] for c in result]
    assert "jeff@y.com" in emails
    assert "geoff@x.com" not in emails


def test_search_addresses_empty_list_response(monkeypatch):
    # Behavior 8: no messages in the list response → empty result, no get() calls.
    fake = _service(list_response={"messages": []})
    monkeypatch.setattr(gmail, "_build_service", lambda creds: fake)

    result = gmail.search_addresses(creds=MagicMock(), query="jeffrey")

    assert result == []
    fake.users.return_value.messages.return_value.get.assert_not_called()
