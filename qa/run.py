"""Run isolated regression, business and browser checks; publish confirmed findings."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def signature(check):
    payload = {key: check.get(key) for key in ('id', 'expected', 'actual', 'evidence', 'failure_key')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def classify(attempts):
    states = [row['status'] for row in attempts]
    if states == ['not_applicable']:
        return 'not_applicable'
    if states == ['passed'] or (states and all(s == 'passed' for s in states)):
        return 'passed'
    if 'blocked' in states:
        return 'blocked'
    if len(states) >= 2 and all(s == 'failed' for s in states):
        return 'confirmed_failure' if len({signature(row) for row in attempts}) == 1 else 'flaky'
    return 'flaky'


def isolated_env(root):
    # Keep HOME untouched. Give project state/config explicit temporary roots;
    # omit secret-bearing environment variables from test children.
    env = {key: value for key, value in os.environ.items()
           if not re.search(r'token|secret|password|credential|api_key', key, re.I)}
    env.update({
        'W2F_DATA_DIR': str(root / 'data'), 'W2F_CONFIG_DIR': str(root / 'config'),
        'W2F_MEDIA_DIR': str(root / 'media'), 'CHZ_DATA_DIR': str(root / 'data'),
        'CHZ_CONFIG_DIR': str(root / 'config'), 'CHZ_MEDIA_DIR': str(root / 'media'),
        'WECHAT_READER': str(root / 'disabled-real-reader'),
        'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'W2F_QA_ISOLATED': '1',
    })
    return env


def blocked(group, actual, check=None):
    return {'id': check or f'{group}.environment', 'title': f'{group} execution',
            'status': 'blocked', 'expected': 'Isolated check completes.',
            'actual': actual, 'evidence': [], 'category': 'bug', 'related_issues': []}


def timeout_detail(path):
    """Only export counters and validated test IDs, never test values or output."""
    try:
        progress = json.loads(path.read_text(encoding='utf-8'))
        completed = progress.get('completed')
        current = progress.get('current_test')
        if type(completed) is not int or not 0 <= completed <= 1000000:
            return ''
        if current is not None and (not isinstance(current, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', current)):
            return ''
        return f' Completed {completed} tests; active: {current or "between tests"}.'
    except (OSError, ValueError, AttributeError):
        return ''


def run_group(group, timeout=240, check=None):
    try:
        import psutil  # noqa: F401 - fail before starting a child if cleanup is unavailable
        from qa.isolation import child_env, ProcessTree
    except ImportError:
        return [blocked(group, 'QA process isolation dependency is unavailable; install requirements-qa.txt.', check)]
    with tempfile.TemporaryDirectory(prefix='w2f-qa-') as directory:
        root = Path(directory)
        output = root / 'result.json'
        args = [sys.executable, '-m', 'qa.worker', '--group', group, '--output', str(output)]
        if check and group == 'unit':
            args += ['--check', check]
        started = time.monotonic()
        env = isolated_env(root)
        env.update(child_env(root))
        process = subprocess.Popen(args, cwd=ROOT, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=(os.name != 'nt'))
        with ProcessTree(process.pid, root) as tree:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                tree.cleanup()
                process.wait(timeout=10)
                return [blocked(group, f'Environment exceeded {timeout}s timeout.' + timeout_detail(output.with_suffix('.progress.json')), check)]
        if process.returncode or not output.is_file():
            return [blocked(group, f'Test process exited without results ({process.returncode}).', check)]
        try:
            rows = json.loads(output.read_text(encoding='utf-8'))
            if not isinstance(rows, list) or not rows:
                raise ValueError('empty result')
            for row in rows:
                if not isinstance(row, dict) or row.get('status') not in {'passed', 'failed', 'blocked', 'not_applicable'}:
                    raise ValueError('invalid check')
                if not re.fullmatch(r'[A-Za-z0-9_.-]+', row.get('id', '')):
                    raise ValueError('invalid id')
                row['group'] = group
                row['duration_seconds'] = round(time.monotonic() - started, 3)
            if len({row['id'] for row in rows}) != len(rows):
                raise ValueError('duplicate id')
        except (ValueError, TypeError, KeyError):
            return [blocked(group, 'Test process produced an invalid result schema.', check)]
        return rows


def verify(groups, timeout=240, check=None):
    findings = []
    for group in groups:
        print(f'Running {group} checks...', flush=True)
        first = run_group(group, timeout, check)
        if check:
            first = [row for row in first if row['id'] == check or row['status'] == 'blocked']
        failed = [row for row in first if row['status'] == 'failed']
        repeated = {}
        if failed:
            print(f'Repeating {len(failed)} {group} failure(s) in a fresh process...', flush=True)
            # Unit failures are selected individually, avoiding an entire slow suite.
            if group == 'unit':
                for row in failed:
                    retry = run_group(group, timeout, row['id'])
                    repeated.update({r['id']: r for r in retry})
            else:
                repeated = {row['id']: row for row in run_group(group, timeout)}
        for row in first:
            attempts = [row]
            if row['status'] == 'failed':
                attempts.append(repeated.get(row['id']) or blocked(group, 'Check missing from repeat run.', row['id']))
            final = dict(row)
            final['status'] = classify(attempts)
            final['attempts'] = [{'status': r['status'], 'signature': signature(r)} for r in attempts]
            final['consistent'] = final['status'] == 'confirmed_failure'
            findings.append(final)
    if check and not any(row['id'] == check for row in findings):
        findings.append(blocked('selection', 'Requested check was not found.', check))
    return findings


def metadata():
    def git(*args):
        try:
            return subprocess.check_output(['git', *args], cwd=ROOT, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return 'unknown'
    return {'schema_version': 1, 'commit': git('rev-parse', 'HEAD'),
            'dirty': bool(git('status', '--porcelain')), 'platform': platform.system(),
            'python': platform.python_version(), 'generated_at': datetime.now(timezone.utc).isoformat(),
            'scope': 'isolated synthetic fixtures; no real WeChat/Feishu delivery'}


def source_fingerprint():
    """Pin the code actually under test, including uncommitted QA additions."""
    digest = hashlib.sha256()
    for base in ('agents', 'integrations', 'tests', 'qa', 'scripts', 'config'):
        for path in sorted((ROOT / base).rglob('*')):
            if path.is_file() and path.suffix in {'.py', '.js', '.html', '.css', '.json'} and '__pycache__' not in path.parts:
                if '.local.' in path.name:
                    continue
                digest.update(path.relative_to(ROOT).as_posix().encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def write_reports(report, directory):
    from qa.issues import redact
    directory.mkdir(parents=True, exist_ok=True)
    # Sanitize structured reports as well as public issues.
    clean = json.loads(json.dumps(report, ensure_ascii=False))
    for row in clean['checks']:
        for key in ('title', 'expected', 'actual'):
            if key in row:
                row[key] = redact(row[key], 1200)
        row['evidence'] = [redact(value, 1200) for value in row.get('evidence', [])]
    (directory / 'report.json').write_text(json.dumps(clean, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    counts = Counter(row['status'] for row in clean['checks'])
    lines = ['# W2F functional test report', '',
             f"Commit: `{clean['commit']}` · Platform: {clean['platform']} · Working tree dirty: {clean['dirty']}",
             '', clean['scope'], '', ' / '.join(f'{k}: {v}' for k, v in sorted(counts.items())), '',
             '| Check | Result | Evidence |', '| --- | --- | --- |']
    for row in clean['checks']:
        actual = str(row.get('actual', '')).replace('|', '/').replace('\n', ' ')
        lines.append(f"| `{row['id']}` | {row['status']} | {actual} |")
    lines += ['', '## Issue publishing', '']
    for item in clean.get('publishing', []):
        lines.append(f"- `{item.get('id', '')}`: {item.get('status', '')} {item.get('url', '')}")
    (directory / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    suite = ET.Element('testsuite', name='w2f-functional', tests=str(len(clean['checks'])),
                       failures=str(counts['confirmed_failure']),
                       skipped=str(counts['blocked'] + counts['flaky'] + counts['not_applicable']))
    for row in clean['checks']:
        case = ET.SubElement(suite, 'testcase', name=row['id'], classname=row.get('group', 'qa'))
        if row['status'] == 'confirmed_failure':
            ET.SubElement(case, 'failure', message=row.get('actual', '')).text = '\n'.join(row.get('evidence', []))
        elif row['status'] in {'blocked', 'flaky', 'not_applicable'}:
            ET.SubElement(case, 'skipped', message=row['status']).text = row.get('actual', '')
    ET.ElementTree(suite).write(directory / 'junit.xml', encoding='utf-8', xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--groups', default='unit,probes,browser')
    parser.add_argument('--check', help='Run/reproduce a stable check ID')
    parser.add_argument('--output', type=Path, default=ROOT / '.qa-results' / 'latest')
    parser.add_argument('--timeout', type=int, default=240, help='Per-process timeout in seconds')
    parser.add_argument('--publish', action='store_true', help='Publish confirmed failures to GitHub')
    parser.add_argument('--dry-run', action='store_true', help='Read GitHub dedupe state, without writing issues')
    parser.add_argument('--repo', default='litianc/w2f-cli')
    parser.add_argument('--max-new', type=int, default=3)
    parser.add_argument('--publish-report', type=Path, help='Publish a report already generated by a trusted run')
    args = parser.parse_args()
    groups = args.groups.split(',')
    if any(group not in {'unit', 'probes', 'browser'} for group in groups):
        parser.error('groups must be unit,probes,browser')
    if args.timeout < 1 or args.max_new < 0:
        parser.error('timeout must be positive; max-new must be nonnegative')
    if args.check:
        if args.check.startswith('unit.'):
            groups = ['unit']
        elif args.check.startswith('browser.'):
            groups = ['browser']
        else:
            groups = ['probes']
    if args.publish_report:
        report = json.loads(args.publish_report.read_text(encoding='utf-8'))
    else:
        started = time.monotonic()
        report = {**metadata(), 'source_fingerprint': source_fingerprint()}
        report['checks'] = verify(groups, args.timeout, args.check)
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        if source_fingerprint() != report['source_fingerprint']:
            for row in report['checks']:
                row.update(status='blocked', consistent=False,
                           actual='Source files changed during this run; repeat against a stable checkout.')
    publishing_failed = False
    if args.publish or args.dry_run or args.publish_report:
        from qa.issues import publish
        try:
            report['publishing'] = publish(report, args.repo, args.max_new, args.dry_run)
            publishing_failed = any(item['status'] == 'error' for item in report['publishing'])
        except Exception as exc:
            # Do not echo CLI stderr or credentials to CI logs.
            report['publishing'] = [{'id': 'publisher', 'status': 'error', 'reason': type(exc).__name__}]
            publishing_failed = True
    write_reports(report, args.output)
    counts = Counter(row['status'] for row in report['checks'])
    print(json.dumps({'results': dict(counts), 'publishing': [item for item in report.get('publishing', [])
                                                           if item.get('status') != 'skipped'],
                      'report': str(args.output / 'report.md')}, ensure_ascii=False, indent=2))
    if publishing_failed:
        return 3
    if counts['confirmed_failure']:
        return 1
    if counts['blocked'] or counts['flaky']:
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
