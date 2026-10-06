"""Transactional text archive. Delivery jobs are consumed by the independent sender."""
from contextlib import contextmanager
import hashlib
import secrets
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from w2f_protocol.ingest import VERSION, ProtocolError, digest, identifier, text, validate_batch

# Prototype writes share one transaction lock, including authorization mutations.
# This deliberately favors an auditable revoke/commit boundary over throughput.
LOCK = 4004001
SCHEMA_VERSION = 1
DDL = '''
CREATE TABLE w2f_schema(version integer PRIMARY KEY, installed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE w2f_generation(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), id uuid NOT NULL);
CREATE TABLE w2f_users(id uuid PRIMARY KEY, enabled boolean NOT NULL DEFAULT true);
CREATE TABLE w2f_devices(id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES w2f_users(id),
    source_account text NOT NULL UNIQUE, token_hash text NOT NULL UNIQUE,
    auth_version bigint NOT NULL DEFAULT 1, enabled boolean NOT NULL DEFAULT true);
CREATE TABLE w2f_bindings(id uuid PRIMARY KEY, device_id uuid NOT NULL REFERENCES w2f_devices(id),
    stream_id uuid NOT NULL UNIQUE, talker text NOT NULL, enabled boolean NOT NULL DEFAULT true,
    delivery_enabled boolean NOT NULL DEFAULT false,
    last_sequence bigint NOT NULL DEFAULT 0, last_local_id bigint NOT NULL DEFAULT 0,
    UNIQUE(device_id,talker));
CREATE TABLE w2f_messages(event_id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES w2f_users(id),
    binding_id uuid NOT NULL REFERENCES w2f_bindings(id), stream_id uuid NOT NULL,
    sequence bigint NOT NULL, local_id bigint NOT NULL, payload_hash text NOT NULL, message jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(stream_id,sequence), UNIQUE(stream_id,local_id));
CREATE TABLE w2f_delivery_jobs(event_id uuid PRIMARY KEY REFERENCES w2f_messages(event_id),
    binding_id uuid NOT NULL REFERENCES w2f_bindings(id), state text NOT NULL DEFAULT 'pending',
    failure_code text);
CREATE INDEX w2f_messages_user ON w2f_messages(user_id,received_at);
'''


class StoreError(Exception):
    def __init__(self, code, status=403):
        super().__init__(code)
        self.code, self.status = code, status


