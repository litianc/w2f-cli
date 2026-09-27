import argparse
import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from integrations.reader_voice import read_voice
from agents import wechat_feishu_mirror as mirror


class VoiceResourcesTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'media_1.db'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.executemany('INSERT INTO Name2Id VALUES(?)',[('room',),('other-room',)])
            db.execute('CREATE TABLE VoiceInfo(chat_name_id INTEGER,local_id INTEGER,svr_id INTEGER,create_time INTEGER,voice_data BLOB)')
        db.close()
        def connect(path):
            conn=sqlite3.connect(path);conn.row_factory=sqlite3.Row;return conn
        self.db=SimpleNamespace(message_dbs=[self.root/'message_1.db'],connect=connect)
        self.reader=SimpleNamespace(resolve_username=lambda db,t:t,ReaderError=lambda text,code:RuntimeError(code))
        self.args=argparse.Namespace(talker='room',local_id=26,server_id=99,create_time=1234)
        self.voice=b'\x02#!SILK_V3\x01\x00\x00'
    def insert(self,room=1,local=26,server=0,created=1234,voice=None):
        with sqlite3.connect(self.path) as db:db.execute('INSERT INTO VoiceInfo VALUES(?,?,?,?,?)',(room,local,server,created,voice or self.voice))
        db.close()
    def test_outgoing_zero_server_exact_group_local_time(self):
        self.insert();self.insert(room=2,voice=self.voice+b'other')
        value=read_voice(self.reader,self.db,self.args)
        self.assertEqual(base64.b64decode(value['base64']),self.voice)
        self.assertEqual(value['extension'],'.silk')
    def test_received_uses_server_and_never_guesses_conflicting_server(self):
        self.insert(local=99,server=99,created=1)
        self.assertEqual(read_voice(self.reader,self.db,self.args)['local_id'],26)
        with sqlite3.connect(self.path) as db:db.execute('DELETE FROM VoiceInfo')
        db.close();self.insert(server=88)
        with self.assertRaisesRegex(RuntimeError,'voice_resource_missing'):read_voice(self.reader,self.db,self.args)
    def test_missing_timestamp_group_and_duplicate_blobs_do_not_mix(self):
        self.insert(created=1235);self.insert(room=2)
        with self.assertRaisesRegex(RuntimeError,'voice_resource_missing'):read_voice(self.reader,self.db,self.args)
        self.insert();self.insert();self.insert()  # Identical shard copies are safe.
        self.assertEqual(read_voice(self.reader,self.db,self.args)['extension'],'.silk')
        self.insert(voice=self.voice+b'conflict')
        with self.assertRaisesRegex(RuntimeError,'voice_ambiguous'):read_voice(self.reader,self.db,self.args)
    def test_unreadable_other_shard_does_not_claim_uniqueness(self):
        self.insert()
        (self.root/'media_2.db').write_bytes(b'unreadable database')
        with self.assertRaisesRegex(RuntimeError,'voice_database_unreadable'):
            read_voice(self.reader,self.db,self.args)

    def test_worker_validates_resource_identity_and_stages_private_file(self):
        self.insert();value=read_voice(self.reader,self.db,self.args)
        row={'talker':'room','local_id':26,'server_id':99,'create_time':1234}
        result=SimpleNamespace(returncode=0,stdout=json.dumps({'data':value}))
        with patch.object(mirror.subprocess,'run',return_value=result):
            path=mirror.stage_voice_resource(row,self.root,'31-roomhash')
        self.assertEqual(Path(path).read_bytes(),self.voice)
        if sys.platform!='win32':self.assertEqual(Path(path).stat().st_mode & 0o777,0o600)
        value['talker']='other-room';result.stdout=json.dumps({'data':value})
        with patch.object(mirror.subprocess,'run',return_value=result):
            self.assertIsNone(mirror.stage_voice_resource(row,self.root,'31-roomhash'))
    def test_native_decoder_failure_falls_back_to_file_and_cleans_pcm(self):
        source=self.root/'voice.silk';source.write_bytes(self.voice)
        source.with_suffix('.pcm').write_bytes(b'partial')
        result=SimpleNamespace(returncode=1)
        with patch.object(mirror.shutil,'which',return_value='ffmpeg'),patch.object(mirror.subprocess,'run',return_value=result):
            args,cleanup=mirror.voice_upload_args(str(source))
        self.assertEqual(args,['--file',str(source)]);self.assertIsNone(cleanup)
        self.assertEqual(source.with_suffix('.pcm').read_bytes(),b'partial')
        self.assertEqual(list(self.root.glob('*-decode-*')),[])
    def test_generated_silk_converts_to_playable_opus(self):
        try:import pilk
        except ImportError:self.skipTest('optional pilk unavailable')
        if not mirror.shutil.which('ffmpeg'):self.skipTest('optional ffmpeg unavailable')
        import math,struct
        pcm=self.root/'synthetic.pcm'
        pcm.write_bytes(b''.join(struct.pack('<h',int(5000*math.sin(i*2*math.pi*440/24000))) for i in range(12000)))
        silk=self.root/'synthetic.silk';pilk.encode(str(pcm),str(silk),pcm_rate=24000,tencent=True)
        args,cleanup=mirror.voice_upload_args(str(silk))
        self.assertEqual(args[0],'--audio')
        self.assertTrue(Path(args[1]).read_bytes().startswith(b'OggS'))
        self.assertTrue(pcm.exists())
        if sys.platform!='win32':self.assertEqual(Path(args[1]).stat().st_mode & 0o777,0o600)
        self.assertEqual(list(self.root.glob('*-decode-*.pcm')),[])
        mirror.cleanup_staged(cleanup)

if __name__=='__main__':unittest.main()
