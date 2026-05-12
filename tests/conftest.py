"""Shared pytest fixtures and path setup.

The repo isn't a proper package, so we prepend the repo root to ``sys.path`` so
tests can ``from api.slack import events`` and ``from lib import agent``.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _account(id_: str, email_address: str) -> SimpleNamespace:
    return SimpleNamespace(id=id_, email_address=email_address)


def _email(id_: str, account_id: str, sender_email: str, subject: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_, account_id=account_id, sender_email=sender_email, subject=subject,
    )


def _draft(id_: str, status: str, subject: str) -> SimpleNamespace:
    return SimpleNamespace(id=id_, status=status, subject=subject)


@pytest.fixture
def make_account():
    return _account


@pytest.fixture
def make_email():
    return _email


@pytest.fixture
def make_draft():
    return _draft