class Store:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def connection(self, write=False):
        s = self.settings
        with psycopg.connect(host=s.database_host, port=s.database_port, dbname='w2f', user='w2f',
                password=s.database_password, connect_timeout=3, row_factory=dict_row,
                options='-c statement_timeout=15000 -c lock_timeout=10000') as db:
            if write:
                db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
            yield db
        # psycopg commits here; callers must not return a receipt before this exits.

    def migrate(self):
        with self.connection(write=True) as db:
            exists = db.execute("SELECT to_regclass('public.w2f_schema') AS name").fetchone()['name']
            if exists:
                versions = [r['version'] for r in db.execute('SELECT version FROM w2f_schema ORDER BY version')]
                if versions != [SCHEMA_VERSION]:
                    raise StoreError('schema_version', 503)
            else:
                db.execute(DDL)
                db.execute('INSERT INTO w2f_generation(id) VALUES(%s)', (uuid.uuid4(),))
                db.execute('INSERT INTO w2f_schema(version) VALUES(%s)', (SCHEMA_VERSION,))
            # Keep the core v1 marker compatible with the M1 API on image rollback.
            # Delivery evolves in its own additive, transactional schema namespace.
            delivery = db.execute("SELECT to_regclass('public.w2f_delivery_schema') AS name").fetchone()['name']
            if not delivery:
                from cloud.delivery import MIGRATION_2
                db.execute(MIGRATION_2)
                db.execute('CREATE TABLE w2f_delivery_schema(version integer PRIMARY KEY)')
                db.execute('INSERT INTO w2f_delivery_schema VALUES(2)')
            elif db.execute('SELECT version FROM w2f_delivery_schema').fetchall() != [{'version':2}]:
                raise StoreError('delivery_schema_version',503)

    def provision(self, spec):
        if not isinstance(spec, dict) or set(spec) != {'user_id', 'device_id', 'source_account', 'bindings'}:
            raise ProtocolError('Invalid provision spec')
        identifier(spec['user_id']); identifier(spec['device_id'])
        if not text(spec['source_account']) or not isinstance(spec['bindings'], list) or not 1 <= len(spec['bindings']) <= 100:
            raise ProtocolError('Invalid account or bindings')
        for binding in spec['bindings']:
            if not isinstance(binding, dict) or set(binding) != {'id', 'stream_id', 'talker'} or not text(binding['talker']):
                raise ProtocolError('Invalid binding')
            identifier(binding['id']); identifier(binding['stream_id'])
        token = secrets.token_urlsafe(32)
        with self.connection(write=True) as db:
            db.execute('INSERT INTO w2f_users(id) VALUES(%s) ON CONFLICT DO NOTHING', (spec['user_id'],))
            if not db.execute('SELECT enabled FROM w2f_users WHERE id=%s', (spec['user_id'],)).fetchone()['enabled']:
                raise StoreError('user_disabled')
            db.execute('INSERT INTO w2f_devices(id,user_id,source_account,token_hash) VALUES(%s,%s,%s,%s)',
                (spec['device_id'], spec['user_id'], spec['source_account'], hashlib.sha256(token.encode()).hexdigest()))
            for b in spec['bindings']:
                db.execute('INSERT INTO w2f_bindings(id,device_id,stream_id,talker) VALUES(%s,%s,%s,%s)',
                           (b['id'], spec['device_id'], b['stream_id'], b['talker']))
        return dict(protocol=VERSION, token=token, device_id=spec['device_id'],
                    source_account=spec['source_account'], auth_version=1)

    def _authenticate(self, db, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise StoreError('unauthorized', 401)
        device = db.execute('''SELECT d.*, u.enabled AS user_enabled FROM w2f_devices d
            JOIN w2f_users u ON u.id=d.user_id WHERE d.token_hash=%s''',
            (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not device:
            raise StoreError('unauthorized', 401)
        if not device['enabled'] or not device['user_enabled']:
            raise StoreError('authorization_revoked')
        return device

    def ingest(self, token, body):
        validate_batch(body)
        receipts = []
        with self.connection(write=True) as db:
            d = self._authenticate(db, token)
            if (str(d['id']) != body['device_id'] or d['source_account'] != body['source_account']
                    or d['auth_version'] != body['auth_version']):
                raise StoreError('authorization_mismatch')
            generation = str(db.execute('SELECT id FROM w2f_generation').fetchone()['id'])
            for event in body['events']:
                message = event['message']
                b = db.execute('SELECT * FROM w2f_bindings WHERE device_id=%s AND stream_id=%s AND talker=%s',
                    (d['id'], event['stream_id'], message['talker'])).fetchone()
                if not b or not b['enabled']:
                    raise StoreError('binding_forbidden')
                payload_hash = digest(event)
                old = db.execute('SELECT * FROM w2f_messages WHERE event_id=%s', (event['event_id'],)).fetchone()
                if old:
                    if old['user_id'] != d['user_id'] or old['binding_id'] != b['id'] or old['payload_hash'] != payload_hash:
                        raise StoreError('identity_conflict', 409)
                else:
                    if event['sequence'] <= b['last_sequence'] or message['local_id'] <= b['last_local_id']:
                        raise StoreError('source_order_conflict', 409)
                    db.execute('''INSERT INTO w2f_messages(event_id,user_id,binding_id,stream_id,sequence,local_id,payload_hash,message)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''', (event['event_id'],d['user_id'],b['id'],event['stream_id'],
                        event['sequence'],message['local_id'],payload_hash,Jsonb(message)))
                    db.execute('INSERT INTO w2f_delivery_jobs(event_id,binding_id) VALUES(%s,%s)', (event['event_id'],b['id']))
                    db.execute('UPDATE w2f_bindings SET last_sequence=%s,last_local_id=%s WHERE id=%s',
                               (event['sequence'],message['local_id'],b['id']))
                receipts.append(dict(event_id=event['event_id'], payload_hash=payload_hash,
                                     binding_id=str(b['id']), state='stored'))
        return dict(protocol=VERSION, generation=generation, receipts=receipts)

    def session(self, token):
        with self.connection() as db:
            d = self._authenticate(db, token)
            generation = str(db.execute('SELECT id FROM w2f_generation').fetchone()['id'])
        return dict(protocol=VERSION, generation=generation, device_id=str(d['id']),
                    source_account=d['source_account'], auth_version=d['auth_version'])

    def message(self, event_id, token=None):
        identifier(event_id)
        with self.connection() as db:
            user = self._authenticate(db, token)['user_id'] if token is not None else None
            row = db.execute('''SELECT m.event_id,m.user_id,m.binding_id,m.stream_id,m.sequence,m.message,m.payload_hash,
                m.received_at,j.state AS delivery_state,j.failure_code FROM w2f_messages m
                JOIN w2f_delivery_jobs j USING(event_id) WHERE m.event_id=%s''' +
                (' AND m.user_id=%s' if user is not None else ''),
                (event_id,user) if user is not None else (event_id,)).fetchone()
            if not row:
                raise StoreError('not_found', 404)
        return {**row, 'receive_state':'stored'}

    def show(self, kind, value):
        identifier(value)
        if kind == 'message':
            return self.message(value)
        columns = dict(user='id,enabled', device='id,user_id,source_account,auth_version,enabled',
                       binding='id,device_id,stream_id,talker,enabled,delivery_enabled,last_sequence,last_local_id')
        table = dict(user='w2f_users', device='w2f_devices', binding='w2f_bindings')[kind]
        with self.connection() as db:
            row = db.execute(f'SELECT {columns[kind]} FROM {table} WHERE id=%s', (value,)).fetchone()
        if not row:
            raise StoreError('not_found', 404)
        return row

    def disable(self, kind, value):
        identifier(value)
        table = dict(user='w2f_users', device='w2f_devices', binding='w2f_bindings')[kind]
        with self.connection(write=True) as db:
            extra = ',auth_version=auth_version+1' if kind == 'device' else ''
            row = db.execute(f'UPDATE {table} SET enabled=false{extra} WHERE id=%s RETURNING id', (value,)).fetchone()
            if not row:
                raise StoreError('not_found', 404)
        return self.show(kind, value)

    def new_generation(self):
        """Operator restore fence, while ingress/sending is stopped; never replay jobs."""
        generation = uuid.uuid4()
        with self.connection(write=True) as db:
            db.execute('UPDATE w2f_generation SET id=%s', (generation,))
            db.execute('UPDATE w2f_bindings SET delivery_enabled=false')
        return dict(generation=str(generation), delivery_enabled=False)
