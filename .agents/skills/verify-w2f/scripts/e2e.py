#!/usr/bin/env python3
"""Disposable synthetic reader -> actual client -> HTTPS -> PostgreSQL proof."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid

import control
import message_fixture as source

ROOT = control.ROOT
from qa.isolation import child_env, ProcessTree


def trace(run, data, action, **facts):
    with (run / 'evidence/trace.jsonl').open('a', encoding='utf-8') as output:
        output.write(json.dumps(dict(stage=data.get('stage'),action=action,**facts), ensure_ascii=False) + '\n')


def stage(run, data, name):
    data['stage'] = name
    trace(run,data,'stage_started')


def command(run, data, *args, stdin=None):
    config = run / 'runtime/compose.yaml'
    if control.hashlib.sha256(config.read_bytes()).hexdigest() != data['compose_sha256']:
        raise ValueError('Fixture configuration changed')
    result = subprocess.run(['docker', 'compose', '--project-name', data['project'],
        '--env-file', str(run / 'runtime/state/compose.env'), '-f', str(config), *args],
        input=stdin, capture_output=True, text=True, encoding='utf-8', timeout=60)
    if result.returncode:
        raise RuntimeError('Fixture command failed: ' + result.stderr[-1000:])
    return result.stdout


def prepare_guard(run, data):
    data['child_env'] = child_env(run / 'runtime')
    guard = run / 'runtime/bootstrap'
    # Applies to the client and its Python reader subprocess, including library/clock probes.
    bootstrap = guard / 'sitecustomize.py'
    bootstrap.write_text(bootstrap.read_text() + '''import json, os, sys
allowed = json.loads(os.environ['W2F_E2E_NETWORK'])
def guard(event, args):
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or list(address[:2]) not in allowed:
            raise RuntimeError('E2E blocked unexpected network destination')
    if event == 'socket.getaddrinfo' and args[0] not in ('127.0.0.1', b'127.0.0.1'):
        raise RuntimeError('E2E blocked unexpected DNS destination')
sys.addaudithook(guard)
os.environ['W2F_E2E_GUARD_ACTIVE']='1'
''')
    return guard


def client(run, data, state, *args, ok=True, clock=None, receipt_file=None, extra_port=None):
    ports = [['127.0.0.1', int(data['url'].rsplit(':',1)[1])]]
    if extra_port:
        ports.append(['127.0.0.1',extra_port])
    env = {**os.environ, **data['child_env'], 'W2F_E2E_NETWORK':json.dumps(ports)}
    if clock is None:
        invocation = ['-m', 'client', '--state-dir', str(state), *args]
    else:
        # Time is the only substituted dependency. Storage and HTTPS remain real.
        script = '''import json, sys
from client.collector import Collector
from client.upload import Uploader
u=Uploader(Collector(sys.argv[1]), clock=lambda:float(sys.argv[2]))
if sys.argv[3]=='receipt':
    with open(sys.argv[4]) as f: v=json.load(f)
    u.record_receipt(v['events'],v['response']); result={'recorded':True}
else: result=getattr(u,sys.argv[3])()
print(json.dumps(result,ensure_ascii=False))
'''
        invocation = ['-c', script, str(state), str(clock), args[0]] + ([str(receipt_file)] if receipt_file else [])
    process = subprocess.Popen([sys.executable, *invocation], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', start_new_session=True)
    with ProcessTree(process.pid, run / 'runtime'):
        stdout, stderr = process.communicate(timeout=60)
    assert not control.live_processes(run), 'Owned client/reader processes remain'
    trace(run,data,'client',command=args[0],exit_code=process.returncode,
          output=json.loads(stdout) if stdout.strip() else None)
    assert (process.returncode == 0) == ok, 'Client outcome mismatch: ' + stdout + stderr
    return json.loads(stdout)


def http(run, data, route, credentials=None, body=None):
    context = ssl.create_default_context(cafile=str(run / 'runtime/state/pki/ca.crt'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    headers = {'Content-Type':'application/json'}
    if credentials:
        headers['Authorization'] = 'Bearer ' + credentials['token']
    request = urllib.request.Request(data['url'] + route, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        response = opener.open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        result = json.loads(response.read())
        trace(run,data,'https',route=route,status=response.code,body=result)
        return response.code, result


def provision(run, data, alias):
    runtime = run / 'runtime'
    source_path = runtime / (alias + '-source.sqlite3')
    source.initialize(source_path)
    reader = runtime / (alias + '-reader')
    reader.write_text(f'#!{sys.executable}\nimport os, sys\nassert os.environ.get("W2F_E2E_GUARD_ACTIVE")=="1"\n'
        f'sys.path.insert(0, {str(Path(__file__).parent)!r})\nimport message_fixture as s\n'
        f'with s.connect({str(source_path)!r}) as db:\n    result=s.read_page(db,sys.argv[1:])\n'
        'import json\nprint(json.dumps(result,ensure_ascii=False))\n')
    reader.chmod(0o700)
    state = runtime / (alias + '-client')
    identity = client(run, data, state, 'init', '--reader', str(reader), '--account', 'verify-' + alias)
    stream = client(run, data, state, 'enable', '--talker', source.TALKER)
    spec = dict(user_id=str(uuid.uuid4()), device_id=identity['device_id'], source_account='verify-' + alias,
                bindings=[dict(id=str(uuid.uuid4()), stream_id=stream['id'], talker=source.TALKER)])
    remote = '/tmp/' + alias + '-credentials.json'
    command(run, data, 'exec', '-T', 'app', 'python', '-m', 'cloud.manage', 'provision',
            '--spec-file', '-', '--credentials-file', remote, stdin=json.dumps(spec))
    # Docker cp cannot read this tmpfs on all engines. Capture inside the process; never log the credential.
    raw = command(run, data, 'exec', '-T', 'app', 'python', '-c',
                  'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())', remote)
    credentials = runtime / (alias + '-credentials.json')
    with os.fdopen(os.open(credentials, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as output:
        output.write(raw)
    connect(run, data, state, credentials, data['url'])
    return dict(state=state, source=source_path, spec=spec, credentials=json.loads(raw), credentials_path=credentials)


def connect(run, data, state, credentials, url):
    return client(run, data, state, 'connect', '--credentials-file', str(credentials), '--url', url,
                  '--ca-file', str(run / 'runtime/state/pki/ca.crt'))


def batch(device, events):
    return {**{k:v for k,v in device['credentials'].items() if k != 'token'}, 'events':events}


def snapshot(run, data):
    # Independent SELECTs, never insert messages/jobs to manufacture success.
    code = '''import json
from cloud.app import Settings
from cloud.store import Store
with Store(Settings.from_env()).connection() as db:
    messages=db.execute('SELECT event_id,binding_id,sequence,local_id,message FROM w2f_messages ORDER BY received_at').fetchall()
    jobs=db.execute('SELECT event_id,binding_id,state FROM w2f_delivery_jobs ORDER BY event_id').fetchall()
print(json.dumps(dict(messages=messages,jobs=jobs),default=str,ensure_ascii=False))
'''
    return json.loads(command(run, data, 'exec', '-T', 'app', 'python', '-c', code))


@contextmanager
def lost_ack_proxy(run, data):
    attempts = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0'))) if self.command == 'POST' else None
            context = ssl.create_default_context(cafile=str(run / 'runtime/state/pki/ca.crt'))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
            req = urllib.request.Request(data['url'] + self.path, data=raw,
                headers={'Authorization':self.headers['Authorization'], 'Content-Type':'application/json'})
            with opener.open(req, timeout=15) as upstream:
                reply, status = upstream.read(), upstream.code
            if self.command == 'POST':
                attempts.append(dict(status=status, receipt=json.loads(reply)))
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.send_response(status)
            self.end_headers()
            self.wfile.write(reply)
        do_GET = handle_request
        do_POST = handle_request
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(run / 'runtime/state/tls/server.crt', run / 'runtime/state/tls/server.key')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, attempts
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        assert not thread.is_alive()


def drive(run, data):
    stage(run,data,'provision')
    prepare_guard(run, data)
    a, b = provision(run,data,'alpha'), provision(run,data,'beta')
    def cli(device, *args, **kwargs):
        return client(run,data,device['state'],*args,**kwargs)
    rows = []
    rows.append(source.inject(a['source'], 'M1 中文端云验证 😀 ' + data['token'][:8]))
    cli(a,'collect')
    event = cli(a,'pending')['events'][0]
    stage(run,data,'lost_ack')
    with lost_ack_proxy(run,data) as (port, attempts):
        connect(run,data,a['state'],a['credentials_path'],f'https://127.0.0.1:{port}')
        failed = cli(a,'upload',ok=False,extra_port=port)
        assert failed['pending_count'] == 1 and failed['retained_count'] == 0
        assert len(attempts) == 1 and attempts[0]['status'] == 200
        trace(run,data,'proxy_committed_receipt',attempts=attempts)
    connect(run,data,a['state'],a['credentials_path'],data['url'])
    persisted = snapshot(run,data)
    trace(run,data,'postgres_after_lost_ack',snapshot=persisted)
    assert len(persisted['messages']) == len(persisted['jobs']) == 1
    assert persisted['messages'][0]['message'] == rows[0]
    assert persisted['jobs'][0]['state'] == 'pending'
    now = 1700000000
    stage(run,data,'replay_and_first_ack')
    received = cli(a,'upload',clock=now)
    assert received['acknowledged_count'] == 1 and received['pending_count'] == 0
    assert snapshot(run,data) == persisted
    status, response = http(run,data,'/api/v1/events',a['credentials'],batch(a,[event]))
    assert status == 200
    receipt = run / 'runtime/repeat-receipt.json'
    control.save(receipt,dict(events=[event],response=response))
    cli(a,'receipt',clock=now+3600,receipt_file=receipt)
    assert cli(a,'retained')['events'][0]['acknowledged_at'] == now
    # Real process/container recreation, same private volumes, additive migration repeats safely.
    stage(run,data,'restart')
    control.compose(run,data,'up','-d','--force-recreate','--wait','--wait-timeout','120','db','app')
    assert snapshot(run,data) == persisted
    assert cli(a,'upload')['acknowledged_count'] == 0
    status, archive = http(run,data,'/api/v1/messages/' + event['event_id'],a['credentials'])
    assert status == 200 and archive['message'] == rows[0] and archive['delivery_state'] == 'pending'
    assert archive['binding_id'] == a['spec']['bindings'][0]['id']
    assert http(run,data,'/api/v1/messages/' + event['event_id'],b['credentials'])[0] == 404
    assert http(run,data,'/api/v1/events',None,batch(a,[event]))[0] == 401
    stage(run,data,'authorization_and_identity')
    rejected = []
    for label, modify, expected in (
        ('different_payload',lambda v:v['events'][0]['message'].update(text='changed'),409),
        ('different_account',lambda v:v.update(source_account='verify-beta'),403),
        ('different_device',lambda v:v.update(device_id=b['spec']['device_id']),403),
        ('stale_authorization',lambda v:v.update(auth_version=2),403),
        ('unauthorized_group',lambda v:v['events'][0]['message'].update(talker='forbidden-room'),403),
        ('foreign_stream',lambda v:v['events'][0].update(stream_id=b['spec']['bindings'][0]['stream_id']),403)):
        body=copy.deepcopy(batch(a,[event])); modify(body)
        code, error=http(run,data,'/api/v1/events',a['credentials'],body)
        assert code == expected, (label,code)
        rejected.append(dict(case=label,status=code,error=error))
    assert snapshot(run,data) == persisted
    # Pending entries survive pruning; the first confirmation, not replay time, starts retention.
    stage(run,data,'retention')
    rows.append(source.inject(a['source'],'M1 待上传保留'))
    cli(a,'collect')
    pending=cli(a,'pending')['events']
    assert cli(a,'prune',clock=now+604799)['deleted_count'] == 0
    assert cli(a,'prune',clock=now+604800)['deleted_count'] == 1
    assert cli(a,'pending')['events'] == pending
    # A bad tail rolls the entire batch back; no message without its unique job.
    stage(run,data,'atomic_batch_and_second_user')
    invalid=copy.deepcopy(pending[0]); invalid['event_id']=str(uuid.uuid4()); invalid['sequence']+=1
    invalid['message']['local_id']+=1; invalid['message']['talker']='forbidden-room'
    assert http(run,data,'/api/v1/events',a['credentials'],batch(a,pending+[invalid]))[0] == 403
    assert snapshot(run,data) == persisted
    assert cli(a,'upload')['acknowledged_count'] == 1
    source.inject(b['source'],'M1 第二用户独立保存 😀'); cli(b,'collect')
    beta_event=cli(b,'pending')['events'][0]
    assert cli(b,'upload')['acknowledged_count'] == 1
    final=snapshot(run,data)
    assert len(final['messages']) == len(final['jobs']) == 3
    # Restore-generation changes quarantine both pending and acknowledged local copies.
    stage(run,data,'restore_generation')
    source.inject(a['source'],'M1 代次切换前未确认消息'); cli(a,'collect')
    command(run,data,'exec','-T','app','python','-m','cloud.manage','new-generation')
    assert cli(a,'upload',ok=False)['error']['code'] == 'recovery_required'
    source.inject(a['source'],'M1 等待管理员核对期间的新消息')
    blocked=cli(a,'collect',ok=False)
    assert blocked['streams'][0]['error_code'] == 'recovery_required'
    assert blocked['quarantined_count'] == 2 and blocked['pending_count'] == 0
    assert cli(a,'prune',ok=False)['error']['code'] == 'recovery_required'
    # Disable each management object through the restricted public command.
    stage(run,data,'revocation')
    for kind, value in (('binding',a['spec']['bindings'][0]['id']),('device',b['spec']['device_id']),('user',a['spec']['user_id'])):
        owner=a if kind != 'device' else b
        own_event=event if owner is a else beta_event
        if kind == 'user':
            assert http(run,data,'/api/v1/session',owner['credentials'])[0] == 200
        else:
            assert http(run,data,'/api/v1/events',owner['credentials'],batch(owner,[own_event]))[0] == 200
        shown=json.loads(command(run,data,'exec','-T','app','python','-m','cloud.manage','show','--kind',kind,'--id',value))
        assert shown['enabled'] is True
        disabled=json.loads(command(run,data,'exec','-T','app','python','-m','cloud.manage','disable','--kind',kind,'--id',value))
        assert disabled['enabled'] is False
        code,error=http(run,data,'/api/v1/events',owner['credentials'],batch(owner,[own_event]))
        assert code == 403 and error['error'] == ('binding_forbidden' if kind == 'binding' else 'authorization_revoked')
    control.save(run / 'evidence/ingest.json',dict(result='passed', source_rows=rows, event=event,
        after_lost_ack=persisted, archive=archive, final=final, rejected=rejected,
        retention=dict(before_seven_days=0,at_seven_days=1,pending_preserved=True),
        restart='actual app/database recreation', network='client and reader allow only owned HTTPS ports'))


def preserve_state(run, data):
    for state in (run / 'runtime').glob('*-client'):
        try:
            with sqlite3.connect((state / 'collector.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory=sqlite3.Row
                # Synthetic data only. Never copy metadata or cloud.json containing credentials.
                rows=[dict(r) for r in db.execute('SELECT * FROM outbox ORDER BY sequence')]
                streams=[dict(r) for r in db.execute('SELECT * FROM streams')]
            control.save(run / 'evidence' / (state.name + '.json'),dict(outbox=rows,streams=streams))
        except (OSError,sqlite3.Error) as exc:
            trace(run,data,'state_unavailable',error_type=type(exc).__name__)
    try:
        control.save(run / 'evidence/postgres-final.json',snapshot(run,data))
    except Exception as exc:
        trace(run,data,'postgres_unavailable',error_type=type(exc).__name__)


def main():
    run=Path(control.launch())
    print(str(run),flush=True)
    _,data=control.owned(run)
    result=dict(result='failed',mode='text-cloud-ingest',release=data['release'])
    try:
        drive(run,data)
        result['result']='passed'
    except BaseException as exc:
        result.update(error_type=type(exc).__name__,failed_stage=data.get('stage'))
        raise
    finally:
        preserve_state(run,data)
        control.save(run / 'evidence/e2e.json',result)
        result['cleanup']=control.cleanup(run)['result']
        control.save(run / 'evidence/e2e.json',result)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
