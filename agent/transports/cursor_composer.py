"""Cursor Composer 2.5 provider for hermes-agent.

Cursor exposes Composer 2.5 ONLY through the ``cursor-agent`` CLI binary
(no public HTTP API). This module wraps that binary via ``asyncio``
subprocess so it can be used in Hermes's Mixture-of-Agents tool -- both as
a reference expert AND as the aggregator -- and surfaced in the
hermes-cli provider catalog.

Composer itself is a provider-side aggregator: under the hood it fans out
across multiple underlying models (Claude, GPT, Gemini, etc.) and
synthesizes a single response, which makes it a natural MOE aggregator
when used in MoA.

This module is intentionally NOT a ``ProviderTransport`` subclass because
``ProviderTransport`` assumes an HTTP-style SDK that takes a shaped
message array and emits a shaped response. ``cursor-agent`` takes a flat
text prompt and emits stdout, so we use a separate abstraction.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from agent.transports.types import NormalizedResponse, Usage

DEFAULT_MODEL = "composer-2.5"
"""Default Composer model. Override per-call via the ``model`` config field."""

DEFAULT_TIMEOUT_SECONDS = 600
"""Generous default -- Composer can take a while for complex prompts."""

_FALLBACK_BINARIES: Sequence[str] = (
    "/Users/devgwardo/.local/bin/cursor-agent",
    "/opt/homebrew/bin/cursor-agent",
    "/usr/local/bin/cursor-agent",
)


@dataclass
class CursorComposerConfig:
    """Configuration for the Cursor Composer provider.

    Reads from hermes-cli/runtime config under the ``moa.cursor`` block.
    """

    model: str = DEFAULT_MODEL
    workspace: Optional[str] = None
    api_key: Optional[str] = None  # Falls back to ``CURSOR_API_KEY`` env
    extra_args: List[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


def find_cursor_agent_binary() -> Optional[str]:
    """Locate the ``cursor-agent`` CLI binary.

    Order of precedence:
      1. ``CURSOR_AGENT_BIN`` env var
      2. Hardcoded fallback paths (macOS Homebrew + dotfiles)
      3. ``shutil.which("cursor-agent")``
    """
    env_path = os.environ.get("CURSOR_AGENT_BIN")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        return env_path
    for cand in _FALLBACK_BINARIES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("cursor-agent")


def looks_like_cursor_model(model_id: Optional[str]) -> bool:
    """Return True if ``model_id`` should be routed to the cursor provider."""
    if not model_id:
        return False
    lower = model_id.strip().lower()
    if lower.startswith("cursor:") or lower.startswith("cursor/"):
        return True
    return lower in {"composer-2.5", "composer2.5", "composer_2.5"}


def normalize_cursor_model(model_id: str) -> str:
    """Strip ``cursor:`` / ``cursor/`` prefix and return the cursor-side id."""
    lower = model_id.strip().lower()
    for prefix in ("cursor:", "cursor/"):
        if lower.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def _flatten_messages(messages: Iterable[dict]) -> str:
    """Convert OpenAI-format messages into a single text prompt.

    ``cursor-agent -p`` has no structured message array, so we collapse
    the conversation into one text block with role labels.
    """
    parts: List[str] = []
    for m in messages or ():
        role = (m.get("role") or "user").lower()
        content = m.get("content") or ""
        if role == "system":
            parts.append(f"[SYSTEM]\n{content}\n")
        elif role == "assistant":
            parts.append(f"[ASSISTANT]\n{content}\n")
        elif role == "tool":
            parts.append(f"[TOOL RESULT]\n{content}\n")
        else:
            parts.append(f"[USER]\n{content}\n")
    parts.append("[ASSISTANT]\n")
    return "\n".join(parts)


async def invoke_cursor_agent(
    prompt: str,
    config: CursorComposerConfig,
    binary: Optional[str] = None,
    extra_args: Optional[Iterable[str]] = None,
) -> str:
    """Run ``cursor-agent`` non-interactively and return its stdout."""
    bin_path = binary or find_cursor_agent_binary()
    if not bin_path:
        raise RuntimeError(
            "cursor-agent binary not found. Install Cursor (https://cursor.com) "
            "or set CURSOR_AGENT_BIN to the absolute path of cursor-agent."
        )

    args: List[str] = [bin_path, "--print", "--model", config.model]
    if config.workspace:
        args += ["--workspace", config.workspace]
    args += list(extra_args or config.extra_args or ())

    env = os.environ.copy()
    if config.api_key:
        env["CURSOR_API_KEY"] = config.api_key

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=(prompt + "\n").encode("utf-8")),
            timeout=config.timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"cursor-agent timed out after {config.timeout_seconds}s "
            f"(model={config.model}, workspace={config.workspace})"
        )

    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"cursor-agent failed (exit {proc.returncode}, "
            f"model={config.model}): {err_text or 'no stderr'}"
        )

    return stdout.decode("utf-8", errors="replace").strip()


class CursorComposerProvider:
    """Provider wrapper around the ``cursor-agent`` CLI for hermes-agent.

    Exposes a uniform ``complete(messages)`` so the Mixture-of-Agents tool
    can treat cursor models interchangeably with OpenRouter-hosted models.
    """

    api_mode = "cursor_composer"
    label = "Cursor Composer 2.5"

    def __init__(
        self,
        config: Optional[CursorComposerConfig] = None,
        binary: Optional[str] = None,
    ):
        self.config = config or CursorComposerConfig()
        self.binary = binary

    @classmethod
    def from_config(
        cls,
        cfg: Optional[dict] = None,
        binary: Optional[str] = None,
    ) -> "CursorComposerProvider":
        cfg = cfg or {}
        extra_args = list(cfg.get("extra_args") or ())
        return cls(
            CursorComposerConfig(
                model=cfg.get("model", DEFAULT_MODEL),
                workspace=cfg.get("workspace"),
                api_key=cfg.get("api_key") or os.environ.get("HERMES_CURSOR_API_KEY") or os.environ.get("CURSOR_API_KEY"),
                extra_args=extra_args,
                timeout_seconds=int(
                    cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
                ),
            ),
            binary=binary,
        )

    async def complete(self, messages: List[dict], **kwargs) -> NormalizedResponse:
        prompt = _flatten_messages(messages)
        text = await invoke_cursor_agent(prompt, self.config, self.binary)
        return NormalizedResponse(
            content=text,
            tool_calls=[],
            finish_reason="stop",
            reasoning=None,
            usage=Usage(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cached_tokens=0,
            ),
            provider_data={
                "model": self.config.model,
                "api_mode": self.api_mode,
                "workspace": self.config.workspace,
            },
        )

    async def complete_text(self, messages_or_prompt) -> str:
        """Return just the final text -- used by the MoA aggregator role."""
        if isinstance(messages_or_prompt, str):
            return await invoke_cursor_agent(
                messages_or_prompt, self.config, self.binary
            )
        resp = await self.complete(messages_or_prompt)
        return resp.content
