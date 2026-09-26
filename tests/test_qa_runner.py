import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qa.run import classify, isolated_env, signature, verify, write_reports
from qa.worker import EvidenceResult


def check(status='failed', actual='assertion failed'):
    return {'id': 'sample.check', 'title': 'Synthetic check', 'status': status,
            'expected': 'expected', 'actual': actual, 'evidence': ['synthetic fixture'],
            'related_issues': [], 'category': 'bug'}


class ClassificationTests(unittest.TestCase):
    def test_only_identical_repeated_failure_is_confirmed(self):
        row = check()
        self.assertEqual(classify([row, dict(row)]), 'confirmed_failure')
        self.assertEqual(classify([row]), 'flaky')
        self.assertEqual(classify([row, check('passed')]), 'flaky')
        self.assertEqual(classify([row, check(actual='different failure')]), 'flaky')
        self.assertEqual(classify([row, check('blocked')]), 'blocked')

    def test_fresh_retry_and_no_retry_for_passes(self):
        passed = {**check('passed'), 'id': 'sample.pass'}
        with patch('qa.run.run_group', side_effect=[[check(), passed], [check(), passed]]) as run:
            rows = verify(['probes'])
        self.assertEqual(run.call_count, 2)
        self.assertEqual([r['status'] for r in rows], ['confirmed_failure', 'passed'])
        self.assertEqual(len(rows[0]['attempts']), 2)
        self.assertEqual(len(rows[1]['attempts']), 1)

    def test_missing_repeat_is_blocked(self):
        with patch('qa.run.run_group', side_effect=[[check()], []]):
            self.assertEqual(verify(['probes'])[0]['status'], 'blocked')

    def test_unknown_selection_does_not_report_success(self):
        with patch('qa.run.run_group', return_value=[check('passed')]):
            rows = verify(['probes'], check='missing.check')
        self.assertEqual(rows[0]['status'], 'blocked')

    def test_isolation_removes_credentials_and_preserves_home(self):
        with patch.dict(os.environ, {'SAMPLE_TOKEN': 'secret-value', 'SAMPLE_API_KEY': 'key-value'}):
            env = isolated_env(Path('/tmp/qa-fixture'))
        self.assertNotIn('SAMPLE_TOKEN', env)
        self.assertNotIn('SAMPLE_API_KEY', env)
        self.assertEqual(env.get('HOME'), os.environ.get('HOME'))
        self.assertEqual(env['W2F_DATA_DIR'], str(Path('/tmp/qa-fixture') / 'data'))

    def test_test_evidence_never_copies_raw_exception_value(self):
        class Failing(unittest.TestCase):
            def runTest(self):
                self.fail('private assertion content')
        result = EvidenceResult()
        Failing().run(result)
        self.assertEqual(result.checks[0]['status'], 'failed')
        self.assertNotIn('private assertion content', json.dumps(result.checks))

    def test_same_assertion_location_different_values_are_not_same_failure(self):
        class Failing(unittest.TestCase):
            value = 1
            def runTest(self):
                self.assertEqual(self.value, 0)
        first = EvidenceResult()
        second = EvidenceResult()
        Failing().run(first)
        test = Failing()
        test.value = 2
        test.run(second)
        self.assertNotEqual(signature(first.checks[0]), signature(second.checks[0]))

    def test_report_outputs_and_junit_skips_flaky(self):
        with tempfile.TemporaryDirectory() as d:
            row = {**check(), 'status': 'flaky'}
            report = {'commit': 'abc', 'platform': 'TestOS', 'dirty': False,
                      'scope': 'synthetic', 'checks': [row]}
            write_reports(report, Path(d))
            self.assertEqual(json.loads((Path(d) / 'report.json').read_text())['checks'][0]['status'], 'flaky')
            self.assertIn('<skipped', (Path(d) / 'junit.xml').read_text())
            self.assertTrue((Path(d) / 'report.md').is_file())


if __name__ == '__main__':
    unittest.main()
