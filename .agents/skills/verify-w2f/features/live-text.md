# Real text verification (M3 / M4)

Use only for explicitly authorized real cloud/Feishu testing. M3 uses a synthetic reader; M4 uses the chosen Mac reader and only messages after enablement. Default CI continues to use disposable `delivery.py`; this recipe sends real messages.

## Preconditions

Pin the tested, deployed full commit. Verify the four service topology with the server release command. Confirm the dedicated user/app/group mapping, bot membership and readback permission. Keep configuration, device tokens, server CA and evidence under this checkout's ignored `.v040-live/`, with directory mode 0700 and JSON mode 0600. An operator installs the app secret on the server for that user; the client never receives it. This driver does not extract or install app secrets.

The private config JSON requires `expected_release`, `ssh_host` (user@host), `ssh_port`, `remote_root`, `url` (HTTPS), `user_id` (UUID), `app_id`, `chat_id`, `reader` (absolute executable), `source_account`, and `source_talker`. Use actual confirmed IDs only in that private file. SSH uses existing pinned host trust; HTTPS uses the dedicated server CA copied over SSH, with full certificate verification. Never substitute a main binding/state or change the CLI's default Profile.

## M3

Invoke `.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/live.py` with `--config .v040-live/config.json --state .v040-live/m3` followed by:

1. `prepare --mode synthetic`: establishes the real client, independent device/binding and target; sending remains disabled.
2. `inject --text 'W2F-M3-<unique marker> 中文 😀'`: queues through the synthetic reader and actual client.
3. `deliver`: checks both running roles and public HTTPS, enables only that binding, uploads one to three pending samples, waits up to 45 seconds for persisted delivery, reads each Feishu message back, then pauses the binding even on failure. The process/SSH timeout is bounded per operation; no daemon is installed.
4. `replay --evidence <printed private delivery JSON>`: repeats the original event through HTTPS while paused; asserts cloud jobs, parts and attempts unchanged.
5. Ask the receiving user to confirm the exact marker is visible in the chosen Feishu group. Save that human response and its date privately. `api_verified` alone is not milestone acceptance.

## M4

Use a distinct `.v040-live/m4` state with the same private config, `prepare --mode wechat`. Preparation records the real reader's current source boundary; historical messages are not imported. Only after this succeeds ask the user to send a new marked text in the chosen WeChat test group.

Run `collect`, then `deliver` and `replay` as above. Ask for real Feishu visibility confirmation. For recovery, request one more new marked WeChat message, run `collect --offline`, inspect nonempty pending data and the recorded upload failure, then run `deliver` to resume. Offline injection denies network only in that upload subprocess; it changes no system network settings and does not stop the server. Record this precise fault boundary.

## Evidence and stopping

`run.json` records immutable configuration, mode, source baseline and identities. `status.json`, `offline.json`, `replay.json` and each `delivery-*.json` retain actual local events, versions, cloud parts and readbacks. These are private real-data records and must not enter Git/CI artifacts/issues. Publish only redacted counts/status and source-code versions. Failure records are not successful delivery claims.

Every deliver call must finish with `paused: true`. If SSH fails during final pause, preserve evidence and explicitly pause the exact binding before ending the test. The helper retains the client queue, credentials and evidence for safe resumption; it does not delete cloud records or real group messages. No persistent collector is started. M3/M4 acceptance requires API proof, user confirmation and stopped test delivery.
