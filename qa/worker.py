"""One isolated test process. Public evidence contains no raw test output."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import time
import socket
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# An explicit supported-platform contract, never a blanket waiver for skips.
PLATFORM_EXCLUSIONS = {
    'test_realtime_wakeup.ReaderWakeupTest.test_rion_watch_wakes_worker_on_new_row': {'win32'},
}


def restrict_network():
    """Catch accidental Python remote calls; not an OS/container security boundary."""
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def validate(address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
            raise PermissionError('QA allows only loopback networking')

    def connect(sock, address):
        validate(address)
        return original(sock, address)

    def connect_ex(sock, address):
        validate(address)
        return original_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


class EvidenceResult(unittest.TestResult):
    def __init__(self, progress_path=None):
        super().__init__()
        self.checks = []
        self.progress_path = progress_path
        self.test_started = None

    def write_progress(self, current=None):
        if self.progress_path is None:
            return
        identifier = current.id() if current is not None else None
        if identifier and not re.fullmatch(r'[A-Za-z0-9_.-]+', identifier):
            identifier = 'unidentified_test'
        value = {'completed': self.testsRun - (1 if current is not None else 0), 'current_test': identifier}
        temporary = self.progress_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value), encoding='utf-8')
        temporary.replace(self.progress_path)

    def startTest(self, test):
        super().startTest(test)
        self.test_started = time.perf_counter()
        self.write_progress(test)

    def stopTest(self, test):
        super().stopTest(test)
        self.write_progress()


    def record(self, test, status, err=None, reason=''):
        frames = []
        if err:
            for frame in traceback.extract_tb(err[2]):
                try:
                    relative = Path(frame.filename).resolve().relative_to(ROOT)
                except ValueError:
                    continue
                frames.append(f'{relative.as_posix()}:{frame.lineno} ({frame.name})')
        # Exception values can contain credentials or real paths. Only expose type
        # and repository-relative locations; never copy assertion text/locals.
        actual = ('Test passed' if status == 'passed' else
                  f'{err[0].__name__} in isolated regression test' if err else reason)
        record = {
            'id': f'unit.{test.id()}', 'title': f'Regression: {test.id()}',
            'status': status, 'expected': 'The regression test passes.',
            'actual': actual, 'evidence': frames[-4:],
            'related_issues': [], 'category': 'bug',
            'test_duration_seconds': round(time.perf_counter() - self.test_started, 3) if self.test_started is not None else None,
        }
        if err:
            # Preserve distinctions between assertion values without disclosing
            # those values. Normalize only the temporary fixture root.
            detail = str(err[1])
            root = os.environ.get('W2F_QA_ROOT')
            if root:
                detail = detail.replace(root, '<fixture-root>')
            record['failure_key'] = hashlib.sha256((err[0].__name__ + ':' + detail).encode()).hexdigest()
        self.checks.append(record)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, 'passed')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, 'failed', err)

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, 'blocked', err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        if sys.platform in PLATFORM_EXCLUSIONS.get(test.id(), set()):
            self.record(test, 'not_applicable', reason='POSIX Rion watch does not apply to Windows; WDA/SSE has separate coverage.')
        else:
            self.record(test, 'blocked', reason='Test skipped; platform or optional dependency needs review.')

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, 'blocked', reason='Known expected failure; not a passing check.')

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, 'blocked', reason='Unexpected success; update expected-failure declaration.')

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            # Do not publish arbitrary subtest parameter reprs.
            self.record(test, 'failed' if issubclass(err[0], AssertionError) else 'blocked', err)


def unit_checks(check=None, progress_path=None):
    loader = unittest.TestLoader()
    if check:
        sys.path.insert(0, str(ROOT / 'tests'))
        suite = loader.loadTestsFromName(check.removeprefix('unit.'))
    else:
        suite = loader.discover(str(ROOT / 'tests'))
    result = EvidenceResult(progress_path)
    suite.run(result)
    # A test with multiple subtest failures is one finding, never multiple issues.
    rows = {}
    for row in result.checks:
        prior = rows.get(row['id'])
        if prior:
            prior['evidence'] = sorted(set(prior['evidence'] + row['evidence']))
            if row.get('failure_key'):
                prior['failure_key'] = hashlib.sha256(
                    (prior.get('failure_key', '') + row['failure_key']).encode()
                ).hexdigest()
            if row['status'] == 'blocked':
                prior['status'] = 'blocked'
                prior['actual'] = row['actual']
        else:
            rows[row['id']] = row
    return list(rows.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--group', choices=('unit', 'probes', 'browser'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check')
    args = parser.parse_args()
    restrict_network()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            if args.group == 'unit':
                rows = unit_checks(args.check, args.output.with_suffix('.progress.json'))
            elif args.group == 'probes':
                from qa.probes import run_checks
                rows = run_checks()
            else:
                from qa.browser_check import run_checks
                rows = run_checks()
        except Exception as exc:
            rows = [{'id': f'{args.group}.environment', 'title': f'{args.group} execution',
                     'status': 'blocked', 'expected': 'Test environment starts successfully.',
                     'actual': f'{type(exc).__name__}; inspect local environment.',
                     'evidence': [], 'related_issues': [], 'category': 'bug'}]
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
