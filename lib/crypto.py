"""Fernet encryption for OAuth tokens stored in Postgres."""

import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.environ["DATABASE_ENCRYPTION_KEY"]
    return Fernet(key.encode())


def encrypt_tokens(token_json: str) -> str:
    return _get_fernet().encrypt(token_json.encode()).decode()


def decrypt_tokens(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()
