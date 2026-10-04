import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from deploy.v040.release import validate_archive


class ReleaseArchiveTests(unittest.TestCase):
    def test_archive_cannot_import_unrelated_images(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.tar.gz"
            for tag, allowed in (("w2f-v040-cloud:" + "a" * 40, True), ("postgres:16-alpine", False)):
                body = json.dumps([{"RepoTags": [tag], "Config": "config.json"}]).encode()
                config = json.dumps({"os": "linux", "architecture": "amd64",
                                     "config": {"Labels": {"org.opencontainers.image.revision": "a" * 40}}}).encode()
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo("manifest.json")
                    member.size = len(body)
                    archive.addfile(member, io.BytesIO(body))
                    member = tarfile.TarInfo("config.json")
                    member.size = len(config)
                    archive.addfile(member, io.BytesIO(config))
                if allowed:
                    validate_archive(path, "w2f-v040-cloud:" + "a" * 40)
                else:
                    with self.assertRaises(ValueError):
                        validate_archive(path, "w2f-v040-cloud:" + "a" * 40)
