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
    def test_progress_preserves_only_safe_test_identifier_and_count(self):
        from qa.run import timeout_detail
        class Case:
            def id(self): return 'test_fixture.Case.test_slow'
        with tempfile.TemporaryDirectory() as directory:
            progress = Path(directory) / 'progress.json'
            result = EvidenceResult(progress_path=progress)
            test = Case()
            result.startTest(test)
            detail = timeout_detail(progress)
            self.assertIn('test_fixture.Case.test_slow', detail)
            self.assertIn('Completed 0', detail)
            result.stopTest(test)
            self.assertIn('Completed 1', timeout_detail(progress))
            progress.write_text(json.dumps({'completed': 1, 'current_test': 'private value=secret'}))
            self.assertNotIn('secret', timeout_detail(progress))
            progress.write_text('incomplete')
            self.assertEqual(timeout_detail(progress), '')

    def test_only_explicit_platform_exclusion_is_not_applicable(self):
        class Case:
            def __init__(self, name): self.name = name
            def id(self): return self.name
        result = EvidenceResult()
        with patch('qa.worker.sys.platform', 'win32'):
            result.addSkip(Case('test_realtime_wakeup.ReaderWakeupTest.test_rion_watch_wakes_worker_on_new_row'), 'POSIX only')
            result.addSkip(Case('test_unknown.missing_dependency'), 'missing dependency')
        self.assertEqual([row['status'] for row in result.checks], ['not_applicable', 'blocked'])
        self.assertEqual(classify([result.checks[0]]), 'not_applicable')

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
