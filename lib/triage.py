"""Hybrid triage — rule-based scoring with optional Claude AI enhancement.

Rule-based scoring is fast and free.  Claude is called only for PRIMARY-
category emails that survive the rules pass, keeping API costs low.
"""

from datetime import datetime, timezone
from typing import List, Optional

from lib.models import Email, EmailCategory, TriageDecision, TriageResult


# ── Category baseline scores ──

_CATEGORY_SCORES = {
    EmailCategory.PRIMARY: 0.8,
    EmailCategory.SOCIAL: 0.2,
    EmailCategory.PROMOTIONS: 0.1,
    EmailCategory.UPDATES: 0.3,
    EmailCategory.FORUMS: 0.4,
    EmailCategory.SPAM: 0.0,
}

# ── Urgency keywords (subject → score) ──

_URGENCY_KEYWORDS = {
    "urgent": 0.9,
    "asap": 0.9,
    "immediate": 0.8,
    "deadline": 0.8,
    "important": 0.7,
    "priority": 0.7,
    "time sensitive": 0.8,
    "action required": 0.8,
    "please respond": 0.6,
    "follow up": 0.5,
    "reminder": 0.5,
}

# ── Scoring weights ──

_WEIGHTS = {
    "category": 0.30,
    "urgency": 0.25,
    "recency": 0.25,
    "sender": 0.20,
}

# ── Thresholds ──

PRIORITY_THRESHOLD = 0.7
ARCHIVE_THRESHOLD = 0.4

_AUTO_ARCHIVE_CATEGORIES = {
    EmailCategory.PROMOTIONS,
    EmailCategory.SOCIAL,
    EmailCategory.UPDATES,
}


# ---------------------------------------------------------------------------
# Individual scoring factors
# ---------------------------------------------------------------------------


def _score_category(email: Email) -> float:
    return _CATEGORY_SCORES.get(email.category, 0.5)


def _score_urgency(email: Email) -> float:
    """Scan subject (full weight) and body preview (80% weight) for urgency keywords."""
    best = 0.0
    subject_lower = email.subject.lower()
    for keyword, score in _URGENCY_KEYWORDS.items():
        if keyword in subject_lower:
            best = max(best, score)

    if email.body_text:
        preview = email.body_text[:500].lower()
        for keyword, score in _URGENCY_KEYWORDS.items():
            if keyword in preview:
                best = max(best, score * 0.8)

    return best


def _score_recency(email: Email) -> float:
    age_hours = (datetime.now(timezone.utc) - email.date.replace(tzinfo=timezone.utc if email.date.tzinfo is None else email.date.tzinfo)).total_seconds() / 3600
    if age_hours < 1:
        return 1.0
    elif age_hours < 6:
        return 0.8
    elif age_hours < 24:
        return 0.6
    elif age_hours < 72:
        return 0.4
    elif age_hours < 168:
        return 0.2
    return 0.1


def _score_sender(email: Email) -> float:
    """Simple heuristic sender scoring (no history tracking in v1)."""
    addr = email.sender.email.lower()
    if "noreply@" in addr or "no-reply@" in addr or "donotreply@" in addr:
        return 0.1
    if "notification@" in addr or "alerts@" in addr:
        return 0.2
    # Real person emails get a decent baseline
    return 0.5


# ---------------------------------------------------------------------------
# User rules
# ---------------------------------------------------------------------------


def _apply_user_rules(email: Email) -> Optional[TriageDecision]:
    """Check user rules. Returns a forced decision, or None to continue normal scoring."""
    import re
    from lib.db import get_user_rules

    rules = get_user_rules(enabled_only=True)
    sender = email.sender.email.lower()
    sender_domain = sender.split("@")[-1] if "@" in sender else ""
    subject = email.subject.lower()

    for rule in rules:
        val = rule.value.lower()
        # Get the field to match against
        if rule.field == "sender":
            target = sender
        elif rule.field == "sender_domain":
            target = sender_domain
        elif rule.field == "subject":
            target = subject
        else:
            continue

        # Check if it matches
        matched = False
        if rule.operator == "contains":
            matched = val in target
        elif rule.operator == "equals":
            matched = target == val
        elif rule.operator == "regex":
            try:
                matched = bool(re.search(val, target, re.IGNORECASE))
            except re.error:
                pass

        if matched:
            if rule.action == "auto_archive":
                return TriageDecision.AUTO_ARCHIVED
            elif rule.action == "boost":
                return TriageDecision.NEEDS_ATTENTION

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_email(email: Email) -> float:
    """Compute a 0–1 attention score using rule-based factors."""
    raw = (
        _WEIGHTS["category"] * _score_category(email)
        + _WEIGHTS["urgency"] * _score_urgency(email)
        + _WEIGHTS["recency"] * _score_recency(email)
        + _WEIGHTS["sender"] * _score_sender(email)
    )
    return min(1.0, max(0.0, raw))


def decide(email: Email, score: Optional[float] = None) -> TriageDecision:
    """Map an attention score to a triage decision."""
    # User rules take priority over everything
    user_override = _apply_user_rules(email)
    if user_override is not None:
        return user_override

    if score is None:
        score = score_email(email)

    if email.category == EmailCategory.SPAM:
        return TriageDecision.NOISE

    if score >= PRIORITY_THRESHOLD:
        return TriageDecision.NEEDS_ATTENTION

    if score <= ARCHIVE_THRESHOLD and email.category in _AUTO_ARCHIVE_CATEGORIES:
        return TriageDecision.AUTO_ARCHIVED

    # Borderline — still surface to user
    return TriageDecision.NEEDS_ATTENTION


def triage_emails_rule_based(emails: List[Email]) -> List[TriageResult]:
    """Score and decide for a batch of emails using rules only (no AI)."""
    results: List[TriageResult] = []
    for email in emails:
        score = score_email(email)
        decision = decide(email, score)
        results.append(
            TriageResult(
                email_id=email.id,
                attention_score=round(score, 3),
                decision=decision,
                summary=email.subject,  # placeholder until Claude enriches
                needs_reply=(decision == TriageDecision.NEEDS_ATTENTION),
            )
        )
    return results
