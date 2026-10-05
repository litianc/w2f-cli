# Isolated runtime lifecycle

An operator can start a disposable cloud environment, identify its state, and remove it without taking down another environment or losing verification evidence.

## Sub-features

- `runtime-launch`: a fresh instance gets its own loopback port, credentials, trust and storage.
- `runtime-doctor`: the operator can identify the live build and verify owned resources.
- `runtime-coexist`: two runs have disjoint ports and named volumes.
- `runtime-cleanup`: teardown removes only its own instances, volumes and scratch secrets; evidence survives.

## How to get to it (user POV)

- Operators normally use `python -m cloud.runtime init`, `up`, `status` and `stop` with a dedicated state directory, as described in `docs/v0.4.0-部署与发布.md`.
- For disposable verification, use `control.py launch`, `doctor` and `cleanup`; they wrap the same initializer and Compose configuration with run-specific ownership and evidence.

## Driving it with control.py

Preconditions: Docker is running and the baseline run passes doctor. Do not invoke the persistent environment's default `up` or `stop` for this recipe.

- **Start a second instance.** Set `W2F_VERIFY_SECOND=$(.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py launch)` and arrange cleanup for both paths if any following command fails.
- **Identify both.** Run `.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py doctor "$W2F_VERIFY_RUN"` and the same command with `"$W2F_VERIFY_SECOND"`. Compare their `doctor.json`: different URLs, container IDs and volume names, identical intended source fingerprints.
- **Remove one.** Run `.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py cleanup "$W2F_VERIFY_SECOND"`. Require `runtime_removed: true`, no remaining owned containers/volumes, and retained `doctor.json`/`cleanup.json`.
- **Prove the survivor.** Run doctor on `"$W2F_VERIFY_RUN"` again; it must still pass. Finish its mapped drive, then clean it up too.
- **Persistence and failed-release changes.** Additionally run the repository's `.venv-cloud/bin/python scripts/test_v040_stack.py` and retain stdout/stderr/exit code in a unique evidence file. It already verifies real DB/media persistence across recreation and rejected/failed release recovery in its own temporary containers; that integration result supplements this user/operator path.

## Gotchas

- `cleanup` removes disposable volumes; production `cloud.runtime stop` retains them. Those operations have different intended outcomes.
- The release integration script requires linux/amd64 images and uses fixed failure-test tags. Run that integration script serially; the new control helper's independent instances can run side by side.
- An unavailable daemon or changed ownership metadata must not lead to killing unrelated processes or pruning Docker globally.
