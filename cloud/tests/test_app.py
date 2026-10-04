import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg

from cloud.app import Settings, create_app


class CloudHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings("v040-ci", "a" * 40, Path(self.temp.name),
                                 "admin", "independent-admin-password", "independent-db-password")
        self.client = TestClient(create_app(self.settings))
        self.addCleanup(self.client.close)

    def test_management_requires_correct_credentials_and_never_discloses_secrets(self):
        for path in ("/", "/api/status"):
            for auth in (None, ("admin", "wrong"), ("someone", self.settings.admin_password)):
                response = self.client.get(path, auth=auth)
                self.assertEqual(response.status_code, 401)
                self.assertIn("Basic", response.headers["www-authenticate"])
                self.assertNotIn(self.settings.database_password, response.text)

    def test_health_failure_is_redacted_and_blocks_ready_management(self):
        with patch("cloud.app.psycopg.connect", side_effect=psycopg.OperationalError("secret-dsn")):
            self.assertEqual(self.client.get("/health/live").status_code, 200)
            for path in ("/health/ready", "/api/status", "/"):
                response = self.client.get(path, auth=("admin", self.settings.admin_password))
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("secret-dsn", response.text)

    def test_authenticated_status_identifies_the_deployed_release(self):
        with patch("cloud.app.psycopg.connect"):
            response = self.client.get("/api/status", auth=("admin", self.settings.admin_password))
        self.assertEqual(response.json(), {"status": "ok", "environment": "v040-ci", "release": "a" * 40})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_unwritable_media_prevents_readiness(self):
        Path(self.temp.name).joinpath("not-a-directory").write_text("fixture")
        settings = Settings("v040-ci", "local", Path(self.temp.name) / "not-a-directory",
                            "admin", "independent-admin-password", "independent-db-password")
        with TestClient(create_app(settings)) as client, patch("cloud.app.psycopg.connect"):
            self.assertEqual(client.get("/health/ready").status_code, 503)


if __name__ == "__main__":
    unittest.main()
