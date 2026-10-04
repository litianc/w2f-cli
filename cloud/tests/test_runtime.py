import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from cloud.runtime import certificate_files, initialize


class RuntimeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def initialize(self, name="one"):
        state = self.root / name
        initialize(state, "v040-ci", "w2f-v040-" + name, "192.0.2.40", "127.0.0.1", 13040,
                   "w2f-v040-cloud:test")
        return state

    def test_separate_environments_have_independent_credentials_and_trust(self):
        one, two = self.initialize("one"), self.initialize("two")
        for file in ("secrets/admin_password", "secrets/database_password", "pki/ca.key"):
            self.assertNotEqual((one / file).read_bytes(), (two / file).read_bytes())
        cert = x509.load_pem_x509_certificate((one / "tls/server.crt").read_bytes())
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH,
                      cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
        self.assertIn("192.0.2.40", [str(ip) for ip in cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)])

    def test_reinitialization_preserves_identity_and_credentials(self):
        state = self.initialize()
        before = {name: (state / name).read_bytes() for name in ("pki/ca.crt", "secrets/admin_password")}
        self.initialize()
        for name, value in before.items():
            self.assertEqual((state / name).read_bytes(), value)
        with self.assertRaises(ValueError):
            initialize(state, "v040-local", "w2f-v040-other", "127.0.0.1", "127.0.0.1", 13040,
                       "w2f-v040-cloud:local")

    def test_certificate_renewal_and_reinitialization_preserve_release_configuration(self):
        state = self.initialize()
        env = state / "compose.env"
        contents = env.read_text().replace("w2f-v040-cloud:test", "sha256:" + "a" * 64)
        env.write_text(contents)
        previous_ca = (state / "pki/ca.crt").read_bytes()
        previous_cert = (state / "tls/server.crt").read_bytes()
        certificate_files(state, "192.0.2.40")
        self.initialize()
        self.assertEqual(env.read_text(), contents)
        self.assertEqual((state / "pki/ca.crt").read_bytes(), previous_ca)
        self.assertNotEqual((state / "tls/server.crt").read_bytes(), previous_cert)

    def test_existing_unrelated_state_and_reserved_port_are_rejected(self):
        state = self.root / "existing"
        state.mkdir()
        (state / "main.db").write_text("keep")
        for directory, port in ((state, 13040), (self.root / "reserved", 13000)):
            with self.assertRaises(ValueError):
                initialize(directory, "v040-ci", "w2f-v040-ci", "127.0.0.1", "127.0.0.1", port,
                           "w2f-v040-cloud:test")
        self.assertEqual((state / "main.db").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
