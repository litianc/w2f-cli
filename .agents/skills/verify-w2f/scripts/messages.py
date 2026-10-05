#!/usr/bin/env python3
"""Inject synthetic source messages and drive the real isolated legacy Worker."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time

import psutil
import message_fixture as fixture

ROOT = Path(__file__).resolve().parents[4]
BASE = ROOT / '.qa-results/verify-w2f'
sys.path.insert(0, str(ROOT))
from qa.isolation import child_env, ProcessTree


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def append(path, value):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fingerprint():
    paths = [p for directory in ('agents', 'integrations', 'client') for p in (ROOT / directory).glob('*.py')]
    paths += [ROOT / 'qa/isolation.py', Path(__file__), Path(fixture.__file__)]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def owned(path):
    supplied = Path(path).absolute()
    run = supplied.resolve()
    if supplied.is_symlink() or run.parent != BASE.resolve():
        raise ValueError('Use the messages.py launch directory from this checkout')
    meta = json.loads((run / 'run.json').read_text(encoding='utf-8'))
    token = meta['token']
    if (meta.get('kind') != 'synthetic-reader' or meta.get('schema') != 1
            or meta.get('repo') != str(ROOT) or len(token) != 32
            or any(c not in '0123456789abcdef' for c in token) or run.name != 'messages-' + token):
        raise ValueError('Fixture ownership mismatch')
    if any(p.is_symlink() for p in run.rglob('*')):
        raise ValueError('Inspect unexpected symlinks before using or removing this fixture')
    return run, meta


@contextmanager
def locked(run):
    # This helper is intentionally macOS/Linux, as is the existing cloud helper.
    import fcntl
    with (run / '.control.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another command owns this fixture; wait for it to finish') from None
        yield


def environment(run):
    runtime = run / 'runtime'
    overrides = child_env(runtime)
    bootstrap = runtime / 'bootstrap/sitecustomize.py'
    # This text-only fixture needs no network, including local running services.
    deny_network = (
        'import sys\n'
        'def deny_network(event, args):\n'
        "    if event in {'socket.connect','socket.bind','socket.sendto','socket.sendmsg','socket.getaddrinfo'}:\n"
        f"        with open({str(runtime / 'network-denials.log')!r}, 'a') as log: log.write(event + '\\n')\n"
        "        raise PermissionError('Synthetic message verification has no network transport')\n"
        'sys.addaudithook(deny_network)\n'
    )
    bootstrap.write_text(deny_network + bootstrap.read_text(encoding='utf-8'), encoding='utf-8')
    overrides['PATH'] = str(runtime / 'bin') + os.pathsep + str(Path(sys.executable).parent)
    overrides['WECHAT_READER'] = str(runtime / 'bin/reader')
    overrides['PYTHONDONTWRITEBYTECODE'] = '1'
    return {**os.environ, **overrides}


def execute(run, env, command, timeout):
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                            text=True, encoding='utf-8', timeout=timeout)
    append(run / 'evidence/commands.jsonl', dict(command=command, returncode=result.returncode,
                                                stdout=result.stdout, stderr=result.stderr))
    result.check_returncode()
    return result


def product(run, env, operation):
    command = [sys.executable, '-m', 'agents.wechat_feishu_mirror', operation,
               '--config', str(run / 'runtime/config.json')]
    return execute(run, env, command, timeout=15)


def client(run, env, *arguments):
    command = [sys.executable, '-m', 'client', '--state-dir', str(run / 'runtime/client'), *arguments]
    result = execute(run, env, command, timeout=30)
    return json.loads(result.stdout)


def query(path, sql):
    with fixture.connect(path) as db:
        return [dict(row) for row in db.execute(sql)]


def source(run):
    return [json.loads(row['payload']) for row in query(run / 'runtime/fixture.sqlite3',
                                                       'SELECT payload FROM source ORDER BY local_id')]


def snapshot(run):
    state = run / 'runtime/state.sqlite3'
    external = run / 'runtime/fixture.sqlite3'
    return dict(source=source(run),
        messages=query(state, 'SELECT * FROM messages ORDER BY local_id'),
        deliveries=query(state, 'SELECT * FROM deliveries ORDER BY message_id,part'),
        cursor=query(state, 'SELECT * FROM runtime_state'),
        sink=query(external, 'SELECT * FROM sink ORDER BY rowid'),
        events=query(external, 'SELECT * FROM events ORDER BY id'))


def live_registered(run):
    live = []
    for path in (run / 'runtime/.processes').glob('*.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        try:
            process = psutil.Process(record['pid'])
            if process.create_time() == record['created'] and process.status() != psutil.STATUS_ZOMBIE:
                live.append(record)
        except psutil.NoSuchProcess:
            pass
    return live


def inject(run, text):
    row = fixture.inject(run / 'runtime/fixture.sqlite3', text)
    append(run / 'evidence/injections.jsonl', row)
    return row


def cycle(run, live_text=None):
    """Run real CLI resume/run/stop; retain evidence and reap owned children."""
    env = environment(run)
    product(run, env, 'resume')
    expected = len(source(run))
    before = len(query(run / 'runtime/fixture.sqlite3', "SELECT id FROM events WHERE kind='reader'"))
    command = [sys.executable, '-m', 'agents.wechat_feishu_mirror', 'run',
               '--config', str(run / 'runtime/config.json')]
    with (run / 'evidence/worker.log').open('a', encoding='utf-8') as log:
        worker = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        append(run / 'evidence/commands.jsonl', dict(command=command, pid=worker.pid))
        with ProcessTree(worker.pid, run / 'runtime'):
            try:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if worker.poll() is not None:
                        raise RuntimeError('Worker exited early; inspect evidence/worker.log')
                    rows = query(run / 'runtime/state.sqlite3', 'SELECT * FROM runtime_state')
                    reads = query(run / 'runtime/fixture.sqlite3', "SELECT id FROM events WHERE kind='reader'")
                    if (rows and rows[0]['initialized'] and rows[0]['cursor'] == expected
                            and len(reads) > before):
                        if live_text is not None:
                            # Inject only after this Worker has actually read its source.
                            inject(run, live_text)
                            live_text = None
                            expected += 1
                            before = len(reads)
                            continue
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('Worker did not catch up within 25s; inspect evidence/worker.log')
            finally:
                try:
                    product(run, env, 'stop')
                    worker.wait(timeout=10)
                finally:
                    if worker.poll() is None:
                        worker.terminate()
                        try:
                            worker.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            worker.kill()
                            worker.wait(timeout=5)
        if worker.returncode != 0:
            raise RuntimeError(f'Worker exit code {worker.returncode}; inspect evidence/worker.log')
    if live_registered(run):
        raise RuntimeError('Owned fixture processes remain')


def check(run, meta):
    if not (run / 'runtime').is_dir():
        raise ValueError('Fixture was cleaned; launch a new one')
    if meta['source'] != fingerprint():
        raise ValueError('Relevant source changed after launch; create a new fixture')
    if meta['python'] != sys.executable:
        raise ValueError('Use the same Python interpreter as launch')
    for relative, expected in meta['owned_files'].items():
        if hashlib.sha256((run / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Fixture configuration changed: {relative}')
    if live_registered(run):
        raise ValueError('Fixture still has active processes; clean it up first')
    if (run / 'runtime/network-denials.log').exists():
        raise ValueError('Unexpected network operation invalidated this run')
    if query(run / 'runtime/fixture.sqlite3', "SELECT id FROM events WHERE kind='unexpected'"):
        raise ValueError('Unexpected external CLI request invalidated this run')


def launch():
    if os.name != 'posix' or any(c.isspace() for c in sys.executable):
        raise ValueError('This helper requires macOS/Linux and a Python executable path without whitespace')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True, timeout=10).strip()
    if git('branch', '--show-current') in {'main', 'master', ''}:
        raise ValueError('Use the v0.4.0 development branch checkout')
    token = secrets.token_hex(16)
    run = BASE / ('messages-' + token)
    run.mkdir(parents=True, mode=0o700)
    (run / 'evidence').mkdir()
    runtime = run / 'runtime'
    (runtime / 'bin').mkdir(parents=True)
    copied = runtime / 'bin/message_fixture.py'
    shutil.copyfile(Path(fixture.__file__), copied)
    for name, mode in [('reader', 'reader'), ('lark-cli', 'lark')]:
        shim = runtime / 'bin' / name
        shim.write_text(f'#!{sys.executable}\nfrom message_fixture import main\nraise SystemExit(main({mode!r}))\n')
        shim.chmod(0o700)
    fixture.initialize(runtime / 'fixture.sqlite3')
    config = dict(binding_id=1, source_account='verify-' + token,
        wechat_talker=fixture.TALKER, wechat_chat_name='模拟微信群',
        feishu_chat_id=fixture.CHAT, feishu_chat_name='本地模拟飞书接收器',
        lark_profile=fixture.PROFILE, lark_app_id=fixture.APP,
        wechat_reader=str(runtime / 'bin/reader'), sqlite_file=str(runtime / 'state.sqlite3'),
        data_dir=str(runtime / 'data'), media_dir=str(runtime / 'media'),
        state_file=str(runtime / 'state.json'), log_dir=str(runtime / 'logs'),
        sender_map={fixture.SENDER: fixture.NAME}, poll_interval=.2, page_size=2,
        reader_wakeup=False, realtime_wakeup=False)
    save(runtime / 'config.json', config)
    meta = dict(schema=1, kind='synthetic-reader', token=token, repo=str(ROOT),
        python=sys.executable, commit=git('rev-parse', 'HEAD'), dirty=bool(git('status', '--porcelain')),
        source=fingerprint(), owned_files={str(p.relative_to(run)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [runtime / 'config.json', *sorted((runtime / 'bin').iterdir())]})
    save(run / 'run.json', meta)
    try:
        # Establish enable-time on an empty source through the real Worker.
        # Injecting before this boundary would deliberately skip old history.
        with locked(run):
            env = environment(run)
            client(run, env, 'init', '--reader', str(runtime / 'bin/reader'),
                   '--account', 'verify-' + token, '--page-size', '2')
            client(run, env, 'enable', '--talker', fixture.TALKER)
            cycle(run)
            check(run, meta)
            save(run / 'evidence/baseline.json', snapshot(run))
    except BaseException:
        print(f'Incomplete fixture: {run}; run messages.py cleanup on this directory', file=sys.stderr)
        raise
    return str(run)


def doctor(run, meta):
    check(run, meta)
    env = environment(run)
    command = [str(run / 'runtime/bin/reader'), 'sync-page', '--talker', fixture.TALKER,
               '--after-local-id', '0', '--limit', '2']
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    result.check_returncode()
    observed = json.loads(result.stdout)['data']
    rows = source(run)
    if (observed['contract'] != 'ordered_sync_v1' or observed['messages'] != rows[:2]
            or observed['through_local_id'] != len(rows) or observed['has_more'] != (len(rows) > 2)):
        raise AssertionError('Reader did not expose the injected source')
    data = dict(result='passed', source_count=len(rows), contract=observed['contract'],
                worker_alive=False, network='disabled', sink='synthetic-local', cloud='not_implemented')
    save(run / 'evidence/doctor.json', data)
    return data


def drive(run, meta, live_text=None):
    doctor(run, meta)
    if not source(run) and live_text is None:
        raise ValueError('Inject at least one message or use drive --text')
    number = len(list((run / 'evidence').glob('drive-*.json'))) + 1
    report = dict(result='failed', feature='synthetic-text', cloud='not_implemented',
                  real_wechat='not_tested', real_feishu='not_tested')
    try:
        cycle(run, live_text)
        check(run, meta)
        actual = snapshot(run)
        report.update(actual)
        expected = [json.loads(line) for line in (run / 'evidence/injections.jsonl').read_text(encoding='utf-8').splitlines()]
        require(actual['source'] == expected, 'Source diverged from injection evidence')
        require(len(actual['messages']) == len(expected), 'Wrong persisted message count')
        require(len(actual['deliveries']) == len(expected), 'Wrong delivery count')
        require(len(actual['sink']) == len(expected), 'Wrong sink count')
        require(len([e for e in actual['events'] if e['kind'] == 'send']) == len(expected), 'Duplicate send attempt')
        for row, message, delivery, remote in zip(expected, actual['messages'], actual['deliveries'], actual['sink']):
            require(json.loads(message['payload_json']) == row, 'Persisted source mismatch')
            require(message['state'] == delivery['state'] == 'sent', 'Missing success state')
            require(remote['text'] == fixture.NAME + '：' + row['text'], 'Rendered text mismatch')
            require(remote['key'] == delivery['idempotency_key'], 'Receipt key mismatch')
            require(remote['message_id'] == delivery['feishu_message_id'] == message['feishu_message_id'], 'Receipt mismatch')
        require(actual['cursor'][0]['cursor'] == expected[-1]['local_id'], 'Cursor did not advance')
        report['result'] = 'passed'
    except BaseException as exc:
        report['error'] = str(exc)
        try:
            report['observed_on_failure'] = snapshot(run)
        except (OSError, sqlite3.Error):
            pass
        raise
    finally:
        save(run / f'evidence/drive-{number:04d}.json', report)
    return dict(result='passed', source_count=len(expected), evidence=str(run / f'evidence/drive-{number:04d}.json'))


def collect(run, meta):
    """Exercise the new client queue, separate from legacy Feishu delivery."""
    doctor(run, meta)
    expected = source(run)
    if not expected:
        raise ValueError('Inject a message before testing the client queue')
    report = dict(result='failed', feature='client-queue', cloud='not_implemented', feishu='not_exercised')
    number = len(list((run / 'evidence').glob('client-queue-*.json'))) + 1
    try:
        env = environment(run)
        before_sends = query(run / 'runtime/fixture.sqlite3', "SELECT id FROM events WHERE kind='send'")
        status = client(run, env, 'collect')
        first = client(run, env, 'pending', '--limit', '1000')
        client(run, env, 'collect')
        second = client(run, env, 'pending', '--limit', '1000')
        report.update(status=status, pending=first, after_restart=second)
        require([e['message'] for e in first['events']] == expected, 'Client did not persist injected messages')
        require(first == second, 'Client restart changed pending event identities')
        require(status['pending_count'] == len(expected), 'Client pending count mismatch')
        require(status['streams'][0]['read_cursor'] == expected[-1]['local_id'], 'Client cursor mismatch')
        require(query(run / 'runtime/fixture.sqlite3', "SELECT id FROM events WHERE kind='send'") == before_sends,
                'Collection client unexpectedly attempted Feishu delivery')
        require(not live_registered(run), 'Client processes remain')
        check(run, meta)
        report['result'] = 'passed'
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        save(run / f'evidence/client-queue-{number:04d}.json', report)
    return dict(result='passed', pending_count=len(expected),
                evidence=str(run / f'evidence/client-queue-{number:04d}.json'))


def cleanup(run):
    runtime = run / 'runtime'
    if runtime.exists():
        for record in live_registered(run):
            # ProcessTree also checks creation time; never signal by process name.
            process = psutil.Process(record['pid'])
            if process.create_time() == record['created']:
                ProcessTree(process.pid, runtime).cleanup()
        if live_registered(run):
            raise RuntimeError('Owned processes remain; runtime was preserved')
        # Preserve source and captured requests even after an unsuccessful drive.
        if (runtime / 'fixture.sqlite3').exists():
            save(run / 'evidence/final-source.json', source(run))
            save(run / 'evidence/final-events.json', query(runtime / 'fixture.sqlite3', 'SELECT * FROM events ORDER BY id'))
        shutil.rmtree(runtime)
    result = dict(result='passed', runtime_removed=not runtime.exists(), live_processes=[], evidence_retained=True)
    save(run / 'evidence/cleanup.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('launch')
    for name in ('doctor', 'inject', 'drive', 'collect', 'cleanup'):
        command = sub.add_parser(name)
        command.add_argument('run')
        if name in {'inject', 'drive'}:
            command.add_argument('--text', required=name == 'inject',
                                 help='Plain text; drive injects it after the Worker starts reading')
    a = parser.parse_args()
    if a.command == 'launch':
        print(launch())
        return
    run, meta = owned(a.run)
    with locked(run):
        if a.command == 'cleanup':
            result = cleanup(run)
        elif a.command == 'doctor':
            result = doctor(run, meta)
        elif a.command == 'inject':
            check(run, meta)
            result = inject(run, a.text)
        elif a.command == 'collect':
            result = collect(run, meta)
        else:
            result = drive(run, meta, a.text)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
