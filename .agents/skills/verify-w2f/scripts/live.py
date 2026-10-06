#!/usr/bin/env python3
"""Bounded, explicit-config M3/M4 operator verification. Real uploads and Feishu sends."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
from client.upload import NoRedirect, private_json
import message_fixture


class VerificationStopped(RuntimeError):
    pass


def interrupted(signum, frame):
    # A second stop request must not interrupt the binding-pause cleanup.
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    # OSError/InterruptedError may be retried by select/urllib; use a distinct
    # exception so interruption reaches deliver()'s binding-pause finally block.
    raise VerificationStopped('Verification stopped')


def bounded_run(args, **kwargs):
    timeout=kwargs.pop('timeout',60)
    with subprocess.Popen(args,start_new_session=True,stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                          **{k:v for k,v in kwargs.items() if k!='input'}) as process:
        try:
            stdout,stderr=process.communicate(kwargs.get('input'),timeout=timeout)
        except BaseException:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.communicate(timeout=3)
            raise
        return subprocess.CompletedProcess(args,process.returncode,stdout,stderr)


def save(path, value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as out:
        json.dump(value,out,ensure_ascii=False,indent=2)
        out.flush(); os.fsync(out.fileno())
    os.replace(temporary,path)


class Live:
    def __init__(self, config, state):
        self.config=private_json(config)
        self.state=Path(state).resolve()
        if not self.state.is_relative_to(ROOT/'.v040-live'):
            raise ValueError('Keep live verification state under this checkout .v040-live')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+',self.config['ssh_host']):
            raise ValueError('Use an explicit user@host')
        if not 1<=int(self.config['ssh_port'])<=65535:
            raise ValueError('Invalid SSH port')
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.state.stat().st_mode & 0o077:
            raise ValueError('Use a private state directory')
        self.meta_path=self.state/'run.json'
        self.meta=private_json(self.meta_path) if self.meta_path.exists() else None
        self.client_state=self.state/'client'
        self.credentials=self.state/'device.json'
        self.ca=self.state/'ca.crt'
        self.lock=(self.state/'.lock').open('a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not re.fullmatch(r'[0-9a-f]{40}',self.config['expected_release']):
            raise ValueError('Pin the exact tested cloud release')
        if self.meta and self.meta['config']!=self.config:
            raise ValueError('Configuration changed; inspect the existing run instead of retargeting it')

    def ssh(self, args, stdin=None, ok=True):
        result=bounded_run(['ssh','-T','-p',str(self.config['ssh_port']),'-o','BatchMode=yes',
            '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=12',self.config['ssh_host'],shlex.join(args)],
            input=stdin,timeout=60)
        if ok and result.returncode:
            raise RuntimeError('Remote operation failed; exit '+str(result.returncode))
        return result

    def remote(self, role, args, stdin=None, ok=True):
        root=self.config['remote_root']
        return self.ssh(['docker','compose','--env-file',root+'/state/compose.env','-f',root+'/compose.yaml',
                         'exec','-T',role,*args],stdin,ok)

    def manage(self, action, *args):
        return json.loads(self.remote('app',['python','-m','cloud.manage',action,*args]).stdout)

    def client(self, *args, offline=False, ok=True):
        invocation=['-m','client','--state-dir',str(self.client_state),*args]
        if offline:
            invocation=['-c',"import sys; from client.__main__ import main\ndef guard(event,args):\n if event=='socket.connect': raise OSError('verification offline')\nsys.addaudithook(guard)\nraise SystemExit(main())",'--state-dir',str(self.client_state),*args]
        result=bounded_run([sys.executable,*invocation],cwd=ROOT,timeout=60)
        if ok and result.returncode:
            raise RuntimeError('Client operation failed; exit '+str(result.returncode))
        body=json.loads(result.stdout)
        if offline:
            session_failure=body.get('ok') is False and body.get('error',{}).get('code')=='cloud_retryable'
            errors=body.get('errors')
            stream_failure=isinstance(errors,list) and bool(errors) and all(e.get('code')=='cloud_retryable' for e in errors)
            if result.returncode==0 or not (session_failure or stream_failure):
                raise AssertionError('Offline upload did not fail as expected')
        return body

    def preflight(self):
        root=self.config['remote_root']
        self.ssh(['/usr/local/sbin/w2f-v040-release','status'])
        code="import os,sys; assert os.environ['W2F_RELEASE']==sys.argv[1]"
        for role in ('app','sender'):
            self.remote(role,['python','-c',code,self.config['expected_release']])
        context=ssl.create_default_context(cafile=str(self.ca))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=context))
        with opener.open(self.config['url']+'/health/ready',timeout=15) as reply:
            assert json.load(reply)=={'status':'ok'}
        return dict(ready=True,release=self.config['expected_release'])

    def prepare(self, mode):
        if self.meta and self.meta['mode']!=mode:
            raise ValueError('Run mode is immutable')
        if self.meta and self.meta.get('prepared'):
            return self.status()
        if not self.meta:
            self.meta=dict(mode=mode,run_id=uuid.uuid4().hex,config=self.config,prepared=False)
            if mode=='synthetic':
                source=self.state/'source.sqlite3'
                message_fixture.initialize(source)
                reader=self.state/'reader'
                reader.write_text(f'#!{sys.executable}\nimport sys,json\nsys.path.insert(0,{str(Path(__file__).parent)!r})\nimport message_fixture as source\nwith source.connect({str(source)!r}) as db: result=source.read_page(db,sys.argv[1:])\nprint(json.dumps(result,ensure_ascii=False))\n')
                reader.chmod(0o700)
                account='v040-synthetic-'+self.meta['run_id']
                talker=message_fixture.TALKER
            else:
                reader=Path(self.config['reader'])
                account=self.config['source_account'];talker=self.config['source_talker']
            identity=self.client('init','--reader',str(reader),'--account',account)
            stream=self.client('enable','--talker',talker)
            self.meta.update(binding_id=str(uuid.uuid4()),device_id=identity['device_id'],stream_id=stream['id'],
                source_account=account,talker=talker,baseline=stream['read_cursor'],created_at=time.time())
            save(self.meta_path,self.meta)
        root=self.config['remote_root']
        self.ca.write_text(self.ssh(['cat',root+'/state/pki/ca.crt']).stdout)
        self.ca.chmod(0o600)
        self.preflight()
        remote_path='/tmp/w2f-live-'+self.meta['run_id']+'.json'
        if not self.credentials.exists():
            read=self.remote('app',['cat',remote_path],ok=False)
            if read.returncode:
                spec=dict(user_id=self.config['user_id'],device_id=self.meta['device_id'],source_account=self.meta['source_account'],
                    bindings=[dict(id=self.meta['binding_id'],stream_id=self.meta['stream_id'],talker=self.meta['talker'])])
                self.remote('app',['python','-m','cloud.manage','provision','--spec-file','-','--credentials-file',remote_path],json.dumps(spec))
                read=self.remote('app',['cat',remote_path])
            save(self.credentials,json.loads(read.stdout))
        self.client('connect','--credentials-file',str(self.credentials),'--url',self.config['url'],'--ca-file',str(self.ca))
        self.remote('app',['rm','-f',remote_path])
        self.manage('delivery-target','--binding',self.meta['binding_id'],'--app-id',self.config['app_id'],'--chat-id',self.config['chat_id'])
        self.meta['prepared']=True
        save(self.meta_path,self.meta)
        return self.status()

    def status(self):
        local=self.client('status')
        cloud=self.manage('delivery-status','--binding',self.meta['binding_id'])
        save(self.state/'status.json',dict(local=local,cloud=cloud))
        return dict(mode=self.meta['mode'],prepared=self.meta['prepared'],pending=local['pending_count'],
                    retained=local['retained_count'],delivery_enabled=cloud['delivery_enabled'])

    def collect(self, offline=False):
        self.client('collect')
        if offline:
            before=self.client('pending')
            if not before['events']:
                raise ValueError('Offline proof requires new pending input')
            result=self.client('upload',offline=True,ok=False)
            after=self.client('pending')
            assert before==after,'Offline upload changed pending input'
            save(self.state/'offline.json',dict(at=time.time(),result=result,pending=after,preserved=True))
        return self.status()

    def deliver(self):
        self.preflight()
        pending=self.client('pending')['events']
        if not 1<=len(pending)<=3:
            raise ValueError('Smoke delivery requires one to three explicit pending samples')
        evidence=self.state/('delivery-'+str(time.time_ns())+'.json')
        result=dict(result='failed',mode=self.meta['mode'],client_release=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                    cloud_release=self.config['expected_release'],events=pending,started_at=time.time(),readbacks=[],paused=False)
        save(evidence,result)
        try:
            self.manage('delivery-resume','--binding',self.meta['binding_id'])
            result['upload']=self.client('upload')
            assert result['upload'].get('errors')==[]
            deadline=time.monotonic()+45
            wanted={e['event_id'] for e in pending}
            while True:
                status=self.manage('delivery-status','--binding',self.meta['binding_id'])
                result['cloud']=status
                sent={j['event_id'] for j in status['jobs'] if j['state']=='sent'}
                if wanted<=sent:
                    break
                if not status['delivery_enabled'] or time.monotonic()>=deadline:
                    raise RuntimeError('Delivery incomplete; inspect private state')
                time.sleep(2)
            result['cloud']=status
            for event in pending:
                result['readbacks'].append(json.loads(self.remote('sender',['python','-m','cloud.verify',
                    '--binding',self.meta['binding_id'],'--event',event['event_id']]).stdout))
            assert all(r['verified'] for r in result['readbacks'])
            result['result']='api_verified'
        except BaseException as exc:
            result['error_type']=type(exc).__name__
            raise
        finally:
            try:
                result['paused']=not self.manage('delivery-pause','--binding',self.meta['binding_id'])['delivery_enabled']
            finally:
                result['finished_at']=time.time();save(evidence,result)
        return dict(result=result['result'],messages=len(pending),paused=result['paused'],evidence=str(evidence),human_confirmation='pending')

    def replay(self, evidence):
        prior=private_json(evidence)
        if prior['result']!='api_verified' or prior['mode']!=self.meta['mode']:
            raise ValueError('Use the successful evidence for this run')
        before=self.manage('delivery-status','--binding',self.meta['binding_id'])
        assert not before['delivery_enabled']
        credentials=private_json(self.credentials)
        context=ssl.create_default_context(cafile=str(self.ca))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=context))
        body={**{k:v for k,v in credentials.items() if k!='token'},'events':prior['events']}
        request=urllib.request.Request(self.config['url']+'/api/v1/events',data=json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+credentials['token'],'Content-Type':'application/json'})
        with opener.open(request,timeout=15) as reply:
            receipt=json.load(reply)
        assert {r['event_id'] for r in receipt['receipts']}=={e['event_id'] for e in prior['events']}
        after=self.manage('delivery-status','--binding',self.meta['binding_id'])
        assert before['jobs']==after['jobs'] and before['parts']==after['parts']
        save(self.state/'replay.json',dict(result='passed',events=[e['event_id'] for e in prior['events']],cloud=after))
        return dict(result='passed',new_jobs=0,new_attempts=0)


def main():
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGINT,interrupted)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--state',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare');p.add_argument('--mode',choices=['synthetic','wechat'],required=True)
    p=sub.add_parser('inject');p.add_argument('--text',required=True)
    p=sub.add_parser('collect');p.add_argument('--offline',action='store_true')
    p=sub.add_parser('replay');p.add_argument('--evidence',required=True)
    sub.add_parser('deliver');sub.add_parser('status')
    args=parser.parse_args()
    try:
        live=Live(args.config,args.state)
        if args.command=='prepare':result=live.prepare(args.mode)
        elif args.command=='collect':result=live.collect(args.offline)
        elif args.command=='replay':result=live.replay(args.evidence)
        elif args.command=='inject':
            if not live.meta or live.meta['mode']!='synthetic':raise ValueError('Only the synthetic reader accepts injection')
            message_fixture.inject(live.state/'source.sqlite3',args.text)
            result=live.collect()
        else:result=getattr(live,args.command)()
        print(json.dumps(result,ensure_ascii=False));return 0
    except Exception as exc:
        print(json.dumps(dict(result='failed',error_type=type(exc).__name__)))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
