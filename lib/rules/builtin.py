"""Built-in rules for noise filtering — newsletters, promotions, social, spam, etc."""

from typing import List

from lib.models import EmailCategory, EmailPriority, EmailRule, RuleCondition


class BuiltinRules:
    """Factory for built-in email categorization rules."""

    @staticmethod
    def get_all_rules() -> List[EmailRule]:
        return [
            BuiltinRules.social_media_rule(),
            BuiltinRules.newsletters_rule(),
            BuiltinRules.notifications_rule(),
            BuiltinRules.promotions_rule(),
            BuiltinRules.forums_rule(),
            BuiltinRules.automated_emails_rule(),
            BuiltinRules.urgent_emails_rule(),
            BuiltinRules.spam_indicators_rule(),
        ]

    @staticmethod
    def social_media_rule() -> EmailRule:
        return EmailRule(
            id="builtin_social_media",
            name="Social Media",
            conditions=[
                RuleCondition(
                    field="sender_domain",
                    operator="regex",
                    value=r"(facebook|twitter|linkedin|instagram|tiktok|snapchat|discord|slack|teams)\.com$",
                ),
            ],
            actions={"category": EmailCategory.SOCIAL.value},
            priority=10,
        )

    @staticmethod
    def newsletters_rule() -> EmailRule:
        return EmailRule(
            id="builtin_newsletters",
            name="Newsletters & Updates",
            conditions=[
                RuleCondition(
                    field="subject",
                    operator="regex",
                    value=r"(newsletter|digest|weekly|monthly|update|bulletin)",
                ),
            ],
            actions={"category": EmailCategory.UPDATES.value},
            priority=20,
        )

    @staticmethod
    def notifications_rule() -> EmailRule:
        return EmailRule(
            id="builtin_notifications",
            name="Notifications",
            conditions=[
                RuleCondition(
                    field="subject",
                    operator="regex",
                    value=r"(notification|alert|reminder|noreply|no-reply)",
                ),
            ],
            actions={"category": EmailCategory.UPDATES.value},
            priority=30,
        )

    @staticmethod
    def promotions_rule() -> EmailRule:
        return EmailRule(
            id="builtin_promotions",
            name="Promotions & Marketing",
            conditions=[
                RuleCondition(
                    field="subject",
                    operator="regex",
                    value=r"(sale|discount|offer|promo|deal|coupon|% off|free shipping|limited time)",
                ),
            ],
            actions={"category": EmailCategory.PROMOTIONS.value},
            priority=15,
        )

    @staticmethod
    def forums_rule() -> EmailRule:
        return EmailRule(
            id="builtin_forums",
            name="Forums & Communities",
            conditions=[
                RuleCondition(
                    field="subject",
                    operator="regex",
                    value=r"(\[.*\]|forum|community|discussion|replied to|mentioned you)",
                ),
            ],
            actions={"category": EmailCategory.FORUMS.value},
            priority=25,
        )

    @staticmethod
    def automated_emails_rule() -> EmailRule:
        return EmailRule(
            id="builtin_automated",
            name="Automated Emails",
            conditions=[
                RuleCondition(
                    field="sender",
                    operator="regex",
                    value=r"(noreply|no-reply|donotreply|automated|system|daemon)@",
                ),
            ],
            actions={
                "category": EmailCategory.UPDATES.value,
                "priority": EmailPriority.LOW.value,
            },
            priority=40,
        )

    @staticmethod
    def urgent_emails_rule() -> EmailRule:
        return EmailRule(
            id="builtin_urgent",
            name="Urgent Emails",
            conditions=[
                RuleCondition(
                    field="subject",
                    operator="regex",
                    value=r"(urgent|asap|emergency|critical|immediate|deadline|expires)",
                ),
            ],
            actions={"priority": EmailPriority.URGENT.value},
            priority=5,
        )

    @staticmethod
    def spam_indicators_rule() -> EmailRule:
        return EmailRule(
            id="builtin_spam_indicators",
            name="Spam Indicators",
            conditions=[
                RuleCondition(
                    field="subject",
                    operator="regex",
                    value=r"(RE: RE: RE:|FW: FW: FW:|WINNER|CONGRATULATIONS|CLAIM YOUR|ACT NOW|CASH PRIZE)",
                ),
            ],
            actions={"priority": EmailPriority.LOW.value},
            priority=50,
        )
