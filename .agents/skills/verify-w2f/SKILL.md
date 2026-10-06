---
name: verify-w2f
description: Verify W2F cloud management, HTTPS authentication, and runtime isolation, or inject synthetic WeChat text at the reader boundary to test downstream Worker processing, persistence, and replay. Verify actual client uploads and ordered cloud sending with a disposable Postgres stack and private Feishu HTTP fixture. Use after cloud/client changes or when message tests need a controllable source.
---

# Verify W2F

Run from the **v0.4.0 development checkout**, never main. Read [features/README.md](features/README.md), then choose the relevant path:

- Cloud sending, persisted parts and failure recovery: follow [Cloud delivery](features/cloud-delivery.md), with a private simulated Feishu endpoint.
- Client upload, tenant isolation, cloud archive and receipt recovery: follow [Text cloud intake](features/text-cloud-e2e.md). This runs the actual client and Postgres with a synthetic reader; it does not send Feishu messages.
- Cloud management at `/` and `/api/status`: follow the container/browser recipe below.
- Mock a WeChat message, test collection processing or replay: follow [Synthetic source messages](features/synthetic-messages.md). Its `messages.py collect` drives the new client's local queue; `drive` exercises the legacy Worker with a synthetic Feishu CLI. Both need no Docker, browser or real account.
- Legacy `agents.manage` UI controls: use the separate browser harness in the feature map.

The cloud exposes deployment status, administrator Basic authentication, device-authenticated text intake and message queries. Restricted operator commands provision and disable users/devices/bindings. The client persists receipts and retains confirmed copies for seven days. The independent cloud sender supports text delivery; its default verification uses a simulated Feishu HTTP service. Full management UI and real Feishu reception remain separate milestones.

## Launch

Requires macOS or Linux, Python 3.12, a running Docker engine with Compose v2, and registry/package access for first setup. Windows collector acceptance is separate. The repository path must satisfy `cloud.runtime.initialize`'s state-path validation (no spaces). Use the repository's `.venv-cloud`; create it if absent:

```sh
python3.12 -m venv .venv-cloud
.venv-cloud/bin/python -m pip install -r cloud/requirements-test.txt 'playwright>=1.51,<2'
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.qa-results/verify-w2f/browsers"
export PLAYWRIGHT_SKIP_BROWSER_GC=1
.venv-cloud/bin/python -m playwright install chromium
```

On Linux, install missing browser OS dependencies with the Playwright `install-deps chromium` command only when launch reports them. The cloud-only path does not need WeChat, Feishu credentials, ffmpeg, or the legacy QA dependencies.

Use this complete recipe; the EXIT trap also cleans up failed doctor/drive attempts:

```sh
set -e
W2F_VERIFY_RUN=$(.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py launch)
trap '.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py cleanup "$W2F_VERIFY_RUN"' EXIT
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py doctor "$W2F_VERIFY_RUN"
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py drive "$W2F_VERIFY_RUN" --feature dashboard
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py cleanup "$W2F_VERIFY_RUN"
trap - EXIT
test -s "$W2F_VERIFY_RUN/evidence/dashboard.png"
test -s "$W2F_VERIFY_RUN/evidence/actions.json"
test -s "$W2F_VERIFY_RUN/evidence/cleanup.json"
printf 'Retained evidence: %s/evidence\n' "$W2F_VERIFY_RUN"
```

`launch` prints only its absolute run directory. It uses `cloud.runtime.initialize`, the real Dockerfile, and the repository Compose stack with a fixture-owner gateway user, as in `scripts/test_v040_stack.py`. It builds the current source for the host platform, labels a unique image, binds a dynamically allocated loopback port, and creates a unique `w2f-v040-verify-*` project with fresh credentials, CA, Postgres and media volumes. Readiness means four owned running product containers, healthy app/database/sender, the expected image ID, and a strictly verified authenticated HTTPS response identifying the build. No seed business data is needed.

Two runs can coexist; each has its own port, image, state, browser context and volumes. A port-allocation race must fail the run; clean up and launch afresh. Never substitute the existing local port 13040, the public server, or a main data directory for a failed fixture.

## Doctor

```sh
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py doctor "$W2F_VERIFY_RUN"
```

This observes live container ownership, image identity, unpublished DB/API ports, private volume names, TLS trust, valid authentication, environment and commit. It also detects relevant source changes since launch. It writes only diagnostic evidence; it does not reconfigure the service or seed data. The app's GET status performs its normal DB query and temporary writable-media probe. Require `result: passed` before driving. On failure, read `evidence/commands.log` and `build.log`; clean up the owned run before retrying. Do not mistake a partial failed launch for a reusable fixture.

## Drive

