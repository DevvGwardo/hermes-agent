"""Pokemon Agent tool — HTTP API interface to the pokemon-agent server.

Registers six LLM-callable tools:
- ``pokemon_observe``     -- get game state + screenshot + memory in one call
- ``pokemon_action``      -- execute game actions (walk, press buttons, etc.)
- ``pokemon_remember``    -- write an observation to persistent memory
- ``pokemon_update_goal`` -- overwrite the current objective or team section
- ``pokemon_save``        -- save emulator state to a named checkpoint
- ``pokemon_load``        -- load a previously saved checkpoint

The server URL is read from ``POKEMON_AGENT_URL`` env var
(default: http://localhost:8765).
"""

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE_URL: str = ""


def _get_url() -> str:
    return (_BASE_URL or os.getenv("POKEMON_AGENT_URL", "http://localhost:8765")).rstrip("/")


def _check_pokemon_api() -> bool:
    """Return True if the pokemon-agent server is reachable."""
    try:
        import httpx
        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{_get_url()}/health")
            return resp.status_code == 200 and resp.json().get("emulator_ready", False)
    except Exception as e:
        logger.debug("Pokemon API check failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _pokemon_observe(args: Dict[str, Any], **kw) -> str:
    """Combined endpoint: game state + screenshot path + full memory context."""
    import httpx
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_get_url()}/observe")
            resp.raise_for_status()
            data = resp.json()

            # Save screenshot to tmp for vision_analyze
            if data.get("screenshot"):
                import base64
                png = base64.b64decode(data["screenshot"])
                path = "/tmp/pokemon_screen.png"
                with open(path, "wb") as f:
                    f.write(png)
                data["screenshot_path"] = path
                del data["screenshot"]  # don't dump base64 into text context

            return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Observe failed: {e}"})


def _pokemon_action(args: Dict[str, Any], **kw) -> str:
    """Execute a sequence of game actions."""
    import httpx
    actions = args.get("actions", [])
    if not actions:
        return json.dumps({"error": "No actions provided"})

    # Client-side guard: collapse repeated press_a into a_until_dialog_end
    lower = [a.strip().lower() for a in actions]
    if len(lower) > 2 and all(a == "press_a" for a in lower):
        actions = ["a_until_dialog_end"]

    # Cap at 8 actions per call
    if len(actions) > 8:
        actions = actions[:8]

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(f"{_get_url()}/action", json={"actions": actions})
            resp.raise_for_status()
            data = resp.json()

            # Compact the state_after for readability
            state = data.get("state_after", {})
            summary = {
                "success": data.get("success"),
                "actions_executed": data.get("actions_executed"),
                "actions_count": data.get("actions_count"),
                "auto_observations": data.get("auto_observations", []),
                "player": state.get("player"),
                "battle": state.get("battle"),
                "map": state.get("map"),
                "dialog": state.get("dialog"),
            }
            return json.dumps(summary, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Action failed: {e}"})


def _pokemon_remember(args: Dict[str, Any], **kw) -> str:
    """Write an observation to persistent memory."""
    import httpx
    text = args.get("text", "")
    priority = args.get("priority", "\U0001f7e2")  # 🟢 default
    prefix = args.get("prefix")
    if not text:
        return json.dumps({"error": "No text provided"})
    try:
        with httpx.Client(timeout=10) as client:
            payload = {"text": text, "priority": priority}
            if prefix:
                payload["prefix"] = prefix
            resp = client.post(f"{_get_url()}/memory/observe", json=payload)
            resp.raise_for_status()
            return json.dumps(resp.json())
    except Exception as e:
        return json.dumps({"error": f"Remember failed: {e}"})


def _pokemon_update_goal(args: Dict[str, Any], **kw) -> str:
    """Overwrite a structured memory section (Current Objective, Current Team, etc.)."""
    import httpx
    section = args.get("section", "Current Objective")
    lines = args.get("lines", [])
    if not lines:
        return json.dumps({"error": "No lines provided"})
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{_get_url()}/memory/section",
                json={"section": section, "lines": lines},
            )
            resp.raise_for_status()
            return json.dumps(resp.json())
    except Exception as e:
        return json.dumps({"error": f"Update goal failed: {e}"})


def _pokemon_save(args: Dict[str, Any], **kw) -> str:
    """Save emulator state to a named checkpoint."""
    import httpx
    name = args.get("name", "checkpoint")
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(f"{_get_url()}/save", json={"name": name})
            resp.raise_for_status()
            return json.dumps(resp.json())
    except Exception as e:
        return json.dumps({"error": f"Save failed: {e}"})


