<!--
DevvGwardo/hermes-agent is the Maia VM fork (see FORK.md).
Base branch must be DevvGwardo/hermes-agent → maiavm. Never open fork PRs against NousResearch/hermes-agent.
-->

## What does this change?

<!-- The problem and why this approach. -->

## Could this live outside core?

<!-- Every core line is a conflict on each upstream sync. Why isn't this a plugin (plugins/, /api/plugins/<name>) or a managed-scope setting written by hermes-deploy? -->

## Changes

- 

## How to test

1. 

## Production safety

- [ ] Base is `maiavm` (not `main`, not upstream)
- [ ] Backwards compatible for existing clients (web dashboard, apps/desktop, Nub Agent app) — or the break is called out above
- [ ] New config keys are in `hermes_cli/config_defaults.py` and `cli-config.yaml.example`
- [ ] Tests added/updated; relevant modules pass (`scripts/run_tests.sh <paths>`)
- [ ] No secrets logged or echoed in responses/events
- [ ] FORK.md "Current core patches" updated if this adds a core patch
- [ ] Rolls out via a new `maiavm-*` tag + canary VM before the hermes-deploy pin moves
