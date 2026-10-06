# Cloud management status

The management page shows service readiness, the running environment and build, and the notice that text upload/query is available, provisioning uses restricted commands, and Feishu delivery is pending.

## Sub-features

- `cloud-status-page`: the authenticated browser displays the real management page.
- `cloud-status-build`: rendered environment/version agree with the authenticated status API and owned image.
- `cloud-status-readiness`: the page shows `服务已就绪` for the healthy fixture.
- `cloud-status-capabilities`: the page says `文本采集接入已开放`; it does not present unfinished features as usable.

## How to get to it (user POV)

- Open `/` at the management HTTPS URL and log in.
- Query `/api/status` to inspect the complete build identity.
- Operators query `/health/live` and `/health/ready` for process and dependency readiness respectively.

## Driving it with control.py

Preconditions: launched fixture, passing doctor and installed Chromium in the dedicated cache.

- **Open management.** Run `.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py drive "$W2F_VERIFY_RUN" --feature dashboard`. The driver authenticates against both routes, then opens `/` in a fresh Chromium context.
- **Read the page.** The driver uses `get_by_role('heading', name='云端管理', exact=True)` and `get_by_role('heading', name='服务已就绪', exact=True)`. It checks `main` for `环境：v040-ci`, the first 12 characters of the recorded commit, and `文本采集接入已开放`.
- **Read both probes.** The same drive records GET `/health/live` and GET `/health/ready` in `actions.json`; both must return 200 and `{"status":"ok"}`. Testing degraded readiness additionally requires an explicit owned-fixture fault and recovery proof; a healthy page does not cover it.
- **Inspect the proof.** Open `evidence/dashboard.png`, read `dashboard.aria.txt`, and compare to `actions.json` and `doctor.json`. Require a 200 browser response, correct text and zero unexpected browser destinations.

## Gotchas

- Browser trust uses a pin limited to this fixture's server key; the HTTP doctor independently validates its CA chain and IP. This does not test public-device CA installation.
- The environment and commit are user-visible facts. A screenshot from the persistent local or public instance is not valid fixture evidence.
- A healthy page does not prove collection, archive writes, user management or Feishu delivery.
