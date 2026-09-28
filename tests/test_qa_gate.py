import copy
import unittest

from qa.gate import failures


class GateTests(unittest.TestCase):
    def setUp(self):
        self.known = {'example.check': {'issue': 15, 'signature': 'a' * 64}}
        self.row = {'id': 'example.check', 'status': 'confirmed_failure', 'consistent': True,
                    'attempts': [{'status': 'failed', 'signature': 'a' * 64} for _ in range(2)]}

    def test_only_exact_confirmed_failure_is_accepted_without_changing_evidence(self):
        report = {'checks': [self.row, {'id': 'new.check', 'status': 'passed'}]}
        before = copy.deepcopy(report)
        self.assertEqual(failures(report, self.known), [])
        self.assertEqual(report, before)

    def test_new_or_changed_failure_still_blocks(self):
        self.assertTrue(failures({'checks': [self.row]}, {}))
        for attempt in self.row['attempts']:
            attempt['signature'] = 'b' * 64
        self.assertTrue(failures({'checks': [self.row]}, self.known))

    def test_blocked_flaky_and_incomplete_confirmation_cannot_use_baseline(self):
        for status in ('blocked', 'flaky', 'not_applicable'):
            with self.subTest(status=status):
                self.assertTrue(failures({'checks': [{**self.row, 'status': status}]}, self.known))
        self.row['attempts'].pop()
        self.assertTrue(failures({'checks': [self.row]}, self.known))

    def test_fixed_or_removed_checks_require_removing_baseline_entry(self):
        self.row['status'] = 'passed'
        self.assertIn('fixed', failures({'checks': [self.row]}, self.known)[0])
        self.assertIn('missing', failures({'checks': [{'id': 'other', 'status': 'passed'}]}, self.known)[0])

    def test_missing_results_or_invalid_baseline_fail_closed(self):
        for report in ({}, {'checks': []}, {'checks': [self.row, self.row]}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                failures(report, self.known)
        self.known['example.check']['signature'] = '*'
        with self.assertRaises(ValueError):
            failures({'checks': [self.row]}, self.known)
