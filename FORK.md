# DevvGwardo/hermes-agent — the Maia VM fork

This fork is the hermes-agent build that runs on Maia VM / Nub Agent hosted VMs (provisioned by `Maia-VM/hermes-deploy`). Its changes are **fork-only**. Nothing here is proposed to or pushed to `NousResearch/hermes-agent`, and PRs from this repo must never target it.

## Branches

| Branch | What it is | Who writes it |
|---|---|---|
| `upstream` | An exact mirror of `NousResearch/hermes-agent` `main`. Never commit here. | `maiavm-upstream-sync` workflow |
| `maiavm` | What production runs: upstream plus the fork patch layer. All fork PRs target this branch. | PRs only |
| `main` | Legacy. Frozen at the pre-`maiavm` baseline (`797d8bd`) until hermes-deploy is confirmed to install from `maiavm` or a tag. Don't build on it. | Nobody |
| `sync/upstream-YYYYMMDD` | Upstream merged into `maiavm`, opened as a PR for review. | sync workflow |

GitHub opens fork PRs against the parent repo by default. When you open a PR, check that the base is **`DevvGwardo/hermes-agent` → `maiavm`**. Setting `maiavm` as the default branch (Settings → General → Default branch) makes that the default for branch pushes.

## Releases and pinning

- Production (the hermes-deploy VM image) pins a **tag** like `maiavm-2026.09.26` or an exact commit SHA. It never follows a moving branch.
- To cut a tag, run Actions → **maiavm release tag** → Run workflow on `maiavm`. It tags the current `maiavm` head as `maiavm-YYYY.MM.DD` (adding `-2`, `-3`… for more tags on the same day).
- Before moving the fleet to a new tag, roll one canary VM with it and click through the Nub Agent app. Only then bump the pin in hermes-deploy.
- Tag names deliberately don't match `v*.*.*`, so they never trigger the inherited upstream release and installer workflows.

## Keeping up with upstream

Upstream moves hundreds of commits a day. The `maiavm-upstream-sync` workflow runs daily at 06:17 UTC; you can also run it by hand. Each run:

1. Mirrors upstream `main` to the `upstream` branch.
2. If `maiavm` already contains it, stops.
3. Otherwise merges it into `sync/upstream-YYYYMMDD`:
   - **Clean merge:** opens a PR into `maiavm`.
   - **Conflicts:** opens (or updates) one issue listing the conflicting files, so someone resolves them on a sync branch.

Sync PRs are never auto-merged. Review them like any other change, since upstream behaviour changes reach production through them.

**Token.** Upstream often changes `.github/workflows/*`. The default `GITHUB_TOKEN` is not allowed to push workflow changes, and PRs it opens don't trigger CI. Add a fine-grained PAT as the repo secret **`MAIAVM_SYNC_TOKEN`** with these permissions on this repo only:

- Contents: read/write
- Pull requests: read/write
- Issues: read/write
- Workflows: read/write

Without the secret the workflow still runs, but mirroring fails whenever upstream touched a workflow file.

Scheduled workflows are off by default in forks. Enable them once under the **Actions** tab.

## Keeping the patch layer thin

Every line that differs from upstream is a potential conflict on each sync.

- **SaaS-specific behaviour** (Nub wording, defaults, extra dashboard routes) belongs in a plugin installed by hermes-deploy (see `plugins/` and the dashboard plugin routers under `/api/plugins/<name>`), or in the managed-scope config (`/etc/hermes/config.yaml`, `hermes_cli/managed_scope.py`) that hermes-deploy writes on each VM. It does not belong in core files here.
- **Core patches** are only for things a plugin can't do. Keep each one small and conventional-commit titled, and list it below so sync conflicts are easy to reason about.

### Current core patches (on top of `upstream`)

- **Install and CI fixes:** `scripts/install.sh` npm retry and error capture; sandbox Node CA and nodedir; CI noreply email mapping. These are the fork's original 6 commits.
- **Dashboard API for the Nub Agent app** (PR #2):
  - skill readiness in `GET /api/skills`;
  - memory write-approval REST;
  - per-platform toolset toggle;
  - `gateway.pairing_instructions`;
  - `POST /api/pairing/deny`;
  - messaging catalog `extra_env_vars` and plain values for non-secret rows;
  - opt-in secret and clarify prompts on `/v1/runs`.
- **Notification bell API for the Nub Agent app** (maiavm-desktop#17): `GET /api/notifications` and `POST /api/notifications/read` (`hermes_cli/web_routers/notifications.py`), plus the inbox and GitHub poller in `hermes_cli/notifications_bus.py`. Core because plugin routes skip dashboard auth. The only edits to upstream files are the router mount and the poller start/stop in `hermes_cli/web_server.py` (8 lines). `HERMES_NOTIFICATIONS_GITHUB=0` turns the poller off.
- **Dashboard runs API for the Nub chat** (`/v1/dashboard/runs`): `gateway/platforms/run_buffer.py` and `gateway/platforms/dashboard_runs.py`, registered by one block in `APIServerAdapter.connect()` right after `self._app["api_server_adapter"] = self`. hermes-deploy used to patch this onto every VM (its "runs overlay"); the `maiavm-native-dashboard-runs` comment in that block tells its installer and overlay repair to stop. Keep that marker string, and the `_dash_register` / `_dashboard_run_registry` names, which older repair scripts on VMs check before patching. The two modules started byte-identical to hermes-deploy's `scripts/_overlay-files/` copies; change them here from now on, and only after every VM on a native tag runs a repair script that honours the marker (otherwise an old script rewrites them from its embedded copy at the next gateway start). Test: `tests/gateway/test_api_server_dashboard_runs_native.py`; end-to-end smokes (images, profile secret scope, secret/clarify prompts, inline delegation) in `scripts/nub-smoke/`, run from the repo root with the venv python. Since 2026-10-15 hermes-deploy no longer patches anything onto VMs, so these modules are the only copy.
- **SSH backend stops the remote command on a timeout or interrupt** (`tools/environments/ssh.py`): upstream kills only the local `ssh` client, and sshd (no pty) never signals the remote command, so one that never writes output runs forever (a self-matching `pgrep -f` wait loop ran 17h in hermes-deploy's tool sandbox). Each command carries a random `hermes-call-…` token in its remote argv; While the call's wrapper is still running (its closing CWD marker line has not streamed past yet: `_BoundedOutputCollector.watch_cwd_marker` matches it per chunk, so truncation can't hide it and an echo of the wrapper's argv can't fake it; `_wait_for_process` exposes the collector on the process), `_kill_process` hands a bounded, detached, reaped helper a second ssh session that TERMs, then KILLs, the process group of the topmost token holder. A job the call left in the background (the wrapper already finished) is left alone, as are the local client and, on a loopback target, the caller's group. Same group kill as the local backend. Tests: `TestRemoteKillOnTimeout` and the real-process tests in `tests/tools/test_ssh_environment.py`. Reviewed in five rounds by a second model (gpt-6-astra); verified end to end on a Cube sandbox.
