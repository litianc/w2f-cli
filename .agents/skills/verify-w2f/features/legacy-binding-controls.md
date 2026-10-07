# Legacy binding confirmation controls

The existing single-machine management UI lets an operator confirm or cancel group-name following and membership synchronization independently. These controls are not yet cloud features.

## Sub-features

- `legacy-binding-cancel`: cancelling name following leaves stored settings disabled.
- `legacy-binding-name`: explicit enable/stop controls persist the group-name-following setting.
- `legacy-binding-members`: enable/stop controls persist membership synchronization and explain member-removal consequences.
- `legacy-binding-history`: membership operation records remain distinct from message delivery.

## How to get to it (user POV)

- In the single-machine management UI, choose `群映射`.
- Choose `开启群名跟随` / `停止群名跟随`, or `开启成员同步` / `停止成员同步`.
- Read the confirmation dialog and choose submit/cancel; choose `成员操作记录` for the related history explanation.

## Driving it with qa.run

Preconditions: install `requirements-qa.txt` into `.venv-cloud`, use the dedicated browser cache from the parent skill, and choose a fresh report output directory. This harness creates its own temporary HTTP server and SQLite; no cloud fixture is needed.

- **Drive the existing regression.** Run `.venv-cloud/bin/python -m qa.run --check browser.binding_maintenance --timeout 600 --output "$PWD/.qa-results/verify-w2f/legacy-binding-proof"`.
- **Verify actions and persistence.** The implementation in `qa/browser_check.py::_binding_maintenance_controls` clicks the actual named buttons, checks `#dialogNote`, uses `#cancel` / `#submit`, waits for real `/api/bindings` POST responses, and independently reads temporary SQLite for cancelled name following and confirmed membership enable/stop. Confirmed name-following enable/stop is observed through successful POSTs and refreshed controls; the history check verifies its explanation, not operation rows.
- **Retain and evaluate evidence.** Require report check ID `browser.binding_maintenance` to be `passed`, inspect its evidence in `report.json` / `report.md`, and run `.venv-cloud/bin/python -m qa.gate .qa-results/verify-w2f/legacy-binding-proof/report.json`. Choose a new output directory for a later proof rather than overwriting evidence. The existing harness captures textual behavioral evidence, not screenshots.

## Gotchas

- `--check` selects the reported browser result but the current runner still executes the browser group. It is not a one-scenario launch optimization.
- The harness uses the real HTTP handler and temporary storage with synthetic external CLI boundaries. Its blocked-destination and unexpected-boundary checks are part of the proof; it does not prove actual Feishu membership changes.
- Starting `agents.manage serve` directly can start real reminder work. Use `qa.browser_harness.isolated_manager` through the documented runner instead.
- Omit publishing flags, including `--dry-run` (which still contacts GitHub). A blocked/not-applicable report is not success, and this recipe cannot replace Windows or real-message acceptance.
