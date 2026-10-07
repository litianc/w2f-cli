# Text cloud intake (M1)

Run this path for changes to client upload/receipts, device authorization, cloud text storage or the shared protocol. It covers M1 of the text-loop plan. Cloud sending has its separate M2 recipe; real Feishu reception requires M3.

## Run

Use the v0.4.0 worktree and cloud test dependencies from the parent skill; Docker must be running. No browser, real WeChat reader, Feishu account or remote server is used.

```sh
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/e2e.py
```

The command launches an owned `control.py` stack, drives the checks and cleans it in `finally`. It prints the run directory and final result. Both `evidence/e2e.json` and `cleanup.json` must indicate success. On interruption, use `control.py cleanup RUN` with that exact printed directory; inspect its result. A cleaned run cannot be driven again.

## What the driver proves

- A synthetic `ordered_sync_v1` executable feeds the actual `client` process. Two users have independent device credentials, source streams and bindings. The operator CLI creates these records; no test inserts message/job rows.
- Client and reader Python processes have a socket audit guard allowing only the owned HTTPS port and, during one fault, the temporary HTTPS proxy port. Proxy/driver requests use fixed fixture origins and verified CA/IP SAN; no system trust is changed.
- The fault proxy forwards an upload to the real API and receives its committed receipt, then closes the client connection without returning that receipt. The pending local event survives. Re-upload stores one message and one job, not two.
- Independent PostgreSQL SELECTs and device-authenticated queries match source text, event and binding. Recreating app/database containers preserves these rows; a new client process recognizes the saved acknowledgment.
- Cross-user reads, wrong account/device/auth_version/group/stream writes and changed payloads are rejected. A batch with an invalid tail leaves none of its new rows committed.
- An injected clock at the public uploader boundary proves duplicate acknowledgment does not extend retention, confirmed copies remain before seven days and become eligible at seven days, and pending rows survive pruning.
- When upload or pruning detects a cloud restore generation changed through the operator command, the client quarantines local copies and blocks automatic collection/upload/pruning pending reconciliation. Disable/query operations cover users, devices and bindings.

`ingest.json` keeps synthetic inputs, message/job snapshots, queries, rejected requests and retention results. Credentials stay in private runtime files and are removed at teardown. `run.json` records dirty state, source fingerprints and image identity, so an uncommitted build is identifiable. Reports intentionally call delivery `pending`; the sender stays idle because these bindings are not enabled for delivery.

## Product operation

For real device setup and endpoint/credential semantics, read [the operator guide](../../../../docs/v0.4.0-端云接入操作.md). Use real operation only within the user's authorized environment; this default verification command always allocates a disposable local stack.
