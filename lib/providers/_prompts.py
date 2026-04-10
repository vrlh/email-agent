"""Shared system prompts and prompt builders for all LLM providers."""

from typing import List

from lib.models import Email


# ── System prompts ──

COMMAND_PARSE_SYSTEM = (
    "You are an email assistant that parses user messages into commands. "
    "Return a JSON object with exactly two keys:\n"
    '  "intent": one of "list", "summarize", "reply", "archive", "send", '
    '"cancel", "edit", "status", "help", "unknown"\n'
    '  "params": an object with relevant parameters\n\n'
    "Parameter conventions:\n"
    '- "list" intent: {"filter": "unread"|"all"|"urgent", "account": optional email}\n'
    '- "summarize" intent: {"ref": email reference like "#3" or description}\n'
    '- "reply" intent: {"ref": email reference, "message": what to say}\n'
    '- "archive" intent: {"refs": list of references like ["#2", "#3"] or "all low priority"}\n'
    '- "send" intent: {} (confirms pending draft)\n'
    '- "cancel" intent: {} (cancels pending draft)\n'
    '- "edit" intent: {"instruction": edit instruction like "change Thursday to Friday"}\n'
    '- "status" intent: {}\n'
    '- "help" intent: {}\n'
    '- "unknown" intent: {"raw": original text}\n\n'
    "Respond with ONLY the JSON object, no markdown fences."
)

TRIAGE_SYSTEM = (
    "You are an email triage assistant. For each email, determine:\n"
    "1. attention_score: 0.0-1.0 (how much the owner should care)\n"
    "2. decision: 'needs_attention' | 'auto_archived' | 'noise'\n"
    "3. summary: one-line plain-text summary (max 100 chars)\n"
    "4. needs_reply: true/false\n"
    "5. suggested_action: brief suggestion or null\n\n"
    "Return a JSON array (one object per email, in order). "
    "Respond with ONLY the JSON array, no markdown fences."
)

SUMMARIZE_SYSTEM = (
    "Summarize this email in 2-3 sentences. Include key action items "
    "or deadlines if any. Be concise and direct."
)

DRAFT_GENERATE_SYSTEM = (
    "You are drafting an email reply on behalf of the user. "
    "Write a professional, concise reply based on their instruction. "
    "Return ONLY a JSON object with these keys:\n"
    '  "subject": the reply subject (usually "Re: <original>")\n'
    '  "body": the plain-text reply body\n'
    "Do NOT include email headers or signatures. "
    "Respond with ONLY the JSON object, no markdown fences."
)

DRAFT_EDIT_SYSTEM = (
    "You are editing an email draft. Apply the user's edit instruction "
    "to the draft and return ONLY the updated email body text. "
    "No JSON, no explanation — just the revised email text."
)


# ── User prompt builders ──


def build_command_user_prompt(user_text: str, context: str) -> str:
    if context:
        return f"Context:\n{context}\n\nUser message:\n{user_text}"
    return user_text


def build_triage_user_prompt(emails: List[Email]) -> str:
    lines = []
    for i, e in enumerate(emails):
        preview = (e.body_text or "")[:300].replace("\n", " ")
        lines.append(
            f"[{i}] id={e.id} from={e.sender.email} "
            f'subject="{e.subject}" preview="{preview}"'
        )
    return "Triage these emails:\n\n" + "\n".join(lines)


def build_summarize_user_prompt(email: Email) -> str:
    body = (email.body_text or email.body_html or "")[:2000]
    return (
        f"From: {email.sender}\n"
        f"Subject: {email.subject}\n"
        f"Date: {email.date}\n\n"
        f"{body}"
    )


def build_draft_user_prompt(
    original_email: Email, user_instruction: str, account_email: str,
) -> str:
    body = (original_email.body_text or original_email.body_html or "")[:2000]
    return (
        f"Original email:\n"
        f"From: {original_email.sender}\n"
        f"To: {account_email}\n"
        f"Subject: {original_email.subject}\n"
        f"Date: {original_email.date}\n\n"
        f"{body}\n\n"
        f"---\n"
        f"User instruction: {user_instruction}"
    )


def build_edit_user_prompt(current_body: str, edit_instruction: str) -> str:
    return f"Current draft:\n{current_body}\n\nEdit instruction: {edit_instruction}"


# ── Helpers ──


def strip_fences(text: str) -> str:
    """Remove markdown code fences if present."""
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return text
