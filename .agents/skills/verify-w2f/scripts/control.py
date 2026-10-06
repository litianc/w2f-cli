#!/usr/bin/env python3
"""Drive a disposable W2F cloud instance; keep evidence after teardown."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / ".qa-results/verify-w2f"
sys.path.insert(0, str(ROOT))
from cloud.runtime import initialize


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def output(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True, timeout=60).strip()


def fingerprint():
    names = ("cloud/app.py", "cloud/__init__.py", "cloud/Dockerfile", "cloud/runtime.py",
             "cloud/constraints.txt", "requirements-cloud.txt", "deploy/v040/compose.yaml",
             "deploy/v040/Caddyfile", ".agents/skills/verify-w2f/scripts/control.py")
    names = sorted(set(names) | {str(p.relative_to(ROOT)) for folder in ('cloud', 'client', 'w2f_protocol') for p in (ROOT / folder).glob('*.py')} | {'.dockerignore', 'qa/isolation.py', '.agents/skills/verify-w2f/scripts/e2e.py', '.agents/skills/verify-w2f/scripts/delivery.py', '.agents/skills/verify-w2f/scripts/feishu_fixture.py', '.agents/skills/verify-w2f/scripts/message_fixture.py'})
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def owned(path):
    run = Path(path).resolve()
    if run.parent != BASE.resolve() or run.is_symlink():
        raise ValueError("Use the run directory returned by this checkout's launch")
    data = json.loads((run / "run.json").read_text())
    token = data["token"]
    if (data["schema"] != 1 or data["repo"] != str(ROOT)
            or len(token) != 40 or any(c not in "0123456789abcdef" for c in token)
            or run.name != "run-" + token
            or data["project"] != "w2f-v040-verify-" + token
            or data["image"] != "w2f-v040-cloud:" + token):
        raise ValueError("Run ownership does not match")
    return run, data


def compose(run, data, *args):
    runtime = run / "runtime"
    config = runtime / "compose.yaml"
    if hashlib.sha256(config.read_bytes()).hexdigest() != data["compose_sha256"]:
        raise ValueError("Verification Compose file changed; inspect before teardown")
    with (run / "evidence/commands.log").open("a") as log:
        command = ["docker", "compose", "--project-name", data["project"],
                   "--env-file", str(runtime / "state/compose.env"), "-f", str(config), *args]
        log.write("$ " + " ".join(command) + "\n")
        log.flush()
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=log, timeout=240)
        log.write(f"exit_code={result.returncode}\n")
        result.check_returncode()


def launch(delivery=False):
    output("docker", "info", "--format", "{{.OSType}}")
    BASE.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(20)
    run = BASE / ("run-" + token)
    run.mkdir(mode=0o700)
    (run / "evidence").mkdir()
    runtime = run / "runtime"
    runtime.mkdir(mode=0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    data = {"schema": 1, "token": token, "repo": str(ROOT),
            "project": "w2f-v040-verify-" + token, "image": "w2f-v040-cloud:" + token,
            "release": output("git", "rev-parse", "HEAD"), "source": fingerprint(),
            "git_dirty": bool(output("git", "status", "--porcelain")),
            "url": f"https://127.0.0.1:{port}", "created_at": datetime.now(timezone.utc).isoformat()}
    data["delivery_fixture"] = delivery
    save(run / "run.json", data)
    try:
        initialize(runtime / "state", "v040-ci", data["project"], "127.0.0.1",
                   "127.0.0.1", port, data["image"])
        config = yaml.safe_load((ROOT / "deploy/v040/compose.yaml").read_text())
        # Same ownership rule as scripts/test_v040_stack.py on native Linux runners.
        config["services"]["gateway"]["user"] = f"{os.getuid()}:{os.getgid()}"
        if delivery:
            mock = runtime / "feishu"
            mock.mkdir(mode=0o700)
            shutil.copyfile(Path(__file__).with_name('feishu_fixture.py'), mock / 'server.py')
            save(mock / 'control.json', dict(apps={},modes={}))
            config['networks']={'default':{'internal':True},'ingress':{}}
            config['services']['gateway']['networks']=['default','ingress']
            config['services']['feishu-mock']={
                'image':data['image'],'command':['python','/fixture/server.py'],
                'user':f'{os.getuid()}:{os.getgid()}', 'volumes':[str(mock)+':/fixture'],
                'read_only':True,'tmpfs':['/tmp'],'cap_drop':['ALL'],
                'mem_limit':'128m','cpus':0.5,'pids_limit':100}
            config['services']['sender']['environment']['W2F_FEISHU_BASE_URL']='http://feishu-mock:8080'
        data['expected_services']=sorted(config['services'])
        (runtime / "compose.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        shutil.copyfile(ROOT / "deploy/v040/Caddyfile", runtime / "Caddyfile")
        data["compose_sha256"] = hashlib.sha256((runtime / "compose.yaml").read_bytes()).hexdigest()
        data["caddy_sha256"] = hashlib.sha256((runtime / "Caddyfile").read_bytes()).hexdigest()
        save(run / "run.json", data)
        with (run / "evidence/build.log").open("w") as log:
            command = ["docker", "build", "--build-arg", "RELEASE=" + data["release"],
                       "--label", "w2f.verification=" + token, "-t", data["image"],
                       "-f", str(ROOT / "cloud/Dockerfile"), str(ROOT)]
            log.write("$ " + " ".join(command) + "\n")
            log.flush()
            result = subprocess.run(command, stdout=log, stderr=log, timeout=600)
            log.write(f"exit_code={result.returncode}\n")
            result.check_returncode()
        data["image_id"] = output("docker", "image", "inspect", "--format", "{{.Id}}", data["image"])
        save(run / "run.json", data)
        compose(run, data, "up", "-d", "--wait", "--wait-timeout", "120")
        doctor(run)
    except BaseException:
        print(f"Launch failed; evidence: {run / 'evidence'}", file=sys.stderr)
        cleanup(run)
        raise
    return str(run)


def request(run, data, route, mode="anonymous"):
    state = run / "runtime/state"
    context = ssl.create_default_context(cafile=str(state / "pki/ca.crt"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=context))
    headers = {}
    if mode != "anonymous":
        password = (state / "secrets/admin_password").read_text().strip() if mode == "admin" else "incorrect"
        headers["Authorization"] = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
    try:
        response = opener.open(urllib.request.Request(data["url"] + route, headers=headers), timeout=12)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read().decode()
        for name in ("admin_password", "database_password"):
            if (state / "secrets" / name).read_text().strip() in body:
                raise AssertionError("Response disclosed a fixture secret; body withheld")
        return {"action": "GET " + route, "auth": mode, "status": response.code,
                "challenge": response.headers.get("WWW-Authenticate"), "body": body}


def doctor(path):
    run, data = owned(path)
    if fingerprint() != data["source"]:
        raise ValueError("App or deployment source changed since launch; create a fresh run")
    for relative, expected in (("compose.yaml", data["compose_sha256"]), ("Caddyfile", data.get("caddy_sha256"))):
        if hashlib.sha256((run / "runtime" / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Fixture deployment configuration changed; create a fresh run")
    ids = output("docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + data["project"]).split()
    expected = set(data.get("expected_services", ["app","db","gateway","sender"]))
    assert len(ids) == len(expected), "Unexpected owned running services"
    items = json.loads(output("docker", "inspect", *ids))
    services = {}
    for item in items:
        service = item["Config"]["Labels"]["com.docker.compose.service"]
        ports = item["HostConfig"].get("PortBindings")
        if service in {"app", "sender", "feishu-mock"}:
            assert item["Image"] == data["image_id"], "Unexpected app image"
        if service in {"app", "db", "sender", "feishu-mock"}:
            assert not ports, "App/database ports must stay private"
            if service != "feishu-mock":
                assert item["State"]["Health"]["Status"] == "healthy"
        if service == "gateway":
            assert ports == {"3040/tcp": [{"HostIp": "127.0.0.1", "HostPort": data["url"].rsplit(":", 1)[1]}]}
        volumes = [m["Name"] for m in item["Mounts"] if m["Type"] == "volume"]
        assert all(name.startswith(data["project"] + "_") for name in volumes)
        services[service] = {"container_id": item["Id"], "ports": ports, "volumes": volumes}
    assert set(services) == expected
    result = request(run, data, "/api/status", "admin")
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"status": "ok", "environment": "v040-ci", "release": data["release"]}
    report = {"result": "passed", "url": data["url"], "release": data["release"],
              "source": data["source"], "services": services, "status_response": result}
    save(run / "evidence/doctor.json", report)
    return report


def drive(path, feature):
    run, data = owned(path)
    doctor(run)
    actions = []
    try:
        for route in ("/health/live", "/health/ready"):
            result = request(run, data, route)
            actions.append(result)
            assert result["status"] == 200 and json.loads(result["body"]) == {"status": "ok"}
        for route in ("/", "/api/status"):
            for auth, expected in (("anonymous", 401), ("wrong", 401), ("admin", 200)):
                result = request(run, data, route, auth)
                actions.append(result)
                assert result["status"] == expected, result
                if expected == 401:
                    assert result["challenge"].startswith("Basic")
        if feature == "dashboard":
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            from playwright.sync_api import sync_playwright
            state = run / "runtime/state"
            cert = x509.load_pem_x509_certificate((state / "tls/server.crt").read_bytes())
            spki = cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
            pin = base64.b64encode(hashlib.sha256(spki).digest()).decode()
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE / "browsers"))
            os.environ["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=["--ignore-certificate-errors-spki-list=" + pin])
                try:
                    context = browser.new_context(http_credentials={
                        "username": "admin", "password": (state / "secrets/admin_password").read_text().strip()})
                    unexpected = []
                    def local_only(route):
                        if route.request.url.startswith(data["url"] + "/"):
                            route.continue_()
                        else:
                            unexpected.append(route.request.url)
                            route.abort()
                    context.route("**/*", local_only)
                    page = context.new_page()
                    response = page.goto(data["url"] + "/", wait_until="networkidle")
                    assert response.status == 200
                    assert page.title() == "W2F 云端管理"
                    page.get_by_role("heading", name="云端管理", exact=True).wait_for()
                    page.get_by_role("heading", name="服务已就绪", exact=True).wait_for()
                    text = page.locator("main").inner_text()
                    assert "环境：v040-ci" in text and data["release"][:12] in text
                    assert "文本采集接入已开放" in text
                    assert not unexpected, "Unexpected browser network destination"
                    for name in ("admin_password", "database_password"):
                        assert (state / "secrets" / name).read_text().strip() not in text
                    (run / "evidence/dashboard.html").write_text(page.content())
                    (run / "evidence/dashboard.aria.txt").write_text(page.locator("main").aria_snapshot())
                    page.screenshot(path=str(run / "evidence/dashboard.png"), full_page=True)
                    actions.append({"action": "browser: open / with administrator Basic authentication",
                                    "url": page.url, "main_text": text, "unexpected_requests": unexpected,
                                    "browser_trust": "ephemeral server SPKI pin; HTTP doctor verifies full CA chain"})
                finally:
                    browser.close()
        save(run / "evidence/result.json", {"feature": feature, "result": "passed"})
    except BaseException:
        save(run / "evidence/result.json", {"feature": feature, "result": "failed"})
        raise
    finally:
        save(run / "evidence/actions.json", actions)
    return {"result": "passed", "feature": feature, "evidence": str(run / "evidence")}


def live_processes(run):
    import psutil
    live = []
    for path in (run / 'runtime/.processes').glob('*.json'):
        record = json.loads(path.read_text())
        try:
            process = psutil.Process(record['pid'])
            if process.create_time() == record['created'] and process.is_running():
                live.append(record)
        except psutil.NoSuchProcess:
            pass
    return live


def cleanup(path):
    run, data = owned(path)
    runtime = run / "runtime"
    if runtime.exists():
        from qa.isolation import ProcessTree
        # The tracker excludes its own PID; seed ownership only from registered
        # (PID, creation time) pairs, never re-adopt a possibly reused numeric PID.
        ProcessTree(os.getpid(), runtime).cleanup()
        assert not live_processes(run), 'Owned processes remain; preserve runtime'
        if "compose_sha256" in data:
            compose(run, data, "down", "--volumes", "--remove-orphans")
        for resource in ("ps", "volume"):
            command = ["docker", resource] + (["ls"] if resource == "volume" else ["-a"])
            assert not output(*command, "-q", "--filter", "label=com.docker.compose.project=" + data["project"])
        image = subprocess.run(["docker", "image", "inspect", data["image"]], capture_output=True, text=True)
        if image.returncode == 0:
            metadata = json.loads(image.stdout)[0]
            assert metadata["Config"]["Labels"].get("w2f.verification") == data["token"]
            output("docker", "image", "rm", data["image"])
        shutil.rmtree(runtime)
    evidence = sorted(p.name for p in (run / "evidence").iterdir())
    result = {"result": "cleaned", "project": data["project"], "runtime_removed": not runtime.exists(),
              "retained_evidence": evidence, "live_processes": live_processes(run)}
    save(run / "evidence/cleanup.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["launch", "doctor", "drive", "cleanup"])
    parser.add_argument("run", nargs="?")
    parser.add_argument("--feature", choices=["auth", "dashboard"], default="dashboard")
    args = parser.parse_args()
    if args.command != "launch" and not args.run:
        parser.error("Pass the directory returned by launch")
    if args.command == "launch":
        print(launch())
    elif args.command == "doctor":
        print(json.dumps(doctor(args.run), ensure_ascii=False))
    elif args.command == "drive":
        print(json.dumps(drive(args.run, args.feature), ensure_ascii=False))
    else:
        print(json.dumps(cleanup(args.run), ensure_ascii=False))


if __name__ == "__main__":
    main()
