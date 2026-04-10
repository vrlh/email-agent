"""Rules engine for email categorization.

Cherry-picked from the original codebase with BaseRule inlined and imports updated.
"""

import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from lib.models import Email, EmailCategory, EmailPriority, EmailRule, RuleCondition

logger = logging.getLogger(__name__)


# ── BaseRule (inlined from old sdk/base.py) ──


class BaseRule(ABC):
    """Base class for email categorization rules."""

    def __init__(self, rule_config: EmailRule) -> None:
        self.rule_config = rule_config

    @abstractmethod
    def applies(self, email: Email) -> bool:
        pass

    @abstractmethod
    def execute(self, email: Email) -> Email:
        pass

    @property
    def priority(self) -> int:
        return self.rule_config.priority

    @property
    def enabled(self) -> bool:
        return self.rule_config.enabled


# ── Rule Processors ──


def _get_field_value(email: Email, field: str) -> Optional[Any]:
    field_map = {
        "subject": email.subject,
        "sender": email.sender.email,
        "sender_name": email.sender.name or "",
        "sender_domain": (
            email.sender.email.split("@")[-1] if "@" in email.sender.email else ""
        ),
        "body": email.body_text or "",
        "body_html": email.body_html or "",
        "recipients": ", ".join([addr.email for addr in email.recipients]),
        "cc": ", ".join([addr.email for addr in email.cc]),
        "is_read": email.is_read,
        "category": email.category.value,
        "priority": email.priority.value,
    }
    return field_map.get(field)


def _evaluate_condition(condition: RuleCondition, email: Email) -> bool:
    field_value = _get_field_value(email, condition.field)
    if field_value is None:
        return False

    field_str = str(field_value)
    if not condition.case_sensitive:
        field_str = field_str.lower()
        condition_value = condition.value.lower()
    else:
        condition_value = condition.value

    if condition.operator == "equals":
        return field_str == condition_value
    elif condition.operator == "contains":
        return condition_value in field_str
    elif condition.operator == "starts_with":
        return field_str.startswith(condition_value)
    elif condition.operator == "ends_with":
        return field_str.endswith(condition_value)
    elif condition.operator == "regex":
        try:
            flags = re.IGNORECASE if not condition.case_sensitive else 0
            return bool(re.search(condition_value, field_str, flags))
        except re.error:
            return False
    elif condition.operator == "not_equals":
        return field_str != condition_value
    elif condition.operator == "not_contains":
        return condition_value not in field_str
    else:
        logger.warning(f"Unknown operator: {condition.operator}")
        return False


class GenericRule(BaseRule):
    """Generic rule processor — all conditions must match (AND)."""

    def applies(self, email: Email) -> bool:
        return all(
            _evaluate_condition(c, email) for c in self.rule_config.conditions
        )

    def execute(self, email: Email) -> Email:
        actions = self.rule_config.actions

        if "category" in actions:
            try:
                email.category = EmailCategory(actions["category"])
            except ValueError:
                pass

        if "priority" in actions:
            try:
                email.priority = EmailPriority(actions["priority"])
            except ValueError:
                pass

        return email


class RegexRule(GenericRule):
    """Pre-compiles regex patterns for performance."""

    def __init__(self, rule_config: EmailRule):
        super().__init__(rule_config)
        self._compiled: Dict[int, re.Pattern] = {}
        for i, cond in enumerate(rule_config.conditions):
            if cond.operator == "regex":
                try:
                    flags = re.IGNORECASE if not cond.case_sensitive else 0
                    self._compiled[i] = re.compile(cond.value, flags)
                except re.error:
                    pass

    def applies(self, email: Email) -> bool:
        for i, cond in enumerate(self.rule_config.conditions):
            if cond.operator == "regex" and i in self._compiled:
                val = _get_field_value(email, cond.field)
                if val is None or not self._compiled[i].search(str(val)):
                    return False
            elif not _evaluate_condition(cond, email):
                return False
        return True


def _create_processor(rule_config: EmailRule) -> BaseRule:
    has_regex = any(c.operator == "regex" for c in rule_config.conditions)
    if has_regex:
        return RegexRule(rule_config)
    return GenericRule(rule_config)


# ── Engine ──


class RulesEngine:
    """Process emails through a prioritized chain of rules."""

    def __init__(self) -> None:
        self.rules: List[BaseRule] = []

    def load_rules(self, rules: List[EmailRule]) -> None:
        self.rules.clear()
        for rule in rules:
            if rule.enabled:
                try:
                    self.rules.append(_create_processor(rule))
                except Exception as e:
                    logger.error(f"Failed to load rule {rule.id}: {e}")
        self.rules.sort(key=lambda r: r.priority)

    def process_email(self, email: Email) -> Email:
        processed = email.model_copy(deep=True)
        for rule in self.rules:
            try:
                if rule.applies(processed):
                    processed = rule.execute(processed)
            except Exception as e:
                logger.error(f"Rule error {rule.rule_config.name}: {e}")
        return processed

    def process_emails(self, emails: List[Email]) -> List[Email]:
        return [self.process_email(e) for e in emails]
