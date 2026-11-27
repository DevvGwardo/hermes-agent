# Fork Patch Inventory & Convergence Roadmap (`DevvGwardo/hermes-agent`)

This file tracks the fork's patch surface against `NousResearch/hermes-agent:main` and the concrete steps to shrink it so automated upstream syncs converge without manual intervention.

## 1. Sync Automation Architecture

- **3x/day sync (`maiavm-upstream-sync.yml`):** Runs at `06:17`, `14:17`, `22:17` UTC with `git rerere` + `actions/cache` on `.git/rr-cache`, keeping batches small (~40–170 commits) and replaying prior conflict resolutions automatically.
- **Lag-based watchdog (`maiavm-sync-watchdog.yml`):** Runs 2x/day (`07:49`, `19:49` UTC), measures actual `origin/maiavm..upstream/main` commit lag, alerts on >600 commits behind / failed runs / stale PRs (>48h), and self-heals dropped crons by dispatching `maiavm-upstream-sync.yml`.
- **Fork CI (`maiavm-fork-verify.yml`):** Fast `uv run pytest` check on fork-owned/modified test suites. Inherited upstream workflows targeting `blacksmith-*` runners are disabled via GitHub API (`disabled_manually`) so they never hang PRs.
- **Local helper (`scripts/sync-upstream.ps1`):** Standardizes local `rerere`-enabled conflict resolution on `sync/upstream-YYYYMMDD`.

## 2. Prioritized Patch-Shrinking Roadmap

### Tier 1 — Zero-core removals (move to `hermes-deploy` / native upstream features)
1. **`gateway.pairing_instructions` (~46 lines across `cli-config.yaml.example`, `hermes_cli/config_defaults.py`, `hermes_cli/web_server_config.py`, `gateway/config.py`, `gateway/config_loader.py`, `gateway/run_inbound.py`, `gateway/run_inbound_unauthorized.py`):**
   Replace with upstream's native `$HERMES_HOME/locales/<lang>.yaml` overlay (`gateway.pairing.code_reply`). Eliminates edits to `cli-config.yaml.example` (100+ upstream commits/90d).
2. **`terminal.sync_back_skills` default (+3 lines in `hermes_cli/config_defaults.py`):**
   Move to `/etc/hermes/config.yaml` managed overlay written by `hermes-deploy`.
3. **`_MESSAGING_ENV_FALLBACKS` email rows (`hermes_cli/web_routers/messaging.py`):**
   Already redundant with `plugins/platforms/email/plugin.yaml::optional_env`; move remaining platform rows to plugin manifests.

### Tier 2 — Minimal-hook isolation in hot upstream files
4. **`gateway/platforms/api_server.py` `dashboard_runs` registration (22 lines → 1-line marker or plugin factory):**
   Upstream `connect()` already calls `self._wire_plugin_handlers(self._app)`. Keep the `maiavm-native-dashboard-runs` / `_dash_register` / `_dashboard_run_registry` marker for `tests/gateway/test_api_server_dashboard_runs_native.py`.
5. **`gateway/platforms/api_server_runs.py` (+131/−14 → ~10 lines):**
   Delegate `RunPromptBroker` helpers, `/secret`, and `_handle_run_clarify` branching into fork-owned `gateway/platforms/api_server_run_prompts.py`.
6. **`gateway/session_execution.py` (+35/−2 → ~6 lines):**
   Move mid-turn eviction helper into a fork-owned module.
7. **`gateway/run_agent_cache.py` (+6/−1 → 1 line):**
   Use `getattr(runtime.get("api_key"), "cache_identity", None) or str(...)`.
8. **`tools/environments/ssh.py` test alias:**
   Drop `_load_hermes_env_vars = load_hermes_env_vars` (which upstream intentionally removed) and patch `load_hermes_env_vars` directly in tests.

### Tier 3 — Upstreamable general bug fixes (via neutral PRs)
- `plugins/platforms/email/plugin.yaml` (+4: `EMAIL_IMAP_PORT` in `optional_env`)
- `tools/code_execution_env.py` (+1/−1: pop `HERMES_TIMEZONE_FROM_CONFIG`)
- `tools/mcp_tool_lifecycle.py` + `tools/mcp_tool_discovery.py` (`max_live_helpers` cap)
- `agent/secret_scope.py` + `hermes_cli/config.py` (`.env` line-boundary hardening — brings `hermes_cli/config.py` to 0 diff)
- `agent/command_token_source.py` (LRU + profile pinning)
- `tools/environments/ssh.py` (remote process-group kill on timeout) & `tools/environments/file_sync.py` (`sync_back_bounded`)
