# Cloud delivery (M2)

Run after changes to cloud sending, Feishu HTTP transport, shared text rendering, delivery migration or sender lifecycle. Use the v0.4.0 checkout and dependencies in the parent skill, with Docker running. No browser, real account or remote server is required.

```sh
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/delivery.py
```

Require `evidence/delivery.json` to report `passed` and cleanup `cleaned`. The command owns its generated run, HTTPS port, image, credentials and volumes, and cleans them in `finally`. On interruption, use `control.py cleanup RUN` with the printed directory and inspect its result.

The driver uses the synthetic ordered reader, actual client processes, verified HTTPS, real Postgres and the product sender. A fifth private container implements the Feishu token and message endpoints. Sender and fixture are on an internal Docker network; only the gateway has an ingress network and a loopback published port. Only synthetic app credentials are accepted in fixture mode. The same product HTTP adapter connects to the official origin in real mode.

Proof covers default-disabled delivery; Chinese/emoji text and persisted multipart ordering; duplicate intake; fresh sender processes and API restart; rate limits; explicit rejection and operator resume; missing responses/receipts, server errors and “being sent”; binding isolation; concurrent claim exclusion; pause during a request and graceful daemon stop; database-session termination and old-worker fencing; expiry of the safe unknown-result retry window; app credential ownership; and the pinned M1 Store migrator/query reading M2-migrated data. M1's `e2e.py` separately verifies client offline collection, lost intake acknowledgments, database restart and seven-day retention.

The test advances time only through the sender's clock input, not by editing receipt/part timestamps. Messages/jobs come from client uploads. Fault injection terminates only the backend holding the generated binding's advisory lock. The fixture persists every HTTP attempt independently of its deduplicated receipts; compare requests and actual attempt counts, not only receipt counts.

Retained evidence: `sending.json`, `faults.json`, `feishu-final.json`, `m1-compatibility.json`, `trace.jsonl`, `delivery.json`, source fingerprints, build/launch logs, client/cloud snapshots and `cleanup.json`. Payloads are synthetic. Runtime secrets and private keys are deleted. This is proof of the simulated Feishu boundary; real reception needs M3's dedicated server, application, test group and message readback.

Operator configuration, pause/recovery semantics and topology upgrade constraints: [cloud delivery guide](../../../../docs/v0.4.0-云端投递操作.md).
