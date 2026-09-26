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