- Authentication and both protected HTTP entry points: `control.py drive "$W2F_VERIFY_RUN" --feature auth` (invoke through `.venv-cloud/bin/python`, as above).
- Authentication **plus the real Chromium management page**: `control.py drive "$W2F_VERIFY_RUN" --feature dashboard`.
- Lifecycle and the existing single-machine UI have their own recipes in the feature map. Select the recipe matching the change; one dashboard screenshot is not coverage of every mapped entry point.

The HTTP driver checks both `/health/live` and `/health/ready`, then exercises anonymous, wrong-password and successful requests at both `/` and `/api/status`. The browser opens `/` with the fixture's Basic-auth credentials and requires title `W2F 云端管理`, heading `云端管理`, heading `服务已就绪`, environment `v040-ci`, the exact build prefix, and the current capability notice. Browser context credentials exercise HTTP Basic authentication; the native operating-system password dialog is not automated.

TLS: Python verifies the full fixture CA chain and IP SAN before the browser starts. Only the disposable Chromium process gets that server key's SPKI pin so it can render the private-CA page. No system trust store changes or blanket `ignore_https_errors` are used. This proves the page and separately proves TLS, but does not prove a user's installed browser trusts the public server's CA. Public-device trust requires its own authorized verification.

## Evidence

Each run retains `.qa-results/verify-w2f/run-<generated token>/run.json` and `evidence/`:

- `run.json`: source fingerprints, commit, dirty flag, unique image and project identity.
- `build.log`, `commands.log`: actual launch/teardown commands, output and exit codes.
- `doctor.json`: live ownership, ports, volumes, build and authenticated response.
- `actions.json`: ordered HTTP actions and response bodies, auth mode without credentials, followed by the browser action and observed state.
- `dashboard.html`, `dashboard.aria.txt`, `dashboard.png`: actual DOM, accessible page structure and screenshot for the dashboard recipe.
- `result.json`, `cleanup.json`: feature verdict and proof that runtime was removed while artifacts survived.

Proof standards: drive the real route/UI or documented input boundary, capture the action and its result, and verify lasting side effects through an independent read. The cloud page is read-only, so its proof combines rendered build status with the real authenticated status API and runtime ownership; it makes no claim of message writes. Lifecycle proof checks containers and volumes as well as HTTPS. Message proof starts at `sync-page`, then independently reads the Worker's persisted messages and receipts and the local sink's captured requests. Cloud containers use real Postgres and media volumes; the legacy browser and message harnesses substitute their documented external CLI boundaries.

The helper is **not a dry run**: it builds/pulls images, starts local containers, creates temporary secrets/volumes, launches Chromium and makes local HTTPS requests. It has no connector or GitHub publishing command. Keep screenshots and reports local unless publication is explicitly authorized. Do not save Basic-auth headers, credential files, HARs or browser traces. Browser requests are constrained to the fixture origin; unexpected destinations fail the proof. The helper withholds HTTP bodies that disclose fixture secrets.

For legacy QA, omit `--publish`, `--publish-report` and `--dry-run`: the latter still reads GitHub deduplication state. Before relying on any newly introduced dry-run mode, observe its files, network requests and Git refs rather than trusting the name.

## Cleanup

```sh
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py cleanup "$W2F_VERIFY_RUN"
```

Cleanup validates the run's project identity and Compose fingerprint, downs only that project with its volumes, checks for remaining owned containers/volumes, removes only its labelled image, and deletes `runtime/` (including private keys and passwords). `run.json` and `evidence/` survive. Browser teardown closes only the browser launched by the driver. Never kill by process name or use global Docker prune. If ownership verification fails, preserve the run for inspection rather than widening cleanup. Rerunning cleanup on a successfully cleaned run is safe.

After **every failed iteration**, execute cleanup using the reported run directory and inspect its verdict. A failure report alone does not prove the port/processes were removed. The persistent `browsers/` cache is reusable tooling, not fixture state.

## Helpers

`scripts/control.py` is executable; its interpreter is the project virtualenv. Available invocations are `launch`, `doctor RUN`, `drive RUN --feature auth|dashboard`, and `cleanup RUN`, demonstrated above. Its `--help` lists the same interface. It derives the checkout from its own location; it does not embed a developer home path or server credentials.

`scripts/messages.py` has a separate `launch`, `doctor RUN`, `inject RUN --text TEXT`, `collect RUN`, `drive RUN [--text TEXT]`, and `cleanup RUN` interface. `collect` verifies the new client's durable pending queue; `drive --text` injects while the legacy Worker is reading; `inject` queues while stopped. Its [feature recipe](features/synthetic-messages.md) defines replay, evidence and cleanup. Keep each helper's run directory with that helper.

Keep this map aligned with routes and selectors as the app changes using `$maintain-verification-skill`.
