"""Agentic email assistant — Claude tool-calling agent.

The user types anything natural, Claude decides which tools to call,
we execute them, and Claude generates a conversational response.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

AGENT_SYSTEM = (
    "You are an AI email assistant that manages the user's Gmail inboxes via Slack. "
    "You have access to tools to list, summarize, reply to, and dismiss emails, "
    "as well as manage notification rules.\n\n"
    "Guidelines:\n"
    "- Be concise and helpful. Don't over-explain.\n"
    "- When the user references an email by number (#N), use the number from the "
    "last-displayed list in the context.\n"
    "- When the user describes an email vaguely ('that zip email', 'the interview one'), "
    "look through the context to find the best match and use its #N reference.\n"
    "- You can call multiple tools in one turn if needed.\n"
    "- For reply drafts, always use the reply_to_email tool — it will show the user "
    "a draft with Send/Cancel buttons. Don't just write the reply text.\n"
    "- IMPORTANT: Tools that display data (list_emails, get_needs_reply, summarize_email, "
    "list_rules, get_status) already send formatted messages to the user. "
    "After calling these tools, DO NOT repeat or reformat the data. "
    "Just say something brief like 'Here you go' or ask what they'd like to do next. "
    "NEVER re-list emails or data that a tool already displayed.\n"
    "- If the user asks something unrelated to email, politely redirect them."
)

TOOLS = [
    {
        "name": "list_emails",
        "description": "List emails. Default shows only emails needing attention. Use filter='all' to show everything, 'unread' for unread only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["attention", "all", "unread"],
                    "description": "Which emails to show. Default: attention",
                },
            },
        },
    },
    {
        "name": "get_needs_reply",
        "description": "Get emails that the user still needs to reply to.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "summarize_email",
        "description": "Get a detailed AI summary of an email. Use the #N reference from the last displayed list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Email reference like '#3' from the last displayed list",
                },
            },
            "required": ["ref"],
        },
    },
    {
        "name": "reply_to_email",
        "description": "Draft a reply to an email. The draft will be shown to the user with Send/Cancel buttons. Describe what the reply should say.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Email reference like '#1'",
                },
                "message": {
                    "type": "string",
                    "description": "What the reply should say (natural language instruction)",
                },
            },
            "required": ["ref", "message"],
        },
    },
    {
        "name": "dismiss_emails",
        "description": "Mark emails as read in Gmail and clear needs_reply. Does NOT remove from inbox. Use for emails the user doesn't need to act on.",
        "input_schema": {
            "type": "object",
            "properties": {
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of email references like ['#2', '#5']",
                },
            },
            "required": ["refs"],
        },
    },
    {
        "name": "send_draft",
        "description": "Send the currently pending draft reply.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_draft",
        "description": "Cancel the currently pending draft reply.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "edit_draft",
        "description": "Edit the currently pending draft reply.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "How to change the draft, e.g. 'make it more formal' or 'change Tuesday to Wednesday'",
                },
            },
            "required": ["instruction"],
        },
    },
    {
        "name": "create_rule",
        "description": "Create a rule to automatically handle future emails. 'ignore' rules auto-archive matching emails. 'priority' rules always notify.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_type": {
                    "type": "string",
                    "enum": ["ignore", "priority"],
                    "description": "ignore = auto-archive, priority = always notify",
                },
                "field": {
                    "type": "string",
                    "enum": ["sender", "sender_domain", "subject"],
                    "description": "Which email field to match",
                },
                "operator": {
                    "type": "string",
                    "enum": ["contains", "equals"],
                    "description": "How to match",
                },
                "value": {
                    "type": "string",
                    "description": "The pattern to match against",
                },
            },
            "required": ["rule_type", "field", "operator", "value"],
        },
    },
    {
        "name": "delete_rule",
        "description": "Delete a notification rule by its number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Rule reference like '#2'",
                },
            },
            "required": ["ref"],
        },
    },
    {
        "name": "list_rules",
        "description": "Show all active notification rules.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_status",
        "description": "Show connected accounts, last sync times, and pending draft info.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_reply_status",
        "description": "Check if a specific email has been replied to in Gmail by looking at the full thread. Use this when the user says they already replied to something.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Email reference like '#1'",
                },
            },
            "required": ["ref"],
        },
    },
    {
        "name": "onboard",
        "description": "Scan and triage emails from the last 3 months. Use force=true to re-triage all emails.",
        "input_schema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "If true, re-triages all emails. Default false.",
                },
            },
        },
    },
    {
        "name": "reauth",
        "description": "Generate a re-authentication link when Gmail tokens have expired or access has been revoked. User clicks the link, signs in with Google, and tokens are refreshed for the existing account. Use this when the user says tokens expired, sign-in broke, they got disconnected, or they need to reconnect Gmail.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Optional — the email address to reconnect, shown in the message. Google picks the account based on which one the user signs in with.",
                },
            },
        },
    },
]


def run_agent(user_text: str, context: str, tool_executor) -> str:
    """Run the tool-calling agent. Returns the final response text.

    *tool_executor* is a callable(tool_name, tool_input) -> str that
    executes a tool and returns the result as a string.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    messages = [{"role": "user", "content": f"Context:\n{context}\n\nUser: {user_text}"}]

    # Loop: Claude may call tools, we execute them, then Claude responds
    for _ in range(5):  # max 5 tool-call rounds
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=AGENT_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        # Collect text and tool-use blocks
        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            # No more tool calls — return the final text
            return "\n".join(text_parts).strip()

        # Execute tool calls and build tool_result messages
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for tc in tool_calls:
            try:
                result = tool_executor(tc.name, tc.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
                })
            except Exception as exc:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": f"Error: {exc}",
                    "is_error": True,
                })
        messages.append({"role": "user", "content": tool_results})

    return "I'm having trouble processing that. Try a simpler request."