def _pokemon_load(args: Dict[str, Any], **kw) -> str:
    """Load emulator state from a named checkpoint."""
    import httpx
    name = args.get("name", "checkpoint")
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(f"{_get_url()}/load", json={"name": name})
            resp.raise_for_status()
            return json.dumps(resp.json())
    except Exception as e:
        return json.dumps({"error": f"Load failed: {e}"})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry

registry.register(
    name="pokemon_observe",
    toolset="pokemon",
    schema={
        "name": "pokemon_observe",
        "description": (
            "Get the current Pokemon game state (player position, party, map, "
            "battle, dialog, badges, bag), a screenshot saved to /tmp/pokemon_screen.png, "
            "and the full persistent memory context. Call this every turn before deciding."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    handler=_pokemon_observe,
    emoji="\U0001f441",  # 👁
)

registry.register(
    name="pokemon_action",
    toolset="pokemon",
    schema={
        "name": "pokemon_action",
        "description": (
            "Execute game actions in sequence. Max 6-8 actions per call, then observe again. "
            "IMPORTANT RULES: "
            "(1) For dialog/text on screen, use a_until_dialog_end — NEVER send multiple press_a. "
            "(2) For movement, send 2-4 walk_ actions then observe. "
            "(3) Always call pokemon_observe after actions to see the result. "
            "Available actions: "
            "walk_up, walk_down, walk_left, walk_right (move 1 tile), "
            "press_a (confirm/talk — ONE press), press_b (cancel/run), "
            "press_start (menu), press_select, "
            "a_until_dialog_end (advance ALL dialog text — use this instead of repeated press_a), "
            "hold_BUTTON_FRAMES (e.g. hold_b_120 to speed text), "
            "wait_FRAMES (e.g. wait_60 = ~1 second). "
            "State changes (badges, catches, level ups) auto-record to memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "2-8 actions per call. Use a_until_dialog_end for text, "
                        "not repeated press_a. Example: "
                        "[\"walk_up\", \"walk_up\", \"press_a\", \"a_until_dialog_end\"]"
                    ),
                },
            },
            "required": ["actions"],
        },
    },
    handler=_pokemon_action,
    emoji="\U0001f3ae",  # 🎮
)

registry.register(
    name="pokemon_remember",
    toolset="pokemon",
    schema={
        "name": "pokemon_remember",
        "description": (
            "Write an observation to persistent Pokemon memory. "
            "Use priority levels: "
            "\U0001f534 (critical — badges, team, key decisions), "
            "\U0001f7e1 (relevant — route knowledge, strategy), "
            "\U0001f7e2 (routine — minor notes). "
            "Use PKM: prefixes to categorize: "
            "PKM:OBJECTIVE, PKM:MAP, PKM:STRATEGY, PKM:PROGRESS, "
            "PKM:STUCK, PKM:TEAM, PKM:BATTLE, PKM:ITEM."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The observation to record",
                },
                "priority": {
                    "type": "string",
                    "enum": ["\U0001f534", "\U0001f7e1", "\U0001f7e2"],
                    "description": "Priority: \U0001f534 critical, \U0001f7e1 relevant, \U0001f7e2 routine",
                },
                "prefix": {
                    "type": "string",
                    "description": "Category prefix (e.g. PKM:MAP, PKM:STRATEGY)",
                },
            },
            "required": ["text", "priority"],
        },
    },
    handler=_pokemon_remember,
    emoji="\U0001f4dd",  # 📝
)

registry.register(
    name="pokemon_update_goal",
    toolset="pokemon",
    schema={
        "name": "pokemon_update_goal",
        "description": (
            "Overwrite a structured section in memory. Use this to update "
            "'Current Objective' when goals change, or 'Current Team' when "
            "party composition changes. This replaces the section contents "
            "entirely (not append)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "Section name: 'Current Objective', 'Current Team', or 'Milestones'",
                },
                "lines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New lines for the section",
                },
            },
            "required": ["section", "lines"],
        },
    },
    handler=_pokemon_update_goal,
    emoji="\U0001f3af",  # 🎯
)

registry.register(
    name="pokemon_save",
    toolset="pokemon",
    schema={
        "name": "pokemon_save",
        "description": "Save game state to a named checkpoint. Do this before gym battles, rare catches, and dungeons.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Checkpoint name (e.g. 'before_brock', 'mt_moon_entrance')",
                },
            },
            "required": ["name"],
        },
    },
    handler=_pokemon_save,
    emoji="\U0001f4be",  # 💾
)

registry.register(
    name="pokemon_load",
    toolset="pokemon",
    schema={
        "name": "pokemon_load",
        "description": "Load a previously saved checkpoint. Use to retry gym battles or recover from mistakes.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Checkpoint name to load",
                },
            },
            "required": ["name"],
        },
    },
    handler=_pokemon_load,
    emoji="\U0001f4be",  # 💾
)
