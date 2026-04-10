"""LLM provider router — delegates to Gemini or Claude based on LLM_PROVIDER env var.

Set LLM_PROVIDER=gemini (default, free) or LLM_PROVIDER=claude (production).
"""

import os
from typing import List

from lib.models import DraftContent, Email, ParsedCommand, TriageResult


def _provider():
    name = os.environ.get("LLM_PROVIDER", "gemini")
    if name == "claude":
        from lib.providers import claude as mod
    else:
        from lib.providers import gemini as mod
    return mod


def parse_command(user_text: str, context: str = "") -> ParsedCommand:
    return _provider().parse_command(user_text, context)


def triage_emails(emails: List[Email]) -> List[TriageResult]:
    return _provider().triage_emails(emails)


def summarize_email(email: Email) -> str:
    return _provider().summarize_email(email)


def generate_draft(
    original_email: Email, user_instruction: str, account_email: str,
) -> DraftContent:
    return _provider().generate_draft(original_email, user_instruction, account_email)


def edit_draft(current_body: str, edit_instruction: str) -> str:
    return _provider().edit_draft(current_body, edit_instruction)
