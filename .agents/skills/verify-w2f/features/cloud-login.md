# Cloud login

An administrator supplies HTTP Basic credentials to open cloud management or query deployment status. Anonymous and incorrect credentials cannot view either protected entry point.

## Sub-features

- `cloud-login-challenge`: both `/` and `/api/status` return 401 with a Basic challenge anonymously.
- `cloud-login-reject`: wrong credentials return 401 at both routes.
- `cloud-login-accept`: the fixture administrator receives 200 at both routes over verified HTTPS.

## How to get to it (user POV)

- Open the HTTPS management URL in a browser and enter the administrator credentials.
- Query `/api/status` with the same credentials in an HTTP client.

## Driving it with control.py

Preconditions: launched cloud fixture, passing doctor, `W2F_VERIFY_RUN` set using the launch output.

- **Attempt anonymous access, then a wrong password, then correct credentials.** Run `.venv-cloud/bin/python .agents/skills/verify-w2f/scripts/control.py drive "$W2F_VERIFY_RUN" --feature auth`. The ordered response codes for each route must be 401, 401, 200.
- **Observe the challenge and success.** Read `evidence/actions.json`; the failed responses name Basic authentication, while the successful status response identifies `v040-ci` and the run's commit. Passwords and Authorization headers are absent from evidence.
- **Render the authorized page when login UI changes.** Use the `dashboard` recipe from [Cloud management status](cloud-status.md) as well.

## Gotchas

- This is HTTP Basic authentication, not an HTML login form or session-cookie flow.
- The native browser/OS credential prompt is not automated; HTTP challenge/response and the authorized rendered page are separate proofs.
- Cloud, persistent local and temporary fixtures have different passwords and CAs. Read only the current fixture through the helper.
