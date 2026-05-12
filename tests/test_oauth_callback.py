"""Tests for ``api.auth.gmail_callback._parse_state`` and ``_hint_matches``.

Covers behaviors A3.1, A3.2, A3.4 (state parsing) and the mismatch check
that drives A3.3.
"""

from api.auth import gmail_callback


# ---------- _parse_state ----------


def test_parse_state_bare_secret_no_hint():
    # A3.1: bare secret (initial-add flow) → valid, no hint.
    ok, hint = gmail_callback._parse_state("s3cret", "s3cret")
    assert ok is True
    assert hint is None


def test_parse_state_secret_with_hint():
    # A3.2: <secret>::<hint> → valid, hint extracted.
    ok, hint = gmail_callback._parse_state("s3cret::a@x.com", "s3cret")
    assert ok is True
    assert hint == "a@x.com"


def test_parse_state_wrong_secret():
    # A3.4: wrong secret in state → invalid, regardless of hint.
    ok, _ = gmail_callback._parse_state("wrong::a@x.com", "s3cret")
    assert ok is False


def test_parse_state_none_or_empty():
    # Edge: missing state → invalid.
    ok, _ = gmail_callback._parse_state(None, "s3cret")
    assert ok is False
    ok, _ = gmail_callback._parse_state("", "s3cret")
    assert ok is False


# ---------- _hint_matches ----------


def test_hint_matches_when_none():
    # No hint → always matches (initial-add flow has nothing to verify).
    assert gmail_callback._hint_matches(None, "anything@x.com") is True


def test_hint_matches_case_insensitive():
    # Email comparison is case-insensitive — Gmail addresses are case-insensitive.
    assert gmail_callback._hint_matches("A@X.com", "a@x.com") is True


def test_hint_matches_mismatch():
    # Driver for A3.3: hint and returned email differ → False (callback rejects upsert).
    assert gmail_callback._hint_matches("a@x.com", "b@x.com") is False
