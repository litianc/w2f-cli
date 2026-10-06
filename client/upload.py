"""Bounded HTTPS upload and durable per-event receipts; no delivery side effects."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from client.collector import CollectionError, outbox_event
from w2f_protocol.ingest import VERSION, MAX_BATCH, MAX_BODY, ProtocolError, digest, encode, identifier, validate_batch

RETENTION_SECONDS = 7 * 24 * 60 * 60


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_json(path):
    path = Path(path)
    if path.is_symlink() or (os.name != 'nt' and path.stat().st_mode & 0o077):
        raise CollectionError('credentials_permissions', '凭据文件必须仅当前用户可读写')
    return json.loads(path.read_text(encoding='utf-8'))


def private_write(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


class Uploader:
    def __init__(self, collector, clock=time.time):
        self.collector, self.clock = collector, clock

    def connect(self, credentials_file, url, ca_file):
        try:
            credentials = private_json(credentials_file)
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                raise ValueError()
            if (set(credentials) != {'protocol','device_id','source_account','auth_version','token'}
                    or credentials['protocol'] != VERSION or type(credentials['auth_version']) is not int
                    or credentials['auth_version'] < 1 or not isinstance(credentials['token'], str)
                    or not 32 <= len(credentials['token']) <= 128):
                raise ValueError()
            with self.collector.database() as db:
                config = dict(db.execute('SELECT key,value FROM metadata'))
            if credentials['device_id'] != config['device_id'] or credentials['source_account'] != config['source_account']:
                raise CollectionError('credentials_mismatch', '凭据与当前采集设备或源账号不匹配')
            ca = Path(ca_file).read_bytes()
            ssl.create_default_context(cadata=ca.decode('ascii'))
            ca_name = 'cloud-ca-' + hashlib.sha256(ca).hexdigest() + '.pem'
            ca_path = self.collector.root / ca_name
            if not ca_path.exists():
                private_write(ca_path, ca)
            configuration = dict(credentials=credentials, url=url.rstrip('/'), ca=ca_name)
            tmp = self.collector.root / ('cloud-' + os.urandom(12).hex() + '.tmp')
            private_write(tmp, encode(configuration))
            os.replace(tmp, self.collector.root / 'cloud.json')
        except (ValueError, TypeError, KeyError, ssl.SSLError, UnicodeError):
            raise CollectionError('cloud_configuration', '云端配置无效；需要 HTTPS、独立设备凭据和可信 CA') from None
        return dict(configured=True, device_id=config['device_id'])

    def _config(self):
        try:
            return private_json(self.collector.root / 'cloud.json')
        except (OSError, ValueError):
            raise CollectionError('cloud_configuration', '先使用 connect 配置云端地址、设备凭据与可信 CA') from None

    def _request(self, config, path, body=None):
        try:
            context = ssl.create_default_context(cafile=str(self.collector.root / config['ca']))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                 urllib.request.HTTPSHandler(context=context))
            headers = {'Authorization': 'Bearer ' + config['credentials']['token'], 'Content-Type':'application/json'}
            request = urllib.request.Request(config['url'] + path, data=encode(body) if body is not None else None, headers=headers)
            with opener.open(request, timeout=15) as response:
                raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise ValueError()
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            code = 'cloud_retryable' if exc.code in {408,425,429} or exc.code >= 500 else 'cloud_rejected'
            # No remote error text, URL or credentials enter local reports.
            with exc:
                raw = exc.read(4096)
            try:
                remote = json.loads(raw).get('error')
                if remote in {'authorization_mismatch','authorization_revoked','binding_forbidden','identity_conflict','source_order_conflict'}:
                    code = remote
            except (ValueError, AttributeError):
                pass
            raise CollectionError(code, '上传未得到完整确认；保留本地数据') from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise CollectionError('cloud_retryable', '云端连接或响应不可用；保留本地数据等待重试') from None

    def _generation(self, generation):
        try:
            identifier(generation)
        except ProtocolError:
            raise CollectionError('invalid_receipt', '云端恢复代次无效；保留本地数据') from None
        changed = False
        with self.collector.database() as db:
            db.execute('BEGIN IMMEDIATE')
            config = dict(db.execute('SELECT key,value FROM metadata'))
            changed = bool(config.get('cloud_generation') and config['cloud_generation'] != generation)
            if changed:
                db.execute("INSERT OR REPLACE INTO metadata VALUES('recovery_required','1')")
                db.execute('UPDATE outbox SET quarantined=1')
            elif not config.get('cloud_generation'):
                db.execute("INSERT INTO metadata VALUES('cloud_generation',?)", (generation,))
            required = changed or bool(config.get('recovery_required'))
        if required:
            raise CollectionError('recovery_required', '云端恢复代次变化；保留全部本地副本，等待管理员核对')

    def _session(self, config):
        session = self._request(config, '/api/v1/session')
        expected = {k:v for k,v in config['credentials'].items() if k != 'token'}
        if not isinstance(session, dict) or any(session.get(k) != v for k,v in expected.items()):
            raise CollectionError('authorization_mismatch', '设备授权发生变化；保留本地数据等待核对')
        self._generation(session.get('generation'))

    def record_receipt(self, events, response):
        """Validate the whole exact receipt set before atomically acknowledging any event."""
        try:
            if not isinstance(response, dict) or response.get('protocol') != VERSION:
                raise ValueError()
            receipts = response['receipts']
            if not isinstance(receipts, list) or len(receipts) != len(events):
                raise ValueError()
            expected = {e['event_id']:digest(e) for e in events}
            actual = {}
            for receipt in receipts:
                if not isinstance(receipt, dict) or receipt['state'] != 'stored' or receipt['event_id'] in actual:
                    raise ValueError()
                identifier(receipt['binding_id'])
                actual[receipt['event_id']] = receipt['payload_hash']
            if actual != expected:
                raise ValueError()
            self._generation(response['generation'])
        except (KeyError, ValueError, TypeError):
            raise CollectionError('invalid_receipt', '确认内容不完整或与待上传事件不一致；保留本地数据') from None
        with self.collector.database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT value FROM metadata WHERE key='recovery_required'").fetchone():
                raise CollectionError('recovery_required', '本地数据等待核对')
            for event in events:
                row = db.execute('SELECT * FROM outbox WHERE event_id=?', (event['event_id'],)).fetchone()
                if not row or row['quarantined'] or digest(outbox_event(row)) != expected[event['event_id']]:
                    raise CollectionError('invalid_receipt', '本地事件已变化；未保存此次确认')
            now = self.clock()
            for event in events:
                receipt = next(r for r in receipts if r['event_id'] == event['event_id'])
                db.execute('UPDATE outbox SET acknowledged_at=COALESCE(acknowledged_at,?),receipt=? WHERE event_id=?',
                           (now, encode(dict(protocol=VERSION,generation=response['generation'],receipt=receipt)).decode(), event['event_id']))

    def upload(self):
        config = self._config()
        self._session(config)
        count, errors = 0, []
        with self.collector.database() as db:
            streams = [r[0] for r in db.execute('SELECT id FROM streams ORDER BY rowid')]
        for stream in streams:
            try:
                with self.collector.database() as db:
                    rows = db.execute('SELECT * FROM outbox WHERE stream_id=? AND acknowledged_at IS NULL AND quarantined=0 ORDER BY sequence LIMIT ?',
                                      (stream, MAX_BATCH)).fetchall()
                events = []
                body = {k:v for k,v in config['credentials'].items() if k != 'token'}
                for row in rows:
                    event = outbox_event(row)
                    candidate = dict(body, events=events + [event])
                    if len(encode(candidate)) > MAX_BODY:
                        break
                    events.append(event)
                if rows and not events:
                    raise CollectionError('invalid_event', '队首文本超过上传上限；保留等待处理')
                if not events:
                    continue
                body['events'] = events
                validate_batch(body)
                response = self._request(config, '/api/v1/events', body)
                self.record_receipt(events, response)
                count += len(events)
                with self.collector.database() as db:
                    db.execute('UPDATE streams SET upload_error=NULL WHERE id=?', (stream,))
            except (CollectionError, ProtocolError) as exc:
                code = exc.code if isinstance(exc, CollectionError) else 'invalid_event'
                with self.collector.database() as db:
                    db.execute('UPDATE streams SET upload_error=? WHERE id=?', (code,stream))
                errors.append(dict(stream_id=stream,code=code))
        return dict(acknowledged_count=count, errors=errors, **self.collector.status())

    def retained(self):
        with self.collector.database() as db:
            rows = db.execute('SELECT event_id,acknowledged_at,quarantined FROM outbox WHERE acknowledged_at IS NOT NULL OR quarantined=1 ORDER BY sequence').fetchall()
        return dict(events=[dict(r) for r in rows])

    def prune(self):
        # A restore must be noticed before old acknowledgments authorize deletion.
        self._session(self._config())
        with self.collector.database() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT value FROM metadata WHERE key='recovery_required'").fetchone():
                raise CollectionError('recovery_required', '本地数据等待核对')
            count = db.execute('DELETE FROM outbox WHERE acknowledged_at<=? AND quarantined=0',
                               (self.clock() - RETENTION_SECONDS,)).rowcount
        return dict(deleted_count=count)
