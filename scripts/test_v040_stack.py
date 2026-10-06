#!/usr/bin/env python3
"""Exercise real containers, TLS, persistence and failed-release rollback."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud.runtime import initialize
from deploy.v040.release import ReleaseManager
from deploy.v040.upgrade import install as install_infrastructure


def command(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="a" * 40)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    image = "w2f-v040-cloud:" + args.revision
    if not args.skip_build:
        command("docker", "build", "--platform", "linux/amd64", "--build-arg", "RELEASE=" + args.revision,
                "-f", str(ROOT / "cloud/Dockerfile"), "-t", image, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="w2f-v040-ci-") as directory:
        root = Path(directory)
        project = "w2f-v040-ci-" + root.name.rsplit("-", 1)[1].replace("_", "x")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        initialize(root / "state", "v040-ci", project, "127.0.0.1", "127.0.0.1", port, image)
        for name in ("compose.yaml", "Caddyfile"):
            shutil.copyfile(ROOT / "deploy/v040" / name, root / name)
        # CI fixtures belong to the runner, not root. With DAC capabilities dropped,
        # the gateway must use their owner; keep production configuration unchanged.
        compose = yaml.safe_load((root / "compose.yaml").read_text())
        compose["services"]["gateway"]["user"] = f"{os.getuid()}:{os.getgid()}"
        (root / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
        for name in ("pki", "tls"):
            assert (root / "state" / name).stat().st_mode & 0o777 == 0o700
        for name in ("pki/ca.key", "tls/server.key"):
            assert (root / "state" / name).stat().st_mode & 0o777 == 0o600
        manager = ReleaseManager(root, wait_seconds=60)
        bad_revision = "b" * 40 if args.revision != "b" * 40 else "c" * 40
        bad_image = "w2f-v040-cloud:" + bad_revision
        next_revision = "d" * 40 if args.revision != "d" * 40 else "e" * 40
        next_image = "w2f-v040-cloud:" + next_revision
        legacy_image = 'w2f-v040-legacy-test:' + uuid.uuid4().hex
        legacy_revision = '7c4c0a29f0f6e26b0ddbd0a1157ec741812a4b62'
        legacy_dir = root / 'legacy-image'
        legacy_dir.mkdir()
        for name in ('app.py','__init__.py'):
            (legacy_dir / name).write_bytes(subprocess.check_output(['git','show',legacy_revision+':cloud/'+name],cwd=ROOT))
        (legacy_dir / 'Dockerfile').write_text(f'FROM {image}\nUSER root\nRUN rm /app/cloud/sender.py\nCOPY *.py /app/cloud/\nUSER 1040:1040\nENV W2F_RELEASE={legacy_revision}\nLABEL org.opencontainers.image.revision={legacy_revision} w2f.delivery=""\nCMD ["python", "-m", "uvicorn", "cloud.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]\n')
        command('docker','build','--platform','linux/amd64','-t',legacy_image,str(legacy_dir))
        try:
            print('Starting actual legacy binary with three roles',flush=True)
            manager.activate(legacy_image)
            manager.verify(legacy_revision)
            assert not manager.compose('ps','-q','sender').stdout.strip()
            candidate=root/'candidate'
            candidate.mkdir()
            shutil.copyfile(root/'compose.yaml',candidate/'compose.yaml')
            shutil.copyfile(ROOT/'deploy/v040/release.py',candidate/'release.py')
            legacy_compose=yaml.safe_load((root/'compose.yaml').read_text())
            del legacy_compose['services']['sender']
            del legacy_compose['secrets']['feishu_apps']
            (root/'compose.yaml').write_text(yaml.safe_dump(legacy_compose,sort_keys=False))
            (root/'release.py').write_bytes(subprocess.check_output(['git','show',legacy_revision+':deploy/v040/release.py'],cwd=ROOT))
            entrypoint=root/'restricted-release'
            shutil.copyfile(root/'release.py',entrypoint)
            entrypoint.chmod(0o755)
            old_entrypoint=entrypoint.read_bytes()
            (root/'.w2f-managed').write_text(project)
            before=(root/'compose.yaml').read_bytes()
            good=(candidate/'compose.yaml').read_text()
            (candidate/'compose.yaml').write_text('services: [invalid]')
            try:
                install_infrastructure(root,candidate,entrypoint)
                raise AssertionError('Invalid infrastructure was accepted')
            except RuntimeError:
                assert (root/'compose.yaml').read_bytes()==before
                assert entrypoint.read_bytes()==old_entrypoint
                assert entrypoint.stat().st_mode & 0o777 == 0o755
            (candidate/'compose.yaml').write_text(good)
            assert install_infrastructure(root,candidate,entrypoint)['installed']
            assert entrypoint.read_bytes()==(candidate/'release.py').read_bytes()
            # Run the installed copy, with only test root/uid substituted; exercise
            # the same strict single-argument command used by the forced SSH key.
            entry_runner="import runpy,sys; from pathlib import Path; from unittest.mock import patch; m=runpy.run_path(sys.argv[1]); m['main'].__globals__['SERVER_ROOT']=Path(sys.argv[2]); sys.argv=['release','status']; p=patch('os.geteuid',return_value=0); p.start(); m['main']()"
            command(sys.executable,'-c',entry_runner,str(entrypoint),str(root))
            assert not manager.compose('ps','-q','sender').stdout.strip()
            print("Starting isolated TLS stack", flush=True)
            manager.activate(image)
            command(sys.executable,'-c',entry_runner,str(entrypoint),str(root))
            ca = ssl.create_default_context(cafile=str(root / "state/pki/ca.crt"))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPSHandler(context=ca))
            url = f"https://127.0.0.1:{port}"
            try:
                opener.open(url + "/", timeout=10)
                raise AssertionError("Anonymous management request succeeded")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            untrusted = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                    urllib.request.HTTPSHandler())
            try:
                untrusted.open(url + "/health/live", timeout=10)
                raise AssertionError("Untrusted certificate succeeded")
            except urllib.error.URLError as exc:
                assert isinstance(exc.reason, ssl.SSLCertVerificationError)
            config = json.loads(manager.compose("config", "--format", "json").stdout)
            assert not config["services"]["db"].get("ports")
            assert not config["services"]["app"].get("ports")
            assert not config["services"]["sender"].get("ports")
            assert set(config["services"]) == {"app", "db", "gateway", "sender"}
            assert all(item["name"].startswith(project + "_") for item in config["volumes"].values())
            manager.compose("exec", "-T", "db", "psql", "-U", "w2f", "-d", "w2f", "-v", "ON_ERROR_STOP=1",
                            "-c", "CREATE TABLE deployment_probe(value text); INSERT INTO deployment_probe VALUES ('persisted');")
            manager.compose("exec", "-T", "app", "python", "-c",
                            "from pathlib import Path; Path('/var/lib/w2f/deployment-probe').write_text('persisted')")
            manager.compose("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "60", "db", "app", "sender")
            manager.verify(args.revision)
            persisted = manager.compose("exec", "-T", "db", "psql", "-U", "w2f", "-d", "w2f", "-Atc",
                                        "SELECT value FROM deployment_probe").stdout.strip()
            assert persisted == b"persisted"
            manager.compose("exec", "-T", "app", "python", "-c",
                            "from pathlib import Path; assert Path('/var/lib/w2f/deployment-probe').read_text() == 'persisted'")
            print('Rolling back across the sender topology boundary',flush=True)
            manager.rollback()
            manager.verify(legacy_revision)
            command(sys.executable,'-c',entry_runner,str(entrypoint),str(root))
            assert not manager.compose('ps','-q','sender').stdout.strip()
            assert manager.compose('exec','-T','db','psql','-U','w2f','-d','w2f','-Atc','SELECT value FROM deployment_probe').stdout.strip()==b'persisted'
            manager.activate(image)
            manager.verify(args.revision)
            print("TLS/authentication/isolation/persistence passed; testing failed release", flush=True)
            original = manager.describe_image(image)
            # Rebuilding the same source SHA must never overwrite a known image.
            duplicate = f'FROM {image}\nLABEL w2f.test.variant=duplicate\n'
            command("docker", "build", "--platform", "linux/amd64", "-t", next_image, "-", input=duplicate.encode())
            command("docker", "tag", next_image, image)
            try:
                duplicate_archive = command("docker", "image", "save", image, capture_output=True).stdout
            finally:
                command("docker", "tag", original["id"], image)
            duplicate_path = root / "duplicate.tar.gz"
            with gzip.open(duplicate_path, "wb") as stream:
                stream.write(duplicate_archive)
            try:
                with duplicate_path.open("rb") as stream:
                    manager.deploy(args.revision, stream)
                raise AssertionError("An existing commit was overwritten")
            except ValueError as exc:
                assert "different image content" in str(exc)
            manager.verify(args.revision, original["id"])
            assert manager.describe_image(image)["id"] == original["id"]
            # A persistence failure after healthy startup must also roll back.
            healthy_next = f'FROM {image}\nENV W2F_RELEASE={next_revision}\nLABEL org.opencontainers.image.revision={next_revision}\n'
            command("docker", "build", "--platform", "linux/amd64", "-t", next_image, "-", input=healthy_next.encode())
            real_replace = os.replace
            history_failures = []
            def fail_history(source, target):
                if Path(target).name == "releases.json":
                    history_failures.append(str(target))
                    raise OSError("Injected metadata persistence failure")
                return real_replace(source, target)
            try:
                with patch("deploy.v040.release.os.replace", side_effect=fail_history):
                    manager.activate(next_image)
                raise AssertionError("Metadata persistence failure was accepted")
            except RuntimeError as exc:
                assert "restored" in str(exc)
            assert len(history_failures) == 1, "Candidate never reached the history write"
            manager.verify(args.revision, original["id"])
            assert json.loads((root / "state/releases.json").read_text())["current"]["id"] == original["id"]
            dockerfile = f'FROM {image}\nENV W2F_RELEASE={bad_revision}\nLABEL org.opencontainers.image.revision={bad_revision}\nUSER root\nRUN rm /app/cloud/sender.py\nUSER 1040:1040\n'
            command("docker", "build", "--platform", "linux/amd64", "-t", bad_image, "-", input=dockerfile.encode())
            saved = command("docker", "image", "save", bad_image, capture_output=True).stdout
            archive = root / "failed-image.tar.gz"
            with gzip.open(archive, "wb") as stream:
                stream.write(saved)
            try:
                with archive.open("rb") as stream:
                    manager.deploy(bad_revision, stream)
                raise AssertionError("Unhealthy release was accepted")
            except RuntimeError as exc:
                assert "restored" in str(exc)
            manager.verify(args.revision)
            history = json.loads((root / "state/releases.json").read_text())
            assert history["current"]["id"] == original["id"]
            assert list((root / "state/backups").glob("*.dump"))
            assert manager.compose("exec", "-T", "db", "psql", "-U", "w2f", "-d", "w2f", "-Atc",
                                   "SELECT value FROM deployment_probe").stdout.strip() == b"persisted"
            print("PASS: failed release rolled back; healthy release and persistent data retained", flush=True)
        except Exception:
            status = manager.compose("ps", "--all").stdout.decode()
            print(status, file=sys.stderr)
            # This stack contains only generated fixtures, never production credentials/data.
            logs = manager.compose("logs", "--tail", "25", "app", "gateway", "sender").stdout.decode()
            print(logs, file=sys.stderr)
            raise
        finally:
            manager.compose("down", "--volumes", "--remove-orphans")
            subprocess.run(["docker", "image", "rm", bad_image], capture_output=True)
            subprocess.run(["docker", "image", "rm", next_image], capture_output=True)
            subprocess.run(["docker", "image", "rm", legacy_image], capture_output=True)


if __name__ == "__main__":
    main()
