"""Core data models for Email Agent."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class EmailCategory(str, Enum):
    """Email categories based on Gmail's tab model."""

    PRIMARY = "primary"
    SOCIAL = "social"
    PROMOTIONS = "promotions"
    UPDATES = "updates"
    FORUMS = "forums"
    SPAM = "spam"


class EmailPriority(str, Enum):
    """Email priority levels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TriageDecision(str, Enum):
    """Triage outcome for an email."""

    NEEDS_ATTENTION = "needs_attention"
    AUTO_ARCHIVED = "auto_archived"
    NOISE = "noise"


class EmailAddress(BaseModel):
    """Email address with optional display name."""

    email: str
    name: Optional[str] = None

    def __str__(self) -> str:
        if self.name:
            return f"{self.name} <{self.email}>"
        return self.email


class EmailAttachment(BaseModel):
    """Email attachment metadata."""

    filename: str
    content_type: str
    size: int
    content_id: Optional[str] = None
    inline: bool = False


class Email(BaseModel):
    """Core email model."""

    id: str
    account_id: str
    message_id: str
    thread_id: Optional[str] = None

    # Headers
    subject: str
    sender: EmailAddress
    recipients: List[EmailAddress] = Field(default_factory=list)
    cc: List[EmailAddress] = Field(default_factory=list)

    # Content
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    attachments: List[EmailAttachment] = Field(default_factory=list)

    # Metadata
    date: datetime
    is_read: bool = False

    # Categorization
    category: EmailCategory = EmailCategory.PRIMARY
    priority: EmailPriority = EmailPriority.NORMAL

    # Triage
    triage_score: Optional[float] = None
    triage_decision: Optional[TriageDecision] = None
    needs_reply: Optional[bool] = None
    summary: Optional[str] = None

    # Processing
    processed_at: Optional[datetime] = None
    notified_at: Optional[datetime] = None

    # Raw data
    raw_headers: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("date", mode="before")
    @classmethod
    def parse_datetime(cls, v):
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class RuleCondition(BaseModel):
    """Condition for email categorization rules."""

    field: str  # subject, sender, body, etc.
    operator: str  # contains, equals, regex, etc.
    value: str
    case_sensitive: bool = False


class EmailRule(BaseModel):
    """Email categorization rule."""

    id: str
    name: str
    description: Optional[str] = None
    conditions: List[RuleCondition]
    actions: Dict[str, Any]  # category, priority, tags, etc.
    enabled: bool = True
    priority: int = 0  # Lower numbers have higher priority


class ParsedCommand(BaseModel):
    """Result of parsing a natural language Slack command."""

    intent: str  # list, summarize, reply, archive, send, cancel, edit, status
    params: Dict[str, Any] = Field(default_factory=dict)
    raw_text: str


class DraftContent(BaseModel):
    """Generated email draft."""

    subject: str
    body_text: str
    to_addresses: List[EmailAddress]
    cc_addresses: List[EmailAddress] = Field(default_factory=list)


class TriageResult(BaseModel):
    """Result of AI triage for a single email."""

    email_id: str
    attention_score: float  # 0.0 - 1.0
    decision: TriageDecision
    summary: str
    needs_reply: bool
    suggested_action: Optional[str] = None
