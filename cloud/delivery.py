"""Ordered per-binding delivery with a PostgreSQL session lock spanning the HTTP attempt."""
import hashlib
import json
import re
import uuid

from psycopg.types.json import Jsonb

from cloud.feishu import DeliveryError
from cloud.store import LOCK, StoreError
from w2f_protocol.ingest import identifier
from w2f_protocol.text import attributed_text

SAFE_RETRY_SECONDS=3500
MIGRATION_2='''
ALTER TABLE w2f_bindings ADD COLUMN target_app_id text;
ALTER TABLE w2f_bindings ADD COLUMN target_chat_id text;
ALTER TABLE w2f_bindings ADD COLUMN pause_reason text;
CREATE TABLE w2f_delivery_parts(
 event_id uuid NOT NULL REFERENCES w2f_messages(event_id), part integer NOT NULL,
 request_id uuid NOT NULL UNIQUE, payload jsonb NOT NULL,
 state text NOT NULL DEFAULT 'pending', first_attempt_at double precision,
 uncertain_since double precision, last_attempt_at double precision,
 next_attempt_at double precision NOT NULL DEFAULT 0, attempts integer NOT NULL DEFAULT 0,
 attempt_id uuid, message_id text, failure_code text,
 PRIMARY KEY(event_id,part));
'''


def binding_lock(binding):
    return int.from_bytes(hashlib.sha256(('w2f-delivery:'+str(binding)).encode()).digest()[:8], 'big', signed=True)


def retry_window_expired(part, now):
    return (part['uncertain_since'] is not None and (now < part['last_attempt_at'] or now-part['uncertain_since'] >= SAFE_RETRY_SECONDS))


