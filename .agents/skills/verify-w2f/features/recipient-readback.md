# Automatic recipient readback with Lark CLI

Use after an explicitly authorized M3/M4 text delivery. The sender's receipt and its bot API readback establish sending; a separate read with the recipient's user token establishes that user can retrieve the same text in the intended Feishu group. This user API evidence replaces the manual “open the group and confirm the marker” step for text reception. It proves message availability to that user, not desktop rendering, push notifications or that anyone has read it.

## Preconditions

- Finish `live.py deliver` and retain its successful, private `delivery-*.json` with `result: api_verified` and `paused: true`, plus the same state's immutable `run.json`. The helper accepts one to three source events and at most 50 message parts. It derives exact text through the deployed rendering contract; differing or unavailable local rendering source blocks the proof.
- Select an explicit Lark CLI Profile and the intended receiving user's login. Use `--as user` even when the Profile defaults to bot. Do not change the default Profile. Inspect doctor's private `receiver` and compare its identity with the previously selected receiving user before accepting the proof; a verified login alone does not select the intended person. The user must have access to the exact target group and the app/user must have the applicable message-read permissions. For the single-message GET, the API reports `im:message:readonly` or `im:message` as the required alternatives; prefer the read-only permission when reauthorization is needed. Inspect the installed version and actual error before requesting missing scopes. `auth status` alone does not prove message access.
- Lark CLI must be installed and able to reach its credential store and Feishu. A macOS sandbox may block the Keychain: request execution outside that sandbox; keep the credential-store configuration intact. Authenticate only through Lark CLI. The helper takes no token or app secret.
- Keep the existing state and all resulting JSON private under this checkout's ignored `.v040-live/`, with directory mode 0700 and JSON mode 0600. The default fixture's `om_verify_*` IDs are synthetic and cannot be queried on real Feishu.

## Drive

Run from the V0.4.0 checkout, replacing `YOUR_PROFILE` and the delivery path with the selected Profile and exact path printed by `live.py deliver`:

```sh
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/recipient.py \
  --state .v040-live/m3 --profile YOUR_PROFILE --as user doctor
.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/recipient.py \
  --state .v040-live/m3 --profile YOUR_PROFILE --as user verify \
  --evidence .v040-live/m3/delivery-EXACT.json
```

Use the corresponding `.v040-live/m4` state for real WeChat input and each offline-recovery sample. Doctor verifies the selected identity with `lark-cli auth status --json --verify`; it records `message_read_permission: not_tested`. Verify then uses only:

```sh
lark-cli --profile YOUR_PROFILE api GET /open-apis/im/v1/messages/om_EXACT_RECEIPT \
  --as user --format json
```

There is no typed single-message `get` command in the installed CLI. Its batch shortcut also requires private-message/reaction scopes even with `--no-reactions`, and formats the raw message body. The helper therefore uses the documented raw GET, one exact receipt per call, to retain the original text/deleted/sender/group fields and avoid those shortcut prerequisites. `api` help reports generic write risk, but this helper issues only GET requests to the fixed message-read path; it has no write method or arbitrary path input.

Receipts come from that delivery's persisted parts and matching sender readbacks, not keyword search or a supplied list of guessed IDs. The helper requires every expected part, exact message ID, group ID, sending app, text type, unwithdrawn status and full attributed text decoded from `body.content`. It checks the CLI's actual `identity` envelope; it never switches to bot after an error. Missing output fields, inaccessible IDs or changed text cannot pass. No sending, target changes, binding resume, resource download or group-history scan occurs.

`+chat-messages-list --chat-id ... --start ... --end ...` can discover a marked message when no receipt is known; `+messages-search` also exists for user identity. Such discovery must stay within the selected group/time window and account for pagination. A keyword hit alone is insufficient for this receipt-based proof, and a search miss does not establish failed delivery.

## Evidence and acceptance

The helper retains `recipient-doctor-*.json` and `recipient-verify-*.json` in the existing state, recording the requested identity and verified auth receiver (the actual GET envelope identity is checked, not separately saved), receiving user, Profile, CLI version, helper fingerprint, commands, times, source delivery fingerprint, receipt IDs and independent checks. It stores hashes of observed text and selected fields only; raw authentication responses, tokens, CLI stderr and unrelated messages are excluded. Terminal output contains only result, counts and evidence path. Each CLI operation is bounded by `--timeout` (default 30 seconds); its process group is terminated on timeout. There is no persistent process and no extra runtime to tear down; evidence stays for inspection.

- `recipient_verified`: every receipt was fetched as the receiving user and all checks passed. Together with successful sender proof, replay without new jobs/attempts and stopped test delivery, this satisfies the text-reception confirmation for M3/M4. M4 still needs real source collection and its offline recovery evidence.
- `bot_readback_verified`: the explicit diagnostic `--as bot` query passed; this remains bot API proof and does not establish user accessibility.
- `blocked` or `failed`: no recipient acceptance. Missing auth/scope/Keychain/network/source prerequisites are reported separately from successful reads whose fields do not match. A read failure alone does not mean the message was never sent. Fix the prerequisite and rerun the read; use the original receipt instead of sending another message.

The `live.py deliver` response remains `api_verified` with `recipient_verification: pending` until this separate evidence exists. Previously recorded human confirmation stays valid historical evidence. A human check remains useful when evaluating rendering/media/notifications; it is optional for this exact text availability proof.

For helper maintenance, run `.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/test_recipient.py`. These tests use a disposable CLI executable and synthetic responses; they prove the harness contract, not actual Feishu reception. Before declaring the new live path validated, run the actual selected user/Profile against real receipts. Official source references: [CLI mget shortcut scopes](https://github.com/larksuite/cli/blob/v1.0.95/shortcuts/im/im_messages_mget.go), [Feishu get message](https://open.feishu.cn/document/server-docs/im-v1/message/get).
