# Gates: live `subagent.steer` control on the hermes desktop

Scope: complete the chief-of-staff "control agents" surface in hermes-agent by
adding live **steer** (redirect a running child's goal) — today the desktop
overlay only does pause / interrupt / kill. Mirrors the existing
`subagent.interrupt` path end-to-end: Python engine fn → gateway RPC → TUI
overlay control. Implementation only; no unrelated edits.

- [x] G1: `steer_subagent(subagent_id, goal)` added to `tools/delegate_tool.py`
      and unit-tested (redirects child via its thread-safe `AIAgent.steer`).
  CHECK: ./venv/bin/python -m pytest tests/tools/test_delegate.py::TestSubagentSteer -q
  EXPECT: passed
  EVIDENCE: `5 passed in 1.19s`; full test_delegate.py `126 passed in 3.84s`
  FILES:  tools/delegate_tool.py:206  tests/tools/test_delegate.py (TestSubagentSteer)

- [x] G2: `@method("subagent.steer")` RPC added to `tui_gateway/server.py`
      (requires subagent_id + goal; returns {found, subagent_id}).
  CHECK: grep -n '@method("subagent.steer")' tui_gateway/server.py
  EXPECT: match
  EVIDENCE: `2571:@method("subagent.steer")`

- [x] G3: `SubagentSteerResponse` type added to `ui-tui/src/gatewayTypes.ts`
      and imported by `agentsOverlay.tsx`.
  CHECK: grep -nE 'SubagentSteerResponse' ui-tui/src/gatewayTypes.ts ui-tui/src/components/agentsOverlay.tsx
  EXPECT: both files
  EVIDENCE: gatewayTypes.ts:425 (interface); agentsOverlay.tsx:16 (import), :839/:841 (use)

- [x] G4: Overlay has a `steer` action + `r` keybinding that opens an inline
      `TextInput` to collect the new goal, plus updated controls hint.
  CHECK: grep -nE "subagent.steer|steerOne|doSteer|ch === 'r' && selected|TextInput" ui-tui/src/components/agentsOverlay.tsx
  EXPECT: all present
  EVIDENCE: agentsOverlay.tsx:2 (TextInput import), :825 (steerOne), :831 (doSteer),
            :839 (subagent.steer request), :978 (r keybinding), :1091 (TextInput render).
            controlsHint updated: `· x kill · X subtree · r redirect · p ...`

- [x] G5: Python modules import cleanly (no syntax/import errors).
  CHECK: ./venv/bin/python -c "import tools.delegate_tool, tui_gateway.server"
  EXPECT: (empty stderr)
  EVIDENCE: `IMPORTS_OK`

- [x] G6: ui-tui type-checks clean.
  CHECK: cd ui-tui && npx tsc --noEmit -p tsconfig.json
  EXPECT: exit 0
  EVIDENCE: `TYPECHECK_EXIT=0`

- [x] G7 (bonus): ui-tui full test suite + build clean.
  CHECK: cd ui-tui && npx vitest run && npm run build
  EXPECT: all tests pass, build exit 0
  EVIDENCE: `53 passed` test files / `548 passed` tests; build `Successfully compiled 117 files`; BUILD_EXIT=0
