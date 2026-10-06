#!/usr/bin/env python3
"""Restricted image release with readiness verification and rollback.

The server's root-owned copy is invoked by one restricted SSH key. Infrastructure
and secret changes are separate from application image releases.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error

SERVER_ROOT = Path("/opt/w2f-v040")
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def atomic_write(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_archive(path: Path, image: str) -> str:
    with tarfile.open(path, "r:gz") as archive:
        total = 0
        count = 0
        member = None
        for item in archive:
            total += item.size
            count += 1
            if total > 2 * 1024**3 or count > 30000:
                raise ValueError("Expanded image archive exceeds release limit")
            if item.issym() or item.islnk() or item.name.startswith("/") or ".." in Path(item.name).parts:
                raise ValueError("Unsafe archive entry")
            if item.name == "manifest.json":
                member = item
        if member is None:
            raise ValueError("Missing image manifest")
        if not member.isfile() or member.size > 65536:
            raise ValueError("Invalid image manifest")
        manifest = json.load(archive.extractfile(member))
        if len(manifest) != 1 or manifest[0].get("RepoTags") != [image]:
            raise ValueError("Only the requested v0.4.0 image may be loaded")
        config = archive.getmember(manifest[0]["Config"])
        if not config.isfile() or config.size > 1024 * 1024:
            raise ValueError("Invalid image configuration")
        data = archive.extractfile(config).read()
        metadata = json.loads(data)
        if (metadata.get("os") != "linux" or metadata.get("architecture") != "amd64"
                or metadata.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
                != image.rsplit(":", 1)[1]):
            raise ValueError("Image platform or release identity does not match")
        return "sha256:" + hashlib.sha256(data).hexdigest()


class ReleaseManager:
    def __init__(self, root: Path, wait_seconds: int = 120):
        self.root = root
        self.state = root / "state"
        self.env_file = self.state / "compose.env"
        self.wait_seconds = wait_seconds

    def run(self, command, **kwargs):
        return subprocess.run(command, check=True, capture_output=True, timeout=240, **kwargs)

    def compose(self, *args, **kwargs):
        return self.run(["docker", "compose", "--env-file", str(self.env_file),
                         "-f", str(self.root / "compose.yaml"), *args], **kwargs)

    def config(self):
        return dict(line.split("=", 1) for line in self.env_file.read_text().splitlines() if line)

    def select_image(self, image: str):
        values = self.config()
        values["W2F_IMAGE"] = image
        atomic_write(self.env_file, "".join(f"{key}={value}\n" for key, value in values.items()))

    def verify(self, revision: str, image_id: str | None = None):
        port = self.config()["W2F_PORT"]
        password = (self.state / "secrets/admin_password").read_text().strip()
        auth = base64.b64encode(f"admin:{password}".encode()).decode()
        context = ssl.create_default_context(cafile=str(self.state / "pki/ca.crt"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(f"https://127.0.0.1:{port}/api/status",
                                         headers={"Authorization": "Basic " + auth})
        for attempt in range(10):
            try:
                with opener.open(request, timeout=12) as response:
                    result = json.load(response)
                break
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, ssl.SSLCertVerificationError) or attempt == 9:
                    raise
                if isinstance(exc, urllib.error.HTTPError) and exc.code != 503:
                    raise
                time.sleep(1)
        if result.get("status") != "ok" or result.get("release") != revision:
            raise RuntimeError("Deployed release identity/readiness does not match")
        roles = ('app','sender') if self.supports_sender() else ('app',)
        if 'sender' not in roles and self.compose('ps','-q','sender').stdout.strip():
            raise RuntimeError('Legacy release must not leave a sender running')
        for service in roles:
            container = self.compose("ps", "-q", service).stdout.decode().strip()
            metadata = json.loads(self.run(["docker", "inspect", container]).stdout)[0]
            if ((image_id and metadata['Image'] != image_id)
                    or metadata['State']['Health']['Status'] != 'healthy'
                    or metadata['Config']['Labels'].get('org.opencontainers.image.revision') != revision):
                raise RuntimeError("Running role does not match the verified image/readiness")

    def describe_image(self, image: str):
        metadata = json.loads(self.run(["docker", "image", "inspect", image]).stdout)[0]
        revision = metadata["Config"].get("Labels", {}).get("org.opencontainers.image.revision", "")
        if (metadata.get("Os") != "linux" or metadata.get("Architecture") != "amd64"
                or not re.fullmatch(r"[0-9a-f]{40}", revision)):
            raise ValueError("Image platform or release identity does not match")
        return {"tag": "w2f-v040-cloud:" + revision, "id": metadata["Id"], "revision": revision}

    def backup_database(self):
        running = self.compose("ps", "--status", "running", "-q", "db").stdout.strip()
        if not running:
            raise RuntimeError("Cannot back up the current database")
        backup_dir = self.state / "backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        dump = self.compose("exec", "-T", "db", "pg_dump", "-U", "w2f", "-d", "w2f", "-Fc").stdout
        path = backup_dir / (time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns()}.dump")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(dump)

    def supports_sender(self):
        image = self.config()['W2F_IMAGE']
        metadata = json.loads(self.run(['docker','image','inspect',image]).stdout)[0]
        return metadata['Config'].get('Labels',{}).get('w2f.delivery') == '2'

    def up(self):
        # Drain the old role before replacing any image or database container.
        self.compose("stop", "sender")
        self.compose("up", "-d", "--wait", "--wait-timeout", str(self.wait_seconds),
                     "--scale", "sender=" + ("1" if self.supports_sender() else "0"))

    def activate(self, image: str):
        candidate = self.describe_image(image)
        history_file = self.state / "releases.json"
        history = json.loads(history_file.read_text()) if history_file.exists() else {}
        previous = history.get("current")
        if previous and previous["id"] == candidate["id"]:
            self.verify(candidate["revision"], candidate["id"])
            return
        if previous:
            self.backup_database()
        self.select_image(candidate["id"])
        try:
            self.up()
            self.verify(candidate["revision"], candidate["id"])
            atomic_write(history_file, json.dumps({"current": candidate, "previous": previous}) + "\n")
        except Exception:
            if previous:
                self.select_image(previous["id"])
                self.up()
                self.verify(previous["revision"], previous["id"])
            else:
                # Keep volumes and database for diagnosis; a failed first release is never live.
                self.compose("stop", "gateway", "app", "sender")
            raise RuntimeError("Release failed; restored the last healthy release when available") from None

    def deploy(self, revision: str, source):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("A full commit SHA is required")
        image = "w2f-v040-cloud:" + revision
        if shutil.disk_usage(self.root).free < 3 * 1024**3:
            raise RuntimeError("Less than 3 GiB free; refusing a new release")
        with tempfile.TemporaryDirectory(prefix="incoming-", dir=self.root) as directory:
            archive = Path(directory) / "image.tar.gz"
            total = 0
            with archive.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("Image archive exceeds release limit")
                    output.write(chunk)
            image_id = validate_archive(archive, image)
            try:
                existing = json.loads(self.run(["docker", "image", "inspect", image]).stdout)[0]["Id"]
            except subprocess.CalledProcessError:
                existing = None
            if existing and existing != image_id:
                raise ValueError("Refusing to replace an existing commit with different image content")
            if not existing:
                self.run(["docker", "load", "-i", str(archive)])
        if self.describe_image(image)["id"] != image_id:
            raise ValueError("Loaded image ID does not match archive")
        self.activate(image)

    def rollback(self):
        history = json.loads((self.state / "releases.json").read_text())
        previous = history.get("previous")
        if not previous:
            raise ValueError("There is no previous successful release")
        self.activate(previous["id"])


def main():
    # A forced SSH command passes the original command as one argument; no shell eval.
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    if not re.fullmatch(r"deploy [a-f0-9]{40}|rollback|status", command):
        raise SystemExit("Only deploy <full-sha>, rollback and status are allowed")
    if os.geteuid() != 0:
        raise SystemExit("Run through the dedicated deployment account")
    manager = ReleaseManager(SERVER_ROOT)
    with (SERVER_ROOT / ".release.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another v0.4.0 release is in progress") from None
        try:
            if command.startswith("deploy "):
                manager.deploy(command.split()[1], sys.stdin.buffer)
            elif command == "rollback":
                manager.rollback()
            else:
                current = json.loads((manager.state / "releases.json").read_text())["current"]
                manager.verify(current["revision"], current["id"])
            print("v0.4.0 release operation completed")
        except Exception as exc:
            # Do not export subprocess output or configuration secrets to the SSH client.
            print(f"v0.4.0 release operation failed ({type(exc).__name__})", file=sys.stderr)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
