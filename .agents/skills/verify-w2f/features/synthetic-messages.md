# Synthetic source messages

Mock a new source message at the **reader's `sync-page` boundary**. The test replaces the external reader and Feishu CLI; the production Worker, polling, paging, rendering, SQLite writes, cursor and receipt logic execute normally. This recipe currently covers plain text on macOS/Linux.

## Sub-features

- `synthetic-text-live`: inject a Chinese/emoji text after the real Worker begins reading; observe the resulting source row, persisted message, rendered outbound request and matching receipt.
- `synthetic-text-restart`: start a new Worker over the same state; require no additional send request, even if the recording sink would deduplicate it.
- `synthetic-text-backfill`: queue messages while the Worker is stopped, then read across the fixture's two-row pages and advance the cursor only after processing.
- `client-queue`: the new `python -m client` reads the same source into its own durable pending queue; restart preserves event IDs and collection performs no Feishu send. This proves local collection, not cloud upload.
- `synthetic-isolation`: per-run source/sink and product databases, synthetic IDs and executable shims; all network attempts blocked, real external CLIs rejected by the existing QA guard, owned processes removed and evidence retained.

## How to get to it (user POV)

Ask: “Use verify-w2f to simulate receiving the text ‘测试消息’ and verify downstream processing.” This is a test input, not a WeChat send button. The text appears in the synthetic reader's source database; the Worker discovers it through its normal executable reader interface. The receiver records a simulated Feishu message ID. No actual WeChat conversation or Feishu group receives it.

The source boundary is preferable to writing the Worker's `messages` table, which would skip collection, and to calling `send_row` directly, which would skip polling, paging and cursor behavior. Writing a WeChat database is unnecessary and would not prove WeChat server delivery. Directly seeding a future cloud table would likewise bypass the collector and upload contract.

## Driving it with messages.py

Use Python 3.12 in `.venv-cloud` with the product `requirements.txt` and `psutil>=5.9,<8` installed. The helper uses the standard library plus `psutil`; the Worker loads its normal dependencies. This path does not require Docker, Playwright or account credentials. Run from the development checkout; all mutable state lives beneath a fresh `.qa-results/verify-w2f/messages-<token>/`.

```sh
set -e
W2F_MESSAGE_RUN=$(.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py launch)
trap '.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py cleanup "$W2F_MESSAGE_RUN"' EXIT
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py doctor "$W2F_MESSAGE_RUN"

# Arrive while the actual Worker is polling.
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py drive "$W2F_MESSAGE_RUN" --text '模拟微信消息：你好 😀'

# Exercise the new collection client's independent local queue.
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py collect "$W2F_MESSAGE_RUN"

# A new Worker process reads the same state without another send attempt.
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py drive "$W2F_MESSAGE_RUN"

# Queue three messages while stopped; page_size=2 forces continuation.
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py inject "$W2F_MESSAGE_RUN" --text '离线消息一'
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py inject "$W2F_MESSAGE_RUN" --text '离线消息二'
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py inject "$W2F_MESSAGE_RUN" --text '离线消息三'
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py drive "$W2F_MESSAGE_RUN"
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/messages.py cleanup "$W2F_MESSAGE_RUN"
trap - EXIT
```

`launch` establishes an empty-source enable-time boundary through the actual Worker before returning. `drive` resumes, runs and stops the production CLI inside a guarded process tree. It waits for an actual source scan and the expected cursor, stops its Worker, then independently checks SQLite and the recording sink. `drive --text` performs the injection only after that Worker has read the source. `inject` alone leaves the Worker stopped so backlog can be tested. No background service remains between commands. Each command locks its own fixture; separate fixtures can coexist.

Read these retained files before reporting success:

- `run.json`: source fingerprints, Git identity and fixture configuration hashes.
- `evidence/injections.jsonl`: original submitted source rows; `baseline.json`: initial cursor boundary.
- `evidence/drive-NNNN.json`: verdict, full source rows, persisted messages/delivery parts, cursor, recording sink and reader/send request history. Match source text → rendered text → idempotency key → simulated receipt ID. Restart proof requires unchanged **send attempt count**, not just the sink's unique message count.
- `evidence/client-queue-NNNN.json`: new client status and pending events before/after a second process invocation. Source rows and stable event IDs must match, with no extra Feishu requests. Its state is separate from the legacy Worker's state. Keep this recipe within 1000 synthetic messages per fixture.
- `evidence/commands.jsonl`, `worker.log`: actual CLI commands and processing output.
- `evidence/cleanup.json`: no owned live processes and runtime removed. `final-source.json` and `final-events.json` survive failed runs too.

For helper maintenance, run `.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/test_messages.py`. After a failed launch, use the run directory printed on stderr for cleanup; the shell assignment may not have completed. After any failed drive, inspect its failed evidence and clean that run before starting again.

## Gotchas

- The fixture source and product SQLite are separate. Only the synthetic source receives injection writes; production code creates message/delivery records. New `local_id` values are monotonic and assigned by the fixture. Calling `inject` twice creates two distinct messages; restart proof reuses the same source identity instead.
- Text is limited to 4096 UTF-8 bytes. Rich message XML, media, member events, unknown-sender workflows and reader wake-up hints are not covered by this text recipe; the configured sender has a confirmed test name and the Worker uses periodic polling. Add explicit fixtures and resource files before claiming those branches.
- The real macOS/Windows reader, WeChat login/decryption and actual Feishu permissions/network/appearance are outside this proof. The Python guard prevents accidental I/O in trusted QA code; it is not an OS sandbox against hostile code.
- **Cloud intake, upload acknowledgements, tenant isolation, cloud sending and archive browsing remain unimplemented and untested by this recipe.** The new collector currently implements only the local text queue. When upload lands, extend `collect` to exercise the real intake API and database, then record the outbound Feishu boundary. Add upload-ack/retry and tenant assertions at that time. Do not create fake cloud success evidence now.
- Real WeChat → cloud → Feishu acceptance still needs a newly sent message in an authorized source group and actual target-group evidence. Label every report here `synthetic-text`; its `om_verify_*` receipts are local simulation evidence.
- Relevant source changes or edited fixture configuration require a fresh launch. Cleanup remains available for stale fixtures and only removes their owned runtime; main, the existing local stack and the public server are untouched.
