"""Operator safety at the real client CLI and owned process boundaries."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
SCRIPTS=ROOT/'.agents/skills/verify-w2f/scripts'
sys.path.insert(0,str(SCRIPTS))
import live
import message_fixture
from w2f_protocol.ingest import VERSION


class LiveVerification(unittest.TestCase):
    def setUp(self):
        private=ROOT/'.v040-live'
        private.mkdir(exist_ok=True,mode=0o700)
        temporary=tempfile.TemporaryDirectory(prefix='unit-',dir=private)
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        config=self.root/'config.json'
        live.save(config,dict(ssh_host='test@localhost',ssh_port=22,expected_release='a'*40))
        self.run=live.Live(config,self.root/'run')
        self.addCleanup(self.run.lock.close)

    def test_offline_new_input_survives_real_client_session_failure(self):
        source=self.root/'source.sqlite3'
        message_fixture.initialize(source)
        reader=self.root/'reader'
        reader.write_text(f'#!{sys.executable}\nimport sys,json\nsys.path.insert(0,{str(SCRIPTS)!r})\nimport message_fixture as f\nwith f.connect({str(source)!r}) as db: result=f.read_page(db,sys.argv[1:])\nprint(json.dumps(result))\n')
        reader.chmod(0o700)
        identity=self.run.client('init','--reader',str(reader),'--account','synthetic-test')
        self.run.client('enable','--talker',message_fixture.TALKER)
        ca=self.root/'ca.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(self.root/'key'),
                        '-out',str(ca),'-days','1','-subj','/CN=localhost'],check=True,capture_output=True,timeout=15)
        live.save(self.run.credentials,dict(protocol=VERSION,device_id=identity['device_id'],
                  source_account='synthetic-test',auth_version=1,token='x'*40))
        self.run.client('connect','--credentials-file',str(self.run.credentials),'--url','https://127.0.0.1:9','--ca-file',str(ca))
        self.run.meta=dict(mode='synthetic',prepared=True,binding_id='synthetic-test')
        with patch.object(self.run,'manage',return_value={'delivery_enabled':False}):
            with self.assertRaises(ValueError):
                self.run.collect(offline=True)
            message_fixture.inject(source,'Offline boundary sample')
            status=self.run.collect(offline=True)
        self.assertEqual(status['pending'],1)
        evidence=json.loads((self.run.state/'offline.json').read_text())
        self.assertEqual(evidence['result']['error']['code'],'cloud_retryable')
        self.assertTrue(evidence['preserved'])
        self.assertEqual(evidence['pending'],self.run.client('pending'))
        self.assertEqual(self.run.client('retained')['events'],[])

    def test_prepared_input_mode_cannot_be_reinterpreted(self):
        self.run.meta=dict(mode='synthetic',prepared=True)
        with self.assertRaises(ValueError):
            self.run.prepare('wechat')

    def test_sigterm_pauses_binding_and_reaps_owned_command(self):
        script=self.root/'stop.py'
        script.write_text('''import json,os,signal,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import live
root=Path(sys.argv[2])
signal.signal(signal.SIGTERM,live.interrupted)
run=live.Live.__new__(live.Live)
run.state=root
run.meta={'mode':'synthetic','binding_id':'test'}
run.config={'expected_release':'a'*40}
run.preflight=lambda: None
run.client=lambda *args: {'events':[{'event_id':'test'}]}
def manage(action,*args):
 if action=='delivery-resume':
  live.bounded_run([sys.executable,'-c',"import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",str(root/'child.pid')])
 if action=='delivery-pause': return {'delivery_enabled':False}
run.manage=manage
try: run.deliver()
except live.VerificationStopped: pass
''')
        process=subprocess.Popen([sys.executable,str(script),str(SCRIPTS),str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while not (self.root/'child.pid').exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(),deadline)
                time.sleep(.05)
            child=int((self.root/'child.pid').read_text())
            process.send_signal(signal.SIGTERM)
            out,err=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0,err)
            with self.assertRaises(ProcessLookupError):os.kill(child,0)
            evidence=json.loads(next(self.root.glob('delivery-*.json')).read_text())
            self.assertTrue(evidence['paused'])
            self.assertEqual(evidence['result'],'failed')
            self.assertEqual(evidence['error_type'],'VerificationStopped')
        finally:
            if process.poll() is None:
                process.kill();process.communicate(timeout=5)
            if (self.root/'child.pid').exists():
                try:os.kill(int((self.root/'child.pid').read_text()),signal.SIGKILL)
                except ProcessLookupError:pass


if __name__=='__main__':unittest.main()
