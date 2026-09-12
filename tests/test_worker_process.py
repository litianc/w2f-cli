"""Acceptance test using OS processes and real flock/SQLite, fake remote CLI."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from agents.storage import MirrorStore
from agents.paths import ROOT


class WorkerProcessAcceptance(unittest.TestCase):
    def test_T52_duplicate_start_stop_resume_and_offline_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bin_dir=root/'bin';bin_dir.mkdir();source=root/'source.json';sink=root/'sent.jsonl'
            reader=bin_dir/'reader';reader.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
a=sys.argv;get=lambda flag,default: a[a.index(flag)+1] if flag in a else default
cursor=int(get('--after-local-id',0));rows=json.loads(Path(os.environ['TEST_SOURCE']).read_text());high=int(get('--through-local-id',max([r['local_id'] for r in rows],default=cursor)))
rows=[r for r in rows if cursor<r['local_id']<=high]
print(json.dumps({'ok':True,'data':{'contract':'ordered_sync_v1','messages':rows,'through_local_id':high,'has_more':False,'next':None}}))
''');reader.chmod(0o755)
            lark=bin_dir/'lark-cli';lark.write_text('''#!/usr/bin/env python3
import json,os,sys,fcntl
from pathlib import Path
if sys.argv[1:3]==['profile','list']:
 print(json.dumps([{'name':'test','appId':'app_test'}]));sys.exit()
args=sys.argv
with open(os.environ['TEST_SINK'],'a+') as f:
 fcntl.flock(f,fcntl.LOCK_EX);f.seek(0);rows=[json.loads(line) for line in f if line.strip()]
 key=args[args.index('--idempotency-key')+1];old=next((r for r in rows if r['key']==key),None)
 if old is None:
  old={'key':key,'text':args[args.index('--text')+1],'message_id':'om_'+str(len(rows))};f.write(json.dumps(old)+'\\n');f.flush()
 print(json.dumps({'ok':True,'data':{'message_id':old['message_id']}}))
''');lark.chmod(0o755)
            def rows(n):return [{'talker':'room','local_id':i,'create_time':i,'kind_name':'text','sender_wxid':'wx','sender':'昵称','text':f'离线第{i}条 😀'} for i in range(11,n+1)]
            source.write_text(json.dumps(rows(12)));cfg=root/'config.json';db=root/'state.db'
            config={'binding_id':1,'source_account':'demo','wechat_chat_name':'源','wechat_talker':'room','feishu_chat_name':'目标','feishu_chat_id':'chat','lark_profile':'test','lark_app_id':'app_test','initial_cursor':10,'sender_map':{'wx':'实名'},'sqlite_file':str(db),'data_dir':str(root),'log_dir':str(root/'logs'),'poll_interval':0.2}
            cfg.write_text(json.dumps(config));store=MirrorStore(db);store.initialize(config)
            env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],'WECHAT_READER':str(reader),'TEST_SOURCE':str(source),'TEST_SINK':str(sink)}
            def command(op):
                p=subprocess.run([sys.executable,'-m','agents.wechat_feishu_mirror',op,'--config',str(cfg)],cwd=ROOT,env=env,text=True,capture_output=True,timeout=20)
                if p.returncode:raise AssertionError(p.stderr+p.stdout)
                return json.loads(p.stdout)
            def until(predicate):
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    if predicate():return
                    time.sleep(.05)
                raise AssertionError((root/'logs/binding-1.log').read_text())
            try:
                with ThreadPoolExecutor(2) as pool:starts=list(pool.map(command,['start','start']))
                self.assertEqual(starts[0]['worker']['pid'],starts[1]['worker']['pid'])
                until(lambda:store.get_cursor()==12)
                command('stop');until(lambda:not store.status()['worker']['alive'])
                source.write_text(json.dumps(rows(16)))
                before=sink.read_text();time.sleep(.3);self.assertEqual(sink.read_text(),before)
                command('start');until(lambda:store.get_cursor()==16)
                sent=[json.loads(line) for line in sink.read_text().splitlines()]
                self.assertEqual([r['text'] for r in sent],[f'实名：离线第{i}条 😀' for i in range(11,17)])
                self.assertEqual(len({r['key'] for r in sent}),6)
            finally:
                command('stop');until(lambda:not store.status()['worker']['alive'])
