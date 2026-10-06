"""Synthetic ordered reader and recording Feishu CLI; no network transport."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

TALKER = 'verify-room@chatroom'
SENDER = 'verify-sender'
NAME = '测试发送者'
PROFILE = 'verify-w2f'
APP = 'cli_verify_synthetic'
CHAT = 'oc_verify_synthetic'


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    with connect(path) as db:
        db.executescript('''
            CREATE TABLE source(local_id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE sink(key TEXT PRIMARY KEY, text TEXT NOT NULL, message_id TEXT UNIQUE NOT NULL);
            CREATE TABLE events(id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                                request TEXT NOT NULL, response TEXT NOT NULL);
        ''')


def inject(path, text, max_bytes=4096):
    if not text.strip() or len(text.encode('utf-8')) > max_bytes:
        raise ValueError(f'Use nonempty plain text of at most {max_bytes} UTF-8 bytes')
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        local_id = db.execute('SELECT COALESCE(MAX(local_id),0)+1 FROM source').fetchone()[0]
        row = dict(talker=TALKER, local_id=local_id, server_id=f'verify-{local_id}',
                   create_time=int(time.time()), sender_wxid=SENDER, sender=NAME,
                   local_type=1, kind_name='text', text=text)
        db.execute('INSERT INTO source VALUES(?,?)', (local_id, json.dumps(row, ensure_ascii=False)))
    return row


def read_page(db, argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['sync-page'])
    parser.add_argument('--talker', required=True)
    parser.add_argument('--after-local-id', type=int, required=True)
    parser.add_argument('--limit', type=int, required=True)
    parser.add_argument('--through-local-id', type=int)
    parser.add_argument('--page-after-time', type=int)
    parser.add_argument('--page-after-local-id', type=int)
    a = parser.parse_args(argv)
    if a.talker != TALKER or a.after_local_id < 0 or not 1 <= a.limit <= 1000:
        raise ValueError('Unsupported synthetic source request')
    maximum = db.execute('SELECT COALESCE(MAX(local_id),0) FROM source').fetchone()[0]
    high = maximum if a.through_local_id is None else a.through_local_id
    if not a.after_local_id <= high <= maximum:
        raise ValueError('Invalid source snapshot range')
    after = a.after_local_id
    if (a.page_after_time is None) != (a.page_after_local_id is None):
        raise ValueError('Both page anchor fields are required')
    if a.page_after_local_id is not None:
        anchor = db.execute('SELECT payload FROM source WHERE local_id=?', (a.page_after_local_id,)).fetchone()
        if (not anchor or json.loads(anchor[0])['create_time'] != a.page_after_time
                or not after < a.page_after_local_id <= high):
            raise ValueError('Invalid page anchor')
        after = a.page_after_local_id
    rows = [json.loads(r[0]) for r in db.execute(
        'SELECT payload FROM source WHERE local_id>? AND local_id<=? ORDER BY local_id LIMIT ?',
        (after, high, a.limit + 1))]
    more = len(rows) > a.limit
    rows = rows[:a.limit]
    return dict(ok=True, data=dict(contract='ordered_sync_v1', messages=rows,
        through_local_id=high, has_more=more,
        next=dict(time=rows[-1]['create_time'], local_id=rows[-1]['local_id']) if more else None))


def send(db, argv):
    if argv == ['profile', 'list']:
        return [dict(name=PROFILE, appId=APP)]
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', choices=[PROFILE], required=True)
    parser.add_argument('domain', choices=['im'])
    parser.add_argument('command', choices=['+messages-send'])
    parser.add_argument('--chat-id', choices=[CHAT], required=True)
    parser.add_argument('--text', required=True)
    parser.add_argument('--idempotency-key', required=True)
    parser.add_argument('--as', dest='identity', choices=['bot'], required=True)
    parser.add_argument('--format', choices=['json'], required=True)
    a = parser.parse_args(argv)
    previous = db.execute('SELECT * FROM sink WHERE key=?', (a.idempotency_key,)).fetchone()
    if previous:
        if previous['text'] != a.text:
            raise ValueError('Idempotency key reused for different text')
        remote = previous['message_id']
    else:
        remote = 'om_verify_' + str(db.execute('SELECT COUNT(*)+1 FROM sink').fetchone()[0])
        db.execute('INSERT INTO sink VALUES(?,?,?)', (a.idempotency_key, a.text, remote))
    return dict(ok=True, data=dict(message_id=remote))


def main(mode):
    if os.environ.get('W2F_QA_GUARD_ACTIVE') != '1':
        raise RuntimeError('Run this fixture through messages.py with the QA guard')
    root = Path(os.environ['W2F_QA_ROOT']).resolve()
    argv = sys.argv[1:]
    failed = False
    with connect(root / 'fixture.sqlite3') as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            result = read_page(db, argv) if mode == 'reader' else send(db, argv)
            kind = 'reader' if mode == 'reader' else ('profile' if argv == ['profile', 'list'] else 'send')
        except (Exception, SystemExit) as exc:
            kind, failed = 'unexpected', True
            result = dict(ok=False, error=dict(type='validation', message=str(exc)))
        db.execute('INSERT INTO events(kind,request,response) VALUES(?,?,?)',
                   (kind, json.dumps(argv, ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr if failed else sys.stdout)
    return int(failed)
