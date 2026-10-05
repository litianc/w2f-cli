"""Durable reader-to-outbox collection. Upload and delivery are separate stages."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid


class CollectionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def integer(value):
    return type(value) is int and 0 <= value <= 9223372036854775807


class Collector:
    def __init__(self, state_dir):
        self.root = Path(state_dir).expanduser()
        if not self.root.is_absolute() or self.root.is_symlink():
            raise CollectionError('state_path', '使用独立的绝对状态目录，不能使用符号链接')
        self.root = self.root.resolve()
        self.path = self.root / 'collector.sqlite3'

    @contextmanager
    def database(self):
        if self.path.is_symlink():
            raise CollectionError('state_path', '状态数据库不能是符号链接')
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            marker = db.execute("SELECT value FROM metadata WHERE key='format'").fetchone()
            if not marker or marker[0] != 'w2f-client-v040-1':
                raise CollectionError('state_format', '这不是受支持的采集客户端状态库')
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, reader, account, page_size=100, max_pending_bytes=64*1024*1024):
        reader = Path(reader).expanduser()
        if not reader.is_absolute() or not reader.is_file() or not account.strip():
            raise CollectionError('configuration', '需要读取器绝对路径和明确的源账号标识')
        if not 1 <= page_size <= 1000 or max_pending_bytes <= 0:
            raise CollectionError('configuration', '分页大小须为 1–1000，待上传容量须大于零')
        # Refuse existing paths, including another client or main's state directory.
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            with db:
                db.executescript('''
                    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE streams(id TEXT PRIMARY KEY, talker TEXT UNIQUE NOT NULL,
                        read_cursor INTEGER NOT NULL, scan_base INTEGER, snapshot_high INTEGER,
                        page_time INTEGER, page_id INTEGER, error_code TEXT, error TEXT);
                    CREATE TABLE outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT UNIQUE NOT NULL, stream_id TEXT NOT NULL,
                        local_id INTEGER NOT NULL, payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL,
                        UNIQUE(stream_id,local_id));
                ''')
                values = dict(format='w2f-client-v040-1', device_id=str(uuid.uuid4()),
                              source_account=account, reader=str(reader.resolve()),
                              page_size=str(page_size), max_pending_bytes=str(max_pending_bytes))
                db.executemany('INSERT INTO metadata VALUES(?,?)', values.items())
        finally:
            db.close()
        return self.status()

    def _page(self, config, talker, base, high=None, anchor=None):
        command = [config['reader'], 'sync-page', '--talker', talker,
                   '--after-local-id', str(base), '--limit', config['page_size']]
        if high is not None:
            command += ['--through-local-id', str(high)]
        if anchor is not None:
            command += ['--page-after-time', str(anchor['time']), '--page-after-local-id', str(anchor['local_id'])]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=60)
            value = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise CollectionError('reader_unavailable', '读取器未能返回有效响应；保留采集位置') from None
        if result.returncode or not isinstance(value, dict) or value.get('ok') is not True:
            raise CollectionError('reader_unavailable', '读取器报告失败；保留采集位置')
        page = value.get('data')
        if (not isinstance(page, dict) or page.get('contract') != 'ordered_sync_v1'
                or not integer(page.get('through_local_id')) or not isinstance(page.get('messages'), list)
                or type(page.get('has_more')) is not bool):
            raise CollectionError('reader_contract', '读取器响应不符合 ordered_sync_v1')
        upper = page['through_local_id']
        if upper < base or (high is not None and upper != high):
            raise CollectionError('source_changed', '源快照倒退或分页上界变化；需要核对源数据库')
        previous = anchor['local_id'] if anchor else base
        if len(page['messages']) > int(config['page_size']):
            raise CollectionError('reader_contract', '读取器返回的消息超过请求页大小')
        for row in page['messages']:
            if (not isinstance(row, dict) or row.get('talker') != talker or not integer(row.get('local_id'))
                    or not previous < row['local_id'] <= upper or not integer(row.get('create_time'))):
                raise CollectionError('reader_contract', '分页源群、消息编号或顺序无效')
            previous = row['local_id']
        if page['has_more']:
            expected = (dict(time=page['messages'][-1]['create_time'], local_id=previous)
                        if page['messages'] else None)
            if not expected or page.get('next') != expected:
                raise CollectionError('reader_contract', '分页 next 未指向本页末尾消息')
        return page

    def enable(self, talker):
        if not talker.strip():
            raise CollectionError('configuration', '需要明确的源群标识')
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM streams WHERE talker=?', (talker,)).fetchone()
            if old:
                return dict(old)
            config = dict(db.execute('SELECT key,value FROM metadata'))
            page = self._page(config, talker, 0)
            stream_id = str(uuid.uuid4())
            db.execute('INSERT INTO streams(id,talker,read_cursor) VALUES(?,?,?)',
                       (stream_id, talker, page['through_local_id']))
            return dict(db.execute('SELECT * FROM streams WHERE id=?', (stream_id,)).fetchone())

    def _collect_page(self, stream_id):
        with self.database() as db:
            # Serialize collectors before invoking the source. Status readers use WAL.
            db.execute('BEGIN IMMEDIATE')
            stream = dict(db.execute('SELECT * FROM streams WHERE id=?', (stream_id,)).fetchone())
            config = dict(db.execute('SELECT key,value FROM metadata'))
            base = stream['scan_base'] if stream['snapshot_high'] is not None else stream['read_cursor']
            anchor = (dict(time=stream['page_time'], local_id=stream['page_id'])
                      if stream['page_id'] is not None else None)
            page = self._page(config, stream['talker'], base, stream['snapshot_high'], anchor)
            staged = []
            for row in page['messages']:
                if (row.get('kind_name') != 'text' or row.get('local_type') != 1
                        or not isinstance(row.get('text'), str)):
                    raise CollectionError('unsupported_message', '当前采集内核仅支持普通文本；保留该页等待后续支持')
                payload = json.dumps(row, ensure_ascii=False, separators=(',', ':'))
                staged.append((str(uuid.uuid4()), stream_id, row['local_id'], payload, len(payload.encode('utf-8'))))
            used = db.execute('SELECT COALESCE(SUM(payload_bytes),0) FROM outbox').fetchone()[0]
            if used + sum(row[4] for row in staged) > int(config['max_pending_bytes']):
                raise CollectionError('queue_full', '本地待上传队列已达到容量上限；该页未入队，采集位置保留')
            db.executemany('INSERT INTO outbox(event_id,stream_id,local_id,payload,payload_bytes) VALUES(?,?,?,?,?)', staged)
            high = page['through_local_id']
            if page['has_more']:
                after = page['next']
                db.execute('UPDATE streams SET read_cursor=?,scan_base=?,snapshot_high=?,page_time=?,page_id=?,error_code=NULL,error=NULL WHERE id=?',
                           (after['local_id'], base, high, after['time'], after['local_id'], stream_id))
            else:
                db.execute('UPDATE streams SET read_cursor=?,scan_base=NULL,snapshot_high=NULL,page_time=NULL,page_id=NULL,error_code=NULL,error=NULL WHERE id=?',
                           (high, stream_id))
            return page['has_more']

    def collect(self):
        with self.database() as db:
            streams = [row[0] for row in db.execute('SELECT id FROM streams ORDER BY rowid')]
        for stream_id in streams:
            try:
                while self._collect_page(stream_id):
                    pass
            except CollectionError as exc:
                with self.database() as db:
                    db.execute('UPDATE streams SET error_code=?,error=? WHERE id=?', (exc.code, str(exc), stream_id))
        return self.status()

    def status(self):
        with self.database() as db:
            config = dict(db.execute('SELECT key,value FROM metadata'))
            count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(payload_bytes),0) FROM outbox').fetchone()
            streams = [dict(row) for row in db.execute('SELECT * FROM streams ORDER BY rowid')]
        return dict(device_id=config['device_id'], source_account=config['source_account'],
                    pending_count=count, pending_bytes=size, max_pending_bytes=int(config['max_pending_bytes']),
                    streams=streams, upload_implemented=False, delivery_implemented=False)

    def pending(self, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 1000:
            raise CollectionError('configuration', '待上传分页参数无效')
        with self.database() as db:
            rows = db.execute('SELECT o.*,s.talker FROM outbox o JOIN streams s ON s.id=o.stream_id WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit)).fetchall()
        events = [dict(sequence=row['sequence'], event_id=row['event_id'], stream_id=row['stream_id'],
                       message=json.loads(row['payload'])) for row in rows]
        return dict(events=events, next_after=events[-1]['sequence'] if events else after)
