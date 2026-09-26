"""Harness tests: evidence classification and isolation, not expected product bugs."""
import json
import subprocess
import unittest
from unittest.mock import patch

from qa import probes


class FunctionalProbeHarnessTest(unittest.TestCase):
    def test_failure_and_incomplete_execution_are_distinct_and_sanitized(self):
        def failed(sandbox):
            probes.require(False, "Synthetic assertion failed.", ["Synthetic observed state."])

        def incomplete(sandbox):
            raise RuntimeError("unexpected private diagnostic must not escape")

        specs = (probes.Probe("test.failure", "Failure", "Synthetic expected state", failed),
                 probes.Probe("test.incomplete", "Incomplete", "Synthetic expected state", incomplete))
        with patch.object(probes, "PROBES", specs):
            result = probes.run_checks()
        self.assertEqual([r["status"] for r in result], ["failed", "blocked"])
        self.assertEqual(result[0]["evidence"], ["Synthetic observed state."])
        self.assertNotIn("private diagnostic", json.dumps(result))

    def test_unmocked_process_is_blocked_and_temporary_files_are_removed(self):
        roots = []

        def unsafe(sandbox):
            roots.append(sandbox.root)
            subprocess.run(["this-command-must-never-start"], check=True)

        with patch.object(probes, "PROBES", (probes.Probe("test.external", "External", "Blocked", unsafe),)):
            result = probes.run_checks()
        self.assertEqual(result[0]["status"], "blocked")
        self.assertIn("ProbeBlocked", result[0]["actual"])
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())

    def test_each_probe_gets_fresh_database(self):
        def insert(sandbox):
            self.assertEqual(len(sandbox.store.list_people()), 0)
            sandbox.person()
            return "Created synthetic person.", ["Fresh DB verified."]

        specs = tuple(probes.Probe(f"test.fresh-{i}", "Fresh", "Fresh DB", insert) for i in range(2))
        with patch.object(probes, "PROBES", specs):
            result = probes.run_checks()
        self.assertEqual([r["status"] for r in result], ["passed", "passed"])

    def test_swallowed_external_denial_is_still_blocked(self):
        def swallowed(sandbox):
            try:
                subprocess.run(["this-command-must-never-start"])
            except Exception:
                pass
            probes.require(False, "Misleading product failure.", ["Not valid evidence."])

        with patch.object(probes, "PROBES", (probes.Probe("test.swallowed", "Swallowed", "Blocked", swallowed),)):
            result = probes.run_checks()[0]
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("Misleading", result["actual"])

    def test_synthetic_media_probe_uses_production_staging_and_dispatch(self):
        spec = next(p for p in probes.PROBES if p.id == "media.synthetic-dispatch")
        with patch.object(probes, "PROBES", (spec,)):
            result = probes.run_checks()[0]
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["related_issues"], [14])
        self.assertIn("live #14 remains unverified", result["actual"])


if __name__ == "__main__":
    unittest.main()
