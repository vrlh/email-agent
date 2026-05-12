"""Tests for ``lib.agent.TOOLS`` reauth schema.

Covers behavior 12: the reauth tool requires the ``email`` field so Claude can't
generate a generic link.
"""

from lib import agent


def test_reauth_tool_requires_email_field():
    # Behavior 12: input_schema.required must include "email".
    reauth = next((t for t in agent.TOOLS if t["name"] == "reauth"), None)
    assert reauth is not None, "reauth tool not registered in agent.TOOLS"
    required = reauth["input_schema"].get("required", [])
    assert "email" in required, (
        "reauth.input_schema.required must include 'email' so Claude is forced "
        "to identify the target account in every call"
    )
