#!/usr/bin/env python3
"""Exercise real containers, TLS, persistence and failed-release rollback."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud.runtime import initialize
from deploy.v040.release import ReleaseManager


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
        manager = ReleaseManager(root, wait_seconds=30)
        bad_revision = "b" * 40 if args.revision != "b" * 40 else "c" * 40
        bad_image = "w2f-v040-cloud:" + bad_revision
        try:
            print("Starting isolated TLS stack", flush=True)
            manager.activate(image)
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
            assert all(item["name"].startswith(project + "_") for item in config["volumes"].values())
            manager.compose("exec", "-T", "db", "psql", "-U", "w2f", "-d", "w2f", "-v", "ON_ERROR_STOP=1",
                            "-c", "CREATE TABLE deployment_probe(value text); INSERT INTO deployment_probe VALUES ('persisted');")
            manager.compose("exec", "-T", "app", "python", "-c",
                            "from pathlib import Path; Path('/var/lib/w2f/deployment-probe').write_text('persisted')")
            manager.compose("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "60", "db", "app")
            manager.verify(args.revision)
            persisted = manager.compose("exec", "-T", "db", "psql", "-U", "w2f", "-d", "w2f", "-Atc",
                                        "SELECT value FROM deployment_probe").stdout.strip()
            assert persisted == b"persisted"
            manager.compose("exec", "-T", "app", "python", "-c",
                            "from pathlib import Path; assert Path('/var/lib/w2f/deployment-probe').read_text() == 'persisted'")
            print("TLS/authentication/isolation/persistence passed; testing failed release", flush=True)
            dockerfile = f'FROM {image}\nENV W2F_RELEASE={bad_revision}\nLABEL org.opencontainers.image.revision={bad_revision}\nCMD ["python", "-c", "raise SystemExit(1)"]\n'
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
            assert history["current"] == image
            assert list((root / "state/backups").glob("*.dump"))
            assert manager.compose("exec", "-T", "db", "psql", "-U", "w2f", "-d", "w2f", "-Atc",
                                   "SELECT value FROM deployment_probe").stdout.strip() == b"persisted"
            print("PASS: failed release rolled back; healthy release and persistent data retained", flush=True)
        except Exception:
            status = manager.compose("ps", "--all").stdout.decode()
            print(status, file=sys.stderr)
            # This stack contains only generated fixtures, never production credentials/data.
            logs = manager.compose("logs", "--tail", "25", "app", "gateway").stdout.decode()
            print(logs, file=sys.stderr)
            raise
        finally:
            manager.compose("down", "--volumes", "--remove-orphans")
            subprocess.run(["docker", "image", "rm", bad_image], capture_output=True)


if __name__ == "__main__":
    main()
