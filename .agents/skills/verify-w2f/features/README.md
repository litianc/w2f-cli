# W2F verification map

Read this index before choosing a proof. This map covers the cloud entry, synthetic message processing and one existing single-machine UI path; it does not declare all W2F features covered.

## Baseline preconditions

- Follow `../SKILL.md` from the v0.4.0 checkout with Python 3.12. Docker Compose and the browser are needed for cloud proof, not synthetic messages.
- `W2F_VERIFY_RUN` is the absolute directory printed by `control.py launch`; `W2F_MESSAGE_RUN` is the separate directory from `messages.py launch`.
- Require `doctor` to pass before every cloud drive. Each fixture owns its dynamic HTTPS port, fresh passwords/CA, four product containers and two persistent volumes.
- Use the generated fixture only; keep the existing local and public management services running independently.
- Browser binaries use the dedicated `PLAYWRIGHT_BROWSERS_PATH` from the skill. No WeChat or Feishu credentials are needed.

## Driving conventions

- Invoke `control.py` through `.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py`.
- Cloud selectors are role/name based; HTTP proof uses the exact public routes below.
- The supplementary legacy recipe launches its own real HTTP handler and temporary SQLite with synthetic external boundaries; it does not attach to the cloud fixture.
- Record a skipped entry point as skipped, with its missing precondition. Native browser credential dialogs, public CA installation, Windows hardware, real collection and Feishu delivery require separate evidence.

## Proof and skip reporting

Report feature ID, actual commands, source identity, result and retained evidence path. Inspect the screenshot and accessible snapshot for UI proof. Changes affecting several entry points must drive all relevant map entries. One executed feature validates the generated skill, not the entire map. For mutations, pair the browser result with persisted state; for cleanup, check that the fixture disappears and evidence remains.

## Features

- [Live text verification](live-text.md): M3 real cloud/Feishu and M4 Mac input; explicit private configuration, bounded sending/readback, recipient accessibility confirmation.

- [Lark CLI recipient readback](recipient-readback.md): exact real text receipts fetched as the receiving user; automatic acceptance, explicit Profile/identity, no new send.

- [Cloud delivery](cloud-delivery.md): actual cloud sender and private Feishu HTTP fixture; ordered persisted parts, retry/unknown outcomes, ownership and recovery.

- [Text cloud intake](text-cloud-e2e.md): actual client, verified HTTPS and Postgres; lost acknowledgments, restart, tenant authorization, ordered atomic writes and seven-day retention. No Feishu sending.
- [Cloud login](cloud-login.md): anonymous challenge, wrong password, correct credentials at both protected routes.
- [Cloud management status](cloud-status.md): real browser page, environment/build, health and capability notice.
- [Isolated runtime lifecycle](runtime-lifecycle.md): independent instances, ownership checks and scoped teardown.
- [Legacy binding confirmation controls](legacy-binding-controls.md): existing browser/SQLite confirmation and cancellation regression; not cloud functionality.
- [Synthetic source messages](synthetic-messages.md): inject text through an executable `ordered_sync_v1` reader; verify the new client's durable queue or the legacy Worker's polling, paging, rendering/receipts and restart without duplicate sends.

Device provisioning and archive queries are currently command/API operations covered by text cloud intake. Add UI entries when management forms and archive browsing land.
