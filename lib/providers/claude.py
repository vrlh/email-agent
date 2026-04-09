"""Anthropic Claude provider — triage, summarize, draft, parse commands."""

import json
import logging
import os
from typing import List, Optional

import anthropic

from lib.models import (
    DraftContent,
    Email,
    EmailAddress,
    ParsedCommand,
    TriageDecision,
    TriageResult,
)
from lib.providers._prompts import (
    COMMAND_PARSE_SYSTEM,
    DRAFT_EDIT_SYSTEM,
    DRAFT_GENERATE_SYSTEM,
    SUMMARIZE_SYSTEM,
    TRIAGE_SYSTEM,
    build_draft_user_prompt,
    build_triage_user_prompt,
    build_summarize_user_prompt,
    build_command_user_prompt,
    build_edit_user_prompt,
    strip_fences,
)

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"


def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _chat(system: str, user_prompt: str, max_tokens: int = 1024) -> str:
    client = _get_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return resp.content[0].text.strip()


def parse_command(user_text: str, context: str = "") -> ParsedCommand:
    try:
        text = strip_fences(_chat(
            COMMAND_PARSE_SYSTEM,
            build_command_user_prompt(user_text, context),
            max_tokens=256,
        ))
        parsed = json.loads(text)
        return ParsedCommand(
            intent=parsed.get("intent", "unknown"),
            params=parsed.get("params", {}),
            raw_text=user_text,
        )
    except Exception as exc:
        logger.error(f"Command parse failed: {exc}")
        return ParsedCommand(intent="unknown", params={"raw": user_text}, raw_text=user_text)


def triage_emails(emails: List[Email]) -> List[TriageResult]:
    if not emails:
        return []
    try:
        text = strip_fences(_chat(
            TRIAGE_SYSTEM,
            build_triage_user_prompt(emails),
        ))
        items = json.loads(text)
        results: List[TriageResult] = []
        for i, item in enumerate(items):
            if i >= len(emails):
                break
            results.append(
                TriageResult(
                    email_id=emails[i].id,
                    attention_score=float(item.get("attention_score", 0.5)),
                    decision=TriageDecision(item.get("decision", "needs_attention")),
                    summary=item.get("summary", emails[i].subject)[:200],
                    needs_reply=bool(item.get("needs_reply", False)),
                    suggested_action=item.get("suggested_action"),
                )
            )
        return results
    except Exception as exc:
        logger.error(f"Claude triage failed: {exc}")
        return [
            TriageResult(
                email_id=e.id, attention_score=0.5,
                decision=TriageDecision.NEEDS_ATTENTION,
                summary=e.subject[:200], needs_reply=False,
            )
            for e in emails
        ]


def summarize_email(email: Email) -> str:
    try:
        return _chat(SUMMARIZE_SYSTEM, build_summarize_user_prompt(email), max_tokens=300)
    except Exception as exc:
        logger.error(f"Summarize failed: {exc}")
        return email.subject


def generate_draft(original_email: Email, user_instruction: str, account_email: str) -> DraftContent:
    try:
        text = strip_fences(_chat(
            DRAFT_GENERATE_SYSTEM,
            build_draft_user_prompt(original_email, user_instruction, account_email),
        ))
        data = json.loads(text)
        return DraftContent(
            subject=data.get("subject", f"Re: {original_email.subject}"),
            body_text=data.get("body", ""),
            to_addresses=[original_email.sender],
        )
    except Exception as exc:
        logger.error(f"Draft generation failed: {exc}")
        return DraftContent(
            subject=f"Re: {original_email.subject}",
            body_text=f"[Draft generation failed]\n\nRegarding: {user_instruction}",
            to_addresses=[original_email.sender],
        )


def edit_draft(current_body: str, edit_instruction: str) -> str:
    try:
        return _chat(DRAFT_EDIT_SYSTEM, build_edit_user_prompt(current_body, edit_instruction))
    except Exception as exc:
        logger.error(f"Draft edit failed: {exc}")
        return current_body
