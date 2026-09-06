"""Pin the safe default: ``delegation.subagent_auto_approve`` unset means DENY."""

from unittest.mock import patch

from tools.delegate_tool import (
    _get_subagent_approval_callback,
    _subagent_auto_deny,
)


def test_default_subagent_auto_approve_is_deny():
    """No config key → deny callback (fail closed for subagent tool use)."""
    with patch("tools.delegate_tool._load_config", return_value={}):
        assert _get_subagent_approval_callback() is _subagent_auto_deny


def test_deny_callback_returns_deny():
    assert _subagent_auto_deny("rm -rf /tmp/x", "dangerous") == "deny"