class Delivery:
    def __init__(self, store, transport=None, clock=None):
        self.store,self.transport,self.clock=store,transport,clock

    def now(self,db):
        return self.clock() if self.clock else float(db.execute('SELECT extract(epoch FROM clock_timestamp()) AS now').fetchone()['now'])

    def configure(self,binding,app_id,chat_id):
        identifier(binding)
        if not re.fullmatch(r'cli_[A-Za-z0-9_]{1,128}',app_id) or not re.fullmatch(r'oc_[A-Za-z0-9_]{1,128}',chat_id):
            raise StoreError('invalid_target',422)
        with self.store.connection(write=True) as db:
            b=db.execute('SELECT * FROM w2f_bindings WHERE id=%s',(binding,)).fetchone()
            if not b:
                raise StoreError('not_found',404)
            if b['target_app_id'] and (b['target_app_id'],b['target_chat_id']) != (app_id,chat_id):
                raise StoreError('target_immutable',409)
            db.execute('UPDATE w2f_bindings SET target_app_id=%s,target_chat_id=%s WHERE id=%s',(app_id,chat_id,binding))
        return self.status(binding)

    def pause(self,binding):
        identifier(binding)
        with self.store.connection(write=True) as db:
            if not db.execute("UPDATE w2f_bindings SET delivery_enabled=false,pause_reason=COALESCE(pause_reason,'manual') WHERE id=%s RETURNING id",(binding,)).fetchone():
                raise StoreError('not_found',404)
        return self.status(binding)

    def resume(self,binding):
        identifier(binding)
        with self.store.connection(write=True) as db:
            b=self._binding(db,binding)
            if not b or not b['active'] or not b['target_app_id']:
                raise StoreError('binding_unavailable')
            if b['pause_reason'] == 'unknown_expired':
                raise StoreError('reconciliation_required',409)
            db.execute('UPDATE w2f_bindings SET delivery_enabled=true,pause_reason=NULL WHERE id=%s',(binding,))
            db.execute("UPDATE w2f_delivery_parts p SET state='retry',next_attempt_at=0 WHERE state='failed' AND event_id IN (SELECT event_id FROM w2f_messages WHERE binding_id=%s)",(binding,))
            db.execute("UPDATE w2f_delivery_jobs SET state='pending',failure_code=NULL WHERE binding_id=%s AND state='failed'",(binding,))
        return self.status(binding)

    def _binding(self,db,binding):
        return db.execute('''SELECT b.*,d.user_id,(b.enabled AND d.enabled AND u.enabled) AS active
            FROM w2f_bindings b JOIN w2f_devices d ON d.id=b.device_id JOIN w2f_users u ON u.id=d.user_id
            WHERE b.id=%s''',(binding,)).fetchone()

    def status(self,binding):
        identifier(binding)
        with self.store.connection() as db:
            b=self._binding(db,binding)
            if not b:
                raise StoreError('not_found',404)
            jobs=db.execute('''SELECT j.*,m.sequence FROM w2f_delivery_jobs j JOIN w2f_messages m USING(event_id)
                WHERE j.binding_id=%s ORDER BY m.sequence LIMIT 100''',(binding,)).fetchall()
            parts=db.execute('''SELECT p.event_id,p.part,p.request_id,p.state,p.message_id,p.attempts,
                p.first_attempt_at,p.uncertain_since,p.next_attempt_at,p.failure_code
                FROM w2f_delivery_parts p JOIN w2f_messages m USING(event_id)
                WHERE m.binding_id=%s ORDER BY m.sequence,p.part LIMIT 1000''',(binding,)).fetchall()
        return dict(binding_id=str(b['id']),active=b['active'],delivery_enabled=b['delivery_enabled'],
                    pause_reason=b['pause_reason'],target_app_id=b['target_app_id'],target_chat_id=b['target_chat_id'],jobs=jobs,parts=parts)

    def _prepare(self,db,binding):
        with db.transaction():
            db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,))
            b=self._binding(db,binding)
            if not b or not b['active'] or not b['delivery_enabled'] or not b['target_app_id']:
                return None
            job=db.execute('''SELECT j.*,m.message,m.sequence FROM w2f_delivery_jobs j
                JOIN w2f_messages m USING(event_id) WHERE j.binding_id=%s AND j.state<>'sent'
                ORDER BY m.sequence LIMIT 1''',(binding,)).fetchone()
            if not job:
                return None
            existing=db.execute('SELECT 1 FROM w2f_delivery_parts WHERE event_id=%s',(job['event_id'],)).fetchone()
            if not existing:
                row=job['message']
                name=str(row.get('sender') or '未知发言人')[:256]
                for index,part in enumerate(attributed_text(name,row['text'])):
                    request_id=uuid.uuid4()
                    payload=dict(receive_id=b['target_chat_id'],msg_type='text',content=json.dumps({'text':part},ensure_ascii=False),uuid=str(request_id))
                    db.execute('INSERT INTO w2f_delivery_parts(event_id,part,request_id,payload) VALUES(%s,%s,%s,%s)',
                               (job['event_id'],index,request_id,Jsonb(payload)))
            part=db.execute("SELECT * FROM w2f_delivery_parts WHERE event_id=%s AND state<>'sent' ORDER BY part LIMIT 1",(job['event_id'],)).fetchone()
            if not part:
                db.execute("UPDATE w2f_delivery_jobs SET state='sent',failure_code=NULL WHERE event_id=%s",(job['event_id'],))
                return None
            now=self.now(db)
            if retry_window_expired(part,now):
                db.execute("UPDATE w2f_bindings SET delivery_enabled=false,pause_reason='unknown_expired' WHERE id=%s",(binding,))
                db.execute("UPDATE w2f_delivery_parts SET state='unknown',failure_code='unknown_expired' WHERE event_id=%s AND part=%s",(part['event_id'],part['part']))
                db.execute("UPDATE w2f_delivery_jobs SET state='unknown',failure_code='unknown_expired' WHERE event_id=%s",(job['event_id'],))
                return None
            if part['state']=='failed' or now < part['next_attempt_at']:
                return None
            return b,part

    def _attempt(self,db,b,part,stopping):
        # Token acquisition cannot create a chat message; do it before marking a send intent.
        try:
            token=self.transport.token(b['target_app_id'],b['user_id'])
        except DeliveryError as error:
            self._auth_failure(db,b,part,error)
            return error.code
        with db.transaction():
            db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,))
            current=self._binding(db,b['id'])
            if stopping() or not current['active'] or not current['delivery_enabled']:
                return 'paused'
            now=self.now(db)
            if retry_window_expired(part,now):
                return 'window_expired'  # Next prepare persists the pause; never issue this request.
            attempt_id=uuid.uuid4()
            generation=db.execute('SELECT id FROM w2f_generation').fetchone()['id']
            db.execute("""UPDATE w2f_delivery_parts SET state='sending',attempts=attempts+1,attempt_id=%s,
                first_attempt_at=COALESCE(first_attempt_at,%s),uncertain_since=COALESCE(uncertain_since,%s),last_attempt_at=%s
                WHERE event_id=%s AND part=%s""",(attempt_id,now,now,now,part['event_id'],part['part']))
            db.execute("UPDATE w2f_delivery_jobs SET state='sending',failure_code=NULL WHERE event_id=%s",(part['event_id'],))
        # This same connection holds the session lock. Losing it prevents any receipt
        # commit or subsequent send; a replacement worker sees the committed intent.
        try:
            message_id=self.transport.send(part['payload'],token)
            error=None
        except DeliveryError as exc:
            message_id,error=None,exc
        with db.transaction():
            db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,))
            if db.execute('SELECT id FROM w2f_generation').fetchone()['id'] != generation:
                raise StoreError('generation_changed',409)
            if message_id:
                db.execute("""UPDATE w2f_delivery_parts SET state='sent',message_id=%s,failure_code=NULL
                    WHERE event_id=%s AND part=%s AND attempt_id=%s""",(message_id,part['event_id'],part['part'],attempt_id))
                remains=db.execute("SELECT 1 FROM w2f_delivery_parts WHERE event_id=%s AND state<>'sent'",(part['event_id'],)).fetchone()
                db.execute('UPDATE w2f_delivery_jobs SET state=%s,failure_code=NULL WHERE event_id=%s',('pending' if remains else 'sent',part['event_id']))
            else:
                uncertain=error.outcome=='unknown' or part['uncertain_since'] is not None
                state='unknown' if uncertain else error.outcome
                delay=max(error.retry_after,min(300,5*2**min(part['attempts'],6)))
                db.execute('''UPDATE w2f_delivery_parts SET state=%s,failure_code=%s,next_attempt_at=%s,
                    uncertain_since=CASE WHEN %s THEN uncertain_since ELSE NULL END
                    WHERE event_id=%s AND part=%s AND attempt_id=%s''',
                    (state,error.code,now+delay,uncertain,part['event_id'],part['part'],attempt_id))
                db.execute('UPDATE w2f_delivery_jobs SET state=%s,failure_code=%s WHERE event_id=%s',(state,error.code,part['event_id']))
                if error.outcome=='failed':
                    db.execute('UPDATE w2f_bindings SET delivery_enabled=false,pause_reason=%s WHERE id=%s',(error.code,b['id']))
        return 'sent' if message_id else error.code

    def _auth_failure(self,db,b,part,error):
        with db.transaction():
            db.execute('UPDATE w2f_delivery_parts SET next_attempt_at=%s,failure_code=%s WHERE event_id=%s AND part=%s',
                       (self.now(db)+error.retry_after,error.code,part['event_id'],part['part']))
            db.execute('UPDATE w2f_delivery_jobs SET failure_code=%s WHERE event_id=%s',(error.code,part['event_id']))
            if error.outcome=='failed':
                db.execute('UPDATE w2f_bindings SET delivery_enabled=false,pause_reason=%s WHERE id=%s',(error.code,b['id']))

    def run_once(self,limit=20,heartbeat=None,stopping=lambda:False):
        with self.store.connection() as db:
            bindings=[r['id'] for r in db.execute('''SELECT b.id FROM w2f_bindings b WHERE delivery_enabled
                AND EXISTS(SELECT 1 FROM w2f_delivery_jobs j WHERE j.binding_id=b.id AND j.state<>'sent')
                ORDER BY b.id''')]
        results=[]
        for binding in bindings:
            if stopping() or len(results)>=limit:
                break
            with self.store.connection() as db:
                db.autocommit=True
                locked=db.execute('SELECT pg_try_advisory_lock(%s) AS locked',(binding_lock(binding),)).fetchone()['locked']
                if not locked:
                    continue
                prepared=self._prepare(db,binding)
                if prepared:
                    results.append(dict(binding_id=str(binding),outcome=self._attempt(db,*prepared,stopping)))
            if heartbeat:
                heartbeat()
        return dict(results=results)
