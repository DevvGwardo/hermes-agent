"""Tests for the Cursor Composer 2.5 provider + MoA dispatch wiring.

Mirrors the style of tests/hermes_cli/test_model_switch_custom_providers.py:
- Function-based pytest
- monkeypatch for env and module-level state
- Descriptive docstrings
- Async tests wrapped in `asyncio.run(...)` to avoid needing @pytest.mark.asyncio
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Provider detection + config
# ---------------------------------------------------------------------------

def test_looks_like_cursor_model_recognizes_known_forms():
    from agent.transports.cursor_composer import looks_like_cursor_model
    assert looks_like_cursor_model("composer-2.5")
    assert looks_like_cursor_model("composer2.5")
    assert looks_like_cursor_model("composer_2.5")
    assert looks_like_cursor_model("cursor:composer-2.5")
    assert looks_like_cursor_model("Cursor:Composer-2.5")  # case-insensitive
    assert looks_like_cursor_model("cursor/composer-2.5")
    assert looks_like_cursor_model("  cursor:composer-2.5  ")  # whitespace


def test_looks_like_cursor_model_rejects_non_cursor_models():
    from agent.transports.cursor_composer import looks_like_cursor_model
    assert not looks_like_cursor_model(None)
    assert not looks_like_cursor_model("")
    assert not looks_like_cursor_model("anthropic/claude-opus-4.6")
    assert not looks_like_cursor_model("openai/gpt-5")
    assert not looks_like_cursor_model("not-cursor")


def test_normalize_cursor_model_strips_prefix():
    from agent.transports.cursor_composer import normalize_cursor_model
    assert normalize_cursor_model("cursor:composer-2.5") == "composer-2.5"
    assert normalize_cursor_model("cursor/composer-2.5") == "composer-2.5"
    assert normalize_cursor_model("composer-2.5") == "composer-2.5"


def test_find_cursor_agent_binary_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_AGENT_BIN", str(tmp_path))
    # The path doesn't need to exist for the override check; tmp_path always does.
    from agent.transports.cursor_composer import find_cursor_agent_binary
    # CURSOR_AGENT_BIN override should win even when the file isn't yet
    # executable -- let's also assert the existence-checking contract.
    monkeypatch.delenv("CURSOR_AGENT_BIN", raising=False)
    fake_bin = tmp_path / "cursor-agent"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("CURSOR_AGENT_BIN", str(fake_bin))
    assert find_cursor_agent_binary() == str(fake_bin)


def test_from_config_merges_caller_dict_with_env(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "test-key-xyz")
    monkeypatch.delenv("HERMES_CURSOR_API_KEY", raising=False)
    from agent.transports.cursor_composer import (
        CursorComposerProvider,
        CursorComposerConfig,
    )
    p = CursorComposerProvider.from_config({
        "model": "composer-2.5",
        "workspace": "/tmp/x",
        "extra_args": ["--yolo", "--max-steps=50"],
        "timeout_seconds": 30,
    })
    assert isinstance(p.config, CursorComposerConfig)
    assert p.config.api_key == "test-key-xyz"
    assert p.config.workspace == "/tmp/x"
    assert p.config.model == "composer-2.5"
    assert p.config.timeout_seconds == 30
    assert "--yolo" in p.config.extra_args


def test_from_config_uses_hermes_env_key_over_cursor_env_key(monkeypatch):
    """HERMES_CURSOR_API_KEY takes precedence over CURSOR_API_KEY when both set."""
    monkeypatch.setenv("CURSOR_API_KEY", "fallback-key")
    monkeypatch.setenv("HERMES_CURSOR_API_KEY", "explicit-key")
    from agent.transports.cursor_composer import CursorComposerProvider
    p = CursorComposerProvider.from_config({})
    assert p.config.api_key == "explicit-key"


# ---------------------------------------------------------------------------
# Subprocess behavior (mocked)
# ---------------------------------------------------------------------------

def test_complete_flattens_and_returns_normalized_response(monkeypatch):
    """complete() flattens OpenAI-format messages, invokes cursor-agent, returns NormalizedResponse."""
    from agent.transports.cursor_composer import CursorComposerProvider
    from agent.transports import cursor_composer as cm

    captured = {}

    async def fake_invoke(prompt, config, binary=None, extra_args=None):
        captured["prompt"] = prompt
        captured["config"] = config
        captured["extra_args"] = list(extra_args or ())
        return "FAKE_OUTPUT_TEXT"

    monkeypatch.setattr(cm, "invoke_cursor_agent", fake_invoke)

    async def _run():
        provider = CursorComposerProvider.from_config({"model": "composer-2.5"})
        return await provider.complete([{"role": "user", "content": "hello"}])

    result = asyncio.run(_run())

    assert "[USER]\nhello" in captured["prompt"]
    assert captured["config"].model == "composer-2.5"
    assert captured["config"].api_key is None
    assert captured["extra_args"] == []
    assert result.content == "FAKE_OUTPUT_TEXT"
    assert result.finish_reason == "stop"
    assert result.tool_calls == []
    assert result.provider_data["model"] == "composer-2.5"
    assert result.provider_data["api_mode"] == "cursor_composer"


def test_complete_text_with_plain_string_skips_flattening(monkeypatch):
    """complete_text with a plain string prompt bypasses flattening and returns stdout."""
    from agent.transports import cursor_composer as cm

    captured = {}

    async def fake_invoke(prompt, config, binary=None, extra_args=None):
        captured["prompt"] = prompt
        return "RAW"

    monkeypatch.setattr(cm, "invoke_cursor_agent", fake_invoke)

    async def _run():
        provider = cm.CursorComposerProvider.from_config({})
        return await provider.complete_text("just a prompt")

    text = asyncio.run(_run())
    assert text == "RAW"
    assert captured["prompt"] == "just a prompt"


def test_invoke_cursor_agent_raises_when_binary_missing(monkeypatch):
    """Clear all binary discovery paths; should raise RuntimeError, not silently return."""
    monkeypatch.delenv("CURSOR_AGENT_BIN", raising=False)
    monkeypatch.setattr(
        "agent.transports.cursor_composer._FALLBACK_BINARIES",
        ("/nonexistent/a", "/nonexistent/b"),
    )
    monkeypatch.setattr(shutil, "which", lambda name: None)

    from agent.transports.cursor_composer import (
        CursorComposerConfig,
        invoke_cursor_agent,
    )
    with pytest.raises(RuntimeError, match="cursor-agent binary not found"):
        asyncio.run(invoke_cursor_agent("hi", CursorComposerConfig(), binary=None))


# ---------------------------------------------------------------------------
# Provider catalog surface
# ---------------------------------------------------------------------------

def test_cursor_is_in_canonical_providers_catalog():
    """`cursor` must appear in hermes_cli CANONICAL_PROVIDERS so users can pick it."""
    from hermes_cli.models import CANONICAL_PROVIDERS
    slugs = {entry.slug for entry in CANONICAL_PROVIDERS}
    assert "cursor" in slugs
    cursor_entry = next(e for e in CANONICAL_PROVIDERS if e.slug == "cursor")
    assert cursor_entry.label == "Cursor Composer"
    assert "Composer 2.5" in cursor_entry.tui_desc


# ---------------------------------------------------------------------------
# MoA env-driven configuration
# ---------------------------------------------------------------------------

def test_moa_env_references_accepts_json_list(monkeypatch):
    monkeypatch.setenv("HERMES_MOA_REFERENCES", json.dumps(
        ["cursor:composer-2.5", "anthropic/claude-opus-4.6"]
    ))
    from tools.mixture_of_agents_tool import _moa_env_references
    refs = _moa_env_references()
    assert refs == ["cursor:composer-2.5", "anthropic/claude-opus-4.6"]


def test_moa_env_references_accepts_csv(monkeypatch):
    monkeypatch.setenv("HERMES_MOA_REFERENCES", "cursor:composer-2.5, openai/gpt-5")
    from tools.mixture_of_agents_tool import _moa_env_references
    refs = _moa_env_references()
    assert refs == ["cursor:composer-2.5", "openai/gpt-5"]


def test_moa_env_aggregator(monkeypatch):
    monkeypatch.setenv("HERMES_MOA_AGGREGATOR", "cursor:composer-2.5")
    from tools.mixture_of_agents_tool import _moa_env_aggregator
    assert _moa_env_aggregator() == "cursor:composer-2.5"


def test_moa_env_cursor_config_composes_envs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CURSOR_API_KEY", "k1")
    monkeypatch.setenv("HERMES_CURSOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HERMES_CURSOR_MODEL", "composer-2.5")
    monkeypatch.setenv("HERMES_CURSOR_EXTRA_ARGS", "--yolo --max-steps 50")
    from tools.mixture_of_agents_tool import _moa_env_cursor_config
    cfg = _moa_env_cursor_config()
    assert cfg["api_key"] == "k1"
    assert cfg["workspace"] == str(tmp_path)
    assert cfg["model"] == "composer-2.5"
    assert cfg["extra_args"] == ["--yolo", "--max-steps", "50"]


def test_moa_env_cursor_config_falls_back_to_cursor_api_key(monkeypatch):
    monkeypatch.delenv("HERMES_CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("CURSOR_API_KEY", "fallback-key")
    from tools.mixture_of_agents_tool import _moa_env_cursor_config
    cfg = _moa_env_cursor_config()
    assert cfg["api_key"] == "fallback-key"


# ---------------------------------------------------------------------------
# MoA dispatch (mocked): routes cursor: models to CursorComposerProvider
# ---------------------------------------------------------------------------

def test_moa_dispatch_reference_model_routes_cursor(monkeypatch):
    """Detect cursor: model and forward to CursorComposerProvider.complete_text."""
    from agent.transports import cursor_composer as cm
    from tools import mixture_of_agents_tool as moa

    captured = {}

    # Monkeypatched instance methods must accept `self`.
    async def fake_complete_text(self, arg):
        captured["arg"] = arg
        return "OK_FROM_TEXT"

    monkeypatch.setattr(cm.CursorComposerProvider, "complete_text", fake_complete_text)

    async def _run():
        return await moa._moa_dispatch_reference_model(
            "cursor:composer-2.5", "test prompt", 0.6,
        )

    text, ok = asyncio.run(_run())
    assert ok is True
    assert text == "OK_FROM_TEXT"
    assert captured["arg"] == [{"role": "user", "content": "test prompt"}]


def test_moa_dispatch_reference_model_falls_back_to_openrouter(monkeypatch):
    """Non-cursor models delegate to `_run_reference_model_safe`."""
    from tools import mixture_of_agents_tool as moa

    captured = {"called_with": None}

    async def wrapper(model, prompt, temperature):
        captured["called_with"] = (model, prompt, temperature)
        return ("OPENROUTER_OUTPUT", True)

    monkeypatch.setattr(moa, "_run_reference_model_safe", wrapper)

    async def _run():
        return await moa._moa_dispatch_reference_model(
            "anthropic/claude-opus-4.6", "hi", 0.5,
        )

    text, ok = asyncio.run(_run())
    assert ok is True
    assert text == "OPENROUTER_OUTPUT"
    assert captured["called_with"] == ("anthropic/claude-opus-4.6", "hi", 0.5)


def test_moa_dispatch_aggregator_returns_string_for_cursor(monkeypatch):
    """The cursor aggregator branch returns a plain string (matches _run_aggregator_model -> str)."""
    from agent.transports import cursor_composer as cm
    from tools import mixture_of_agents_tool as moa

    captured = {}

    async def fake_complete_text(self, arg):
        captured["messages"] = arg
        return "AGG_OUTPUT_STRING"

    monkeypatch.setattr(cm.CursorComposerProvider, "complete_text", fake_complete_text)

    async def _run():
        return await moa._moa_dispatch_aggregator(
            "system prompt", "user prompt", 0.4,
            max_tokens=1024, model="cursor:composer-2.5",
        )

    result = asyncio.run(_run())
    assert isinstance(result, str), f"expected str, got {type(result).__name__}"
    assert result == "AGG_OUTPUT_STRING"
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1]["role"] == "user"


def test_moa_dispatch_aggregator_falls_back_for_non_cursor(monkeypatch):
    """Non-cursor aggregator calls `_run_aggregator_model` and returns its str value."""
    from tools import mixture_of_agents_tool as moa

    captured = {"called_with": None}

    async def fake_aggregator(system_prompt, user_prompt, temperature, max_tokens=None):
        captured["called_with"] = (system_prompt, user_prompt, temperature, max_tokens)
        return "AGG_FROM_OPENROUTER"

    monkeypatch.setattr(moa, "_run_aggregator_model", fake_aggregator)

    async def _run():
        return await moa._moa_dispatch_aggregator(
            "system prompt", "user prompt", 0.4,
            max_tokens=512, model="anthropic/claude-opus-4.6",
        )

    result = asyncio.run(_run())
    assert isinstance(result, str)
    assert result == "AGG_FROM_OPENROUTER"
    assert captured["called_with"] == ("system prompt", "user prompt", 0.4, 512)
