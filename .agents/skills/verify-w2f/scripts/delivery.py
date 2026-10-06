#!/usr/bin/env python3
"""Real client/cloud sender with a disposable HTTP Feishu boundary."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import control
import e2e
import message_fixture as source


def manage(run,data,action,binding,*args):
    return json.loads(e2e.command(run,data,'exec','-T','app','python','-m','cloud.manage',
        'delivery-'+action,'--binding',binding,*args))


def once(run,data,now=None):
    command=['run','--rm','--no-deps','-T','sender']
    if now is None:
        command+=['python','-m','cloud.sender','--once']
    else:
        command+=['python','-c',
            'import json,sys; from cloud.sender import settings,transport; from cloud.store import Store; from cloud.delivery import Delivery; '
            'print(json.dumps(Delivery(Store(settings()),transport(),clock=lambda:float(sys.argv[1])).run_once()))',str(now)]
    result=json.loads(e2e.command(run,data,*command))
    e2e.trace(run,data,'sender_once',result=result,clock=now)
    return result


def sink(run):
    with sqlite3.connect((run/'runtime/feishu/sink.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        return {table:[dict(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('attempts','receipts')}


def credentials(run,devices):
    entries={d['app_id']:dict(user_id=d['spec']['user_id'],app_secret='synthetic-'+d['app_id']) for d in devices}
    path=run/'runtime/state/secrets/feishu_apps.json'
    path.chmod(0o600)
    path.write_text(json.dumps(entries))
    path.chmod(0o444)
    control.save(run/'runtime/feishu/control.json',dict(apps={k:v['app_secret'] for k,v in entries.items()},modes={}))


def mode(run,chat,value):
    path=run/'runtime/feishu/control.json'
    data=json.loads(path.read_text())
    data['modes'][chat]=value
    control.save(path,data)


def collect(run,data,device,texts):
    for text in texts:
        source.inject(device['source'],text,max_bytes=16384)
    e2e.client(run,data,device['state'],'collect')
    events=e2e.client(run,data,device['state'],'pending')['events']
    e2e.client(run,data,device['state'],'upload')
    return events


def requests(run,device,table='attempts'):
    return [row for row in sink(run)[table] if row['chat']==device['chat']]


def wait_for(predicate):
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError('Timed out waiting for observed fixture state')


def faults(run,data,a,b):
    binding=a['spec']['bindings'][0]['id']
    other=b['spec']['bindings'][0]['id']
    e2e.stage(run,data,'multipart-restart')
    before=len(requests(run,a))
    collect(run,data,a,['中'*5000+'😀尾部','分段之后'])
    once(run,data)
    status=manage(run,data,'status',binding)
    assert status['parts'][-2]['state']=='sent' and status['parts'][-1]['state']=='pending'
    control.compose(run,data,'up','-d','--force-recreate','--wait','app')
    once(run,data); once(run,data)
    sent=requests(run,a)[before:]
    assert len(sent)==3
    rendered=[json.loads(json.loads(r['request'])['content'])['text'] for r in sent]
    assert ''.join(t.removeprefix('测试发送者：') for t in rendered[:2])=='中'*5000+'😀尾部'
    assert rendered[-1]=='测试发送者：分段之后'

    e2e.stage(run,data,'rate-limit-and-binding-isolation')
    before=len(requests(run,a))
    collect(run,data,a,['限流旧消息','限流后的新消息'])
    collect(run,data,b,['其他绑定不受限流影响'])
    mode(run,a['chat'],'rate')
    once(run,data)
    status=manage(run,data,'status',binding)
    part=status['parts'][-1]
    assert part['state']=='retry' and part['uncertain_since'] is None
    once(run,data,part['first_attempt_at']+1)
    assert len(requests(run,a))==before+1
    assert manage(run,data,'status',other)['jobs'][-1]['state']=='sent'
    mode(run,a['chat'],'success')
    once(run,data,part['next_attempt_at']+1); once(run,data)
    rows=requests(run,a)[before:]
    assert len(rows)==3 and rows[0]['request']==rows[1]['request'] and rows[2]['request']!=rows[1]['request']

    e2e.stage(run,data,'definite-rejection-and-resume')
    collect(run,data,a,['被拒绝但保留的消息'])
    mode(run,a['chat'],'reject')
    once(run,data)
    status=manage(run,data,'status',binding)
    assert not status['delivery_enabled'] and status['parts'][-1]['state']=='failed'
    attempts=len(requests(run,a)); once(run,data)
    assert len(requests(run,a))==attempts
    mode(run,a['chat'],'success'); manage(run,data,'resume',binding); once(run,data)
    assert requests(run,a)[-1]['request']==requests(run,a)[-2]['request']

    for fault in ('drop','missing_id','being_sent','server_error'):
        e2e.stage(run,data,'unknown-'+fault)
        collect(run,data,a,['未知结果 '+fault])
        before=len(requests(run,a)); receipt_count=len(requests(run,a,'receipts'))
        mode(run,a['chat'],fault); once(run,data)
        status=manage(run,data,'status',binding); part=status['parts'][-1]
        assert part['state']=='unknown' and part['uncertain_since'] is not None
        assert len(requests(run,a))==before+1
        assert len(requests(run,a,'receipts'))==receipt_count+(fault!='server_error')
        mode(run,a['chat'],'success'); once(run,data,part['next_attempt_at']+1)
        assert requests(run,a)[-1]['request']==requests(run,a)[-2]['request']
        assert len(requests(run,a,'receipts'))==receipt_count+1
        assert manage(run,data,'status',binding)['parts'][-1]['state']=='sent'

    e2e.stage(run,data,'session-lock-pause-in-flight')
    collect(run,data,b,['在途请求','暂停后不应发出的下一条'])
    before=len(requests(run,b)); mode(run,b['chat'],'hold')
    release=run/'runtime/feishu/release-hold'
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker=pool.submit(once,run,data)
        try:
            wait_for(lambda:len(requests(run,b))==before+1)
            once(run,data)
            assert len(requests(run,b))==before+1
            manage(run,data,'pause',other)
        finally:
            release.touch()
        worker.result()
    once(run,data)
    assert len(requests(run,b))==before+1
    status=manage(run,data,'status',other)
    assert status['jobs'][-2]['state']=='sent' and status['jobs'][-1]['state']=='pending'
    release.unlink(); mode(run,b['chat'],'success'); manage(run,data,'resume',other); once(run,data)

    e2e.stage(run,data,'lost-worker-fencing')
    collect(run,data,b,['丢失数据库锁的请求','旧进程不得继续发送'])
    before=len(requests(run,b)); receipts=len(requests(run,b,'receipts')); mode(run,b['chat'],'hold')
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker=pool.submit(once,run,data)
        try:
            wait_for(lambda:len(requests(run,b))==before+1)
            # Terminate only the backend holding this fixture binding's advisory lock.
            code='''import sys
from cloud.app import Settings
from cloud.store import Store
from cloud.delivery import binding_lock
key=binding_lock(sys.argv[1]) & ((1<<64)-1)
with Store(Settings.from_env()).connection() as db:
    rows=db.execute("SELECT pg_terminate_backend(pid) AS stopped FROM pg_locks WHERE locktype='advisory' AND objsubid=1 AND classid=%s AND objid=%s AND granted",(key>>32,key&0xffffffff)).fetchall()
    assert rows==[{'stopped':True}],rows
'''
            e2e.command(run,data,'exec','-T','app','python','-c',code,other)
        finally:
            release.touch()
        try:
            worker.result()
            raise AssertionError('Lost-lock sender committed its receipt')
        except RuntimeError:
            pass
    assert len(requests(run,b))==before+1
    assert manage(run,data,'status',other)['parts'][-1]['state']=='sending'
    release.unlink(); mode(run,b['chat'],'success')
    once(run,data); once(run,data)
    rows=requests(run,b)[before:]
    assert len(rows)==3 and rows[0]['request']==rows[1]['request']
    assert len(requests(run,b,'receipts'))==receipts+2

    e2e.stage(run,data,'graceful-sender-stop')
    collect(run,data,b,['停止时的在途请求','重启后才处理下一条'])
    before=len(requests(run,b)); mode(run,b['chat'],'hold')
    control.compose(run,data,'start','sender')
    wait_for(lambda:len(requests(run,b))==before+1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        stopped=pool.submit(control.compose,run,data,'stop','sender')
        # Allow Docker to deliver SIGTERM while the fixture holds the HTTP reply.
        time.sleep(1)
        release.touch()
        stopped.result()
    assert len(requests(run,b))==before+1
    status=manage(run,data,'status',other)
    assert status['jobs'][-2]['state']=='sent' and status['jobs'][-1]['state']=='pending'
    release.unlink(); mode(run,b['chat'],'success'); once(run,data)

    e2e.stage(run,data,'feishu-offline')
    collect(run,data,b,['出口短时断网后恢复'])
    before=len(requests(run,b))
    control.compose(run,data,'stop','feishu-mock')
    once(run,data)
    status=manage(run,data,'status',other)
    assert status['parts'][-1]['failure_code']=='auth_unavailable'
    assert len(requests(run,b))==before
    control.compose(run,data,'start','feishu-mock')
    once(run,data,status['parts'][-1]['next_attempt_at']+1)
    assert len(requests(run,b))==before+1

    e2e.stage(run,data,'unknown-window-expiry')
    collect(run,data,a,['收到但响应丢失且超过去重窗口','不能越过待核对消息'])
    before=len(requests(run,a)); mode(run,a['chat'],'drop'); once(run,data)
    part=manage(run,data,'status',binding)['parts'][-1]
    # A later definite rejection cannot erase the earlier uncertain acceptance.
    mode(run,a['chat'],'reject'); once(run,data,part['next_attempt_at']+1)
    status=manage(run,data,'status',binding)
    assert status['parts'][-1]['state']=='unknown'
    assert status['parts'][-1]['uncertain_since']==part['uncertain_since']
    manage(run,data,'resume',binding)
    collect(run,data,b,['另一个绑定继续工作'])
    once(run,data,part['uncertain_since']+3501)
    status=manage(run,data,'status',binding)
    assert status['pause_reason']=='unknown_expired' and not status['delivery_enabled']
    assert len(requests(run,a))==before+2
    assert manage(run,data,'status',other)['jobs'][-1]['state']=='sent'
    try:
        manage(run,data,'resume',binding)
        raise AssertionError('Expired unknown result resumed without reconciliation')
    except RuntimeError:
        pass
    once(run,data,part['uncertain_since']+7200)
    assert len(requests(run,a))==before+2

    e2e.stage(run,data,'credential-ownership')
    collect(run,data,b,['不能借用另一用户的飞书应用'])
    path=run/'runtime/state/secrets/feishu_apps.json'
    entries=json.loads(path.read_text()); entries[b['app_id']]['user_id']=a['spec']['user_id']
    path.chmod(0o600); path.write_text(json.dumps(entries)); path.chmod(0o444)
    before=len(requests(run,b)); once(run,data)
    assert len(requests(run,b))==before
    assert manage(run,data,'status',other)['pause_reason']=='app_credentials_unavailable'
    control.save(run/'evidence/faults.json',dict(result='passed',alpha=manage(run,data,'status',binding),beta=manage(run,data,'status',other)))


def m1_compatibility(run,data,event_id):
    e2e.stage(run,data,'m1-reader-after-m2-migration')
    revision='61f3ad8cfec54a35508ff1830f937e9974a8e208'
    old_source=subprocess.check_output(['git','show',revision+':cloud/store.py'],cwd=control.ROOT,text=True)
    code='''import json,sys,types
from cloud.app import Settings
legacy=types.ModuleType('m1_store')
exec(compile(sys.stdin.read(),'M1/cloud/store.py','exec'),legacy.__dict__)
store=legacy.Store(Settings.from_env())
store.migrate()
row=store.message(sys.argv[1])
assert row['receive_state']=='stored' and row['delivery_state']=='sent'
with store.connection() as db:
    assert db.execute('SELECT version FROM w2f_schema').fetchall()==[{'version':1}]
    assert db.execute('SELECT version FROM w2f_delivery_schema').fetchall()==[{'version':2}]
print(json.dumps(row,default=str,ensure_ascii=False))
'''
    result=json.loads(e2e.command(run,data,'exec','-T','app','python','-c',code,event_id,stdin=old_source))
    control.save(run/'evidence/m1-compatibility.json',dict(result='passed',source_revision=revision,
        source_sha256=control.hashlib.sha256(old_source.encode()).hexdigest(),message=result))


def drive(run,data):
    e2e.stage(run,data,'setup')
    control.compose(run,data,'stop','sender')
    e2e.prepare_guard(run,data)
    a=e2e.provision(run,data,'delivery-alpha')
    a.update(app_id='cli_verify_alpha',chat='oc_verify_alpha')
    b=e2e.provision(run,data,'delivery-beta')
    b.update(app_id='cli_verify_beta',chat='oc_verify_beta')
    credentials(run,[a,b])
    other=b['spec']['bindings'][0]['id']
    manage(run,data,'target',other,'--app-id',b['app_id'],'--chat-id',b['chat'])
    manage(run,data,'resume',other)
    binding=a['spec']['bindings'][0]['id']
    target=manage(run,data,'target',binding,'--app-id',a['app_id'],'--chat-id',a['chat'])
    assert target['delivery_enabled'] is False
    events=collect(run,data,a,['M2 有序文本 1 😀','M2 有序文本 2'])
    once(run,data)
    assert sink(run)['attempts']==[]
    manage(run,data,'resume',binding)
    once(run,data); once(run,data)
    result=sink(run)
    assert len(result['attempts'])==len(result['receipts'])==2
    assert [json.loads(json.loads(r['request'])['content'])['text'] for r in result['attempts']]==['测试发送者：M2 有序文本 1 😀','测试发送者：M2 有序文本 2']
    status=manage(run,data,'status',binding)
    assert [j['state'] for j in status['jobs']]==['sent','sent']
    readback=json.loads(e2e.command(run,data,'run','--rm','--no-deps','-T','sender','python','-m','cloud.verify',
        '--binding',binding,'--event',events[0]['event_id']))
    assert readback['verified'] and len(readback['parts'])==1
    mode(run,a['chat'],'wrong_readback_target')
    try:
        e2e.command(run,data,'run','--rm','--no-deps','-T','sender','python','-m','cloud.verify',
                    '--binding',binding,'--event',events[0]['event_id'])
        raise AssertionError('Readback accepted a different target')
    except RuntimeError:
        pass
    mode(run,a['chat'],'success')
    assert json.loads(e2e.command(run,data,'run','--rm','--no-deps','-T','sender','python','-m','cloud.verify',
        '--binding',binding,'--event',events[0]['event_id']))['verified']
    for event in events:
        assert e2e.http(run,data,'/api/v1/events',a['credentials'],e2e.batch(a,[event]))[0]==200
    once(run,data)
    assert sink(run)==result
    control.save(run/'evidence/sending.json',dict(result='passed',sink=result,status=status))
    faults(run,data,a,b)
    m1_compatibility(run,data,events[0]['event_id'])


def main():
    run=Path(control.launch(delivery=True))
    print(run,flush=True)
    _,data=control.owned(run)
    result=dict(result='failed',mode='cloud-delivery',release=data['release'])
    try:
        drive(run,data)
        result['result']='passed'
    except BaseException as exc:
        result.update(error_type=type(exc).__name__,failed_stage=data.get('stage'))
        raise
    finally:
        e2e.preserve_state(run,data)
        try:
            control.save(run/'evidence/feishu-final.json',sink(run))
        except (OSError,sqlite3.Error):
            pass
        control.save(run/'evidence/delivery.json',result)
        result['cleanup']=control.cleanup(run)['result']
        control.save(run/'evidence/delivery.json',result)
    print(json.dumps(result))


if __name__=='__main__':
    main()
