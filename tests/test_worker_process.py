"""Acceptance test using OS processes and real file locks/SQLite, fake remote CLI."""
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

READER_BODY = '''
import json,os,sys
from pathlib import Path
a=sys.argv;get=lambda flag,default: a[a.index(flag)+1] if flag in a else default
cursor=int(get('--after-local-id',0));rows=json.loads(Path(os.environ['TEST_SOURCE']).read_text(encoding='utf-8'));high=int(get('--through-local-id',max([r['local_id'] for r in rows],default=cursor)))
rows=[r for r in rows if cursor<r['local_id']<=high]
print(json.dumps({'ok':True,'data':{'contract':'ordered_sync_v1','messages':rows,'through_local_id':high,'has_more':False,'next':None}}))
'''

# The fake lark-cli guards its sink with a real exclusive lock; the lock API
# itself is platform-specific, so the script picks one at runtime.
LARK_BODY = '''
import json,os,sys
from pathlib import Path
if sys.platform=='win32':
 import msvcrt
 def _lock(f):
  f.seek(0)
  while True:
   try:
    msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1);return
   except OSError:
    pass
else:
 import fcntl
 def _lock(f):
  fcntl.flock(f,fcntl.LOCK_EX)
if sys.argv[1:3]==['profile','list']:
 print(json.dumps([{'name':'test','appId':'app_test'}]));sys.exit()
args=sys.argv
with open(os.environ['TEST_SINK'],'a+',encoding='utf-8') as f:
 _lock(f);f.seek(0);rows=[json.loads(line) for line in f if line.strip()]
 key=args[args.index('--idempotency-key')+1];old=next((r for r in rows if r['key']==key),None)
 if old is None:
  old={'key':key,'text':args[args.index('--text')+1],'message_id':'om_'+str(len(rows))};f.write(json.dumps(old)+'\\n');f.flush()
 print(json.dumps({'ok':True,'data':{'message_id':old['message_id']}}))
'''


def install_mock_cli(bin_dir: Path, name: str, body: str) -> Path:
    """Materialize one fake CLI under bin_dir for the current platform.

    POSIX gets a shebang script; Windows cannot exec extensionless scripts,
    so it gets a .py payload plus a .cmd shim that shutil.which() resolves.
    """
    if sys.platform == 'win32':
        payload = bin_dir / f'{name}.py'
        payload.write_text(body, encoding='utf-8')
        shim = bin_dir / f'{name}.cmd'
        shim.write_text(f'@"{sys.executable}" "%~dp0{payload.name}" %*\r\n', encoding='ascii')
        return shim
    script = bin_dir / name
    script.write_text(f'#!/usr/bin/env python3{body}')
    script.chmod(0o755)
    return script


class WorkerProcessAcceptance(unittest.TestCase):
    def test_T52_duplicate_start_stop_resume_and_offline_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bin_dir=root/'bin';bin_dir.mkdir();source=root/'source.json';sink=root/'sent.jsonl'
            reader=install_mock_cli(bin_dir,'reader',READER_BODY)
            lark=install_mock_cli(bin_dir,'lark-cli',LARK_BODY)
            def rows(n):return [{'talker':'room','local_id':i,'create_time':i,'kind_name':'text','sender_wxid':'wx','sender':'昵称','text':f'离线第{i}条 😀'} for i in range(11,n+1)]
            source.write_text(json.dumps(rows(12)),encoding='utf-8');cfg=root/'config.json';db=root/'state.db'
            config={'binding_id':1,'source_account':'demo','wechat_chat_name':'源','wechat_talker':'room','feishu_chat_name':'目标','feishu_chat_id':'chat','lark_profile':'test','lark_app_id':'app_test','initial_cursor':10,'sender_map':{'wx':'实名'},'sqlite_file':str(db),'data_dir':str(root),'log_dir':str(root/'logs'),'poll_interval':0.2}
            cfg.write_text(json.dumps(config),encoding='utf-8');store=MirrorStore(db);store.initialize(config)
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
                raise AssertionError((root/'logs/binding-1.log').read_text(encoding='utf-8',errors='replace'))
            try:
                with ThreadPoolExecutor(2) as pool:starts=list(pool.map(command,['start','start']))
                self.assertEqual(starts[0]['worker']['pid'],starts[1]['worker']['pid'])
                until(lambda:store.get_cursor()==12)
                command('stop');until(lambda:not store.status()['worker']['alive'])
                source.write_text(json.dumps(rows(16)),encoding='utf-8')
                before=sink.read_text(encoding='utf-8');time.sleep(.3);self.assertEqual(sink.read_text(encoding='utf-8'),before)
                command('start');until(lambda:store.get_cursor()==16)
                sent=[json.loads(line) for line in sink.read_text(encoding='utf-8').splitlines()]
                self.assertEqual([r['text'] for r in sent],[f'实名：离线第{i}条 😀' for i in range(11,17)])
                self.assertEqual(len({r['key'] for r in sent}),6)
            finally:
                command('stop');until(lambda:not store.status()['worker']['alive'])
