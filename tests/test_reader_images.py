"""Exact attachment identity fallback; synthetic groups and cache files only."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from agents import wechat_feishu_mirror as mirror
from contextlib import closing


def _symlink_privilege():
    try:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'probe-target'; target.write_bytes(b'x')
            link = Path(directory) / 'probe-link'
            link.symlink_to(target)
            return True
    except (OSError, NotImplementedError):
        return False


SYMLINK_OK = _symlink_privilege()


class ReaderImagesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.group = hashlib.md5(b'fixture-room').hexdigest()
        self.digest = 'b' * 32
        self.path = self.root / 'message.db'
        self.table = 'Msg_' + self.group
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute(f'CREATE TABLE "{self.table}" (local_id INTEGER,server_id INTEGER,create_time INTEGER,local_type INTEGER,packed_info_data BLOB)')
            db.execute(f'INSERT INTO "{self.table}" VALUES (54,456,1234,3,?)', (b'\x08\x02\x1a\x22\x22\x20' + self.digest.encode(),))
        db.close()
        def connect(path):
            c = sqlite3.connect(path); c.row_factory = sqlite3.Row; return c
        self.db = SimpleNamespace(message_dbs=[self.path],resource_roots=[self.root/'msg'],connect=connect)
        self.reader = SimpleNamespace(resolve_username=lambda db,t:t,message_table=lambda t:'Msg_'+hashlib.md5(t.encode()).hexdigest(),ReaderError=lambda text,code:RuntimeError(code))
        self.args = SimpleNamespace(talker='fixture-room',local_id=54,server_id=456,create_time=1234)
        self.directory = self.root/'msg'/'attach'/self.group/'2026-09'/'Img'
        self.directory.mkdir(parents=True)
    def read(self):
        from integrations.reader_images import read_images
        return read_images(self.reader,self.db,self.args)
    def test_missing_index_uses_exact_packed_digest_and_marks_preview(self):
        original = self.directory/(self.digest+'.dat');original.write_bytes(b'original')
        preview = self.directory/(self.digest+'_t.dat');preview.write_bytes(b'preview')
        self.directory.joinpath('c'*32+'.dat').write_bytes(b'unrelated')
        result = self.read()
        self.assertEqual([(r['local_paths'],r['thumbnail_only']) for r in result['resources']], [([str(original.resolve())],False),([str(preview.resolve())],True)])
    def test_rejects_wrong_server_time_or_kind(self):
        for field,value in [('server_id',999),('create_time',999),('local_type',47)]:
            with self.subTest(field=field), sqlite3.connect(self.path) as db:
                db.execute(f'UPDATE "{self.table}" SET {field}=?',(value,))
            db.close()
            with self.assertRaisesRegex(RuntimeError,'image_identity_mismatch'):self.read()
            with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
                db.execute(f'UPDATE "{self.table}" SET server_id=456,create_time=1234,local_type=3')
            db.close()
    def test_other_group_and_similar_names_are_never_used(self):
        other = self.root/'msg'/'attach'/('f'*32)/'2026-09'/'Img';other.mkdir(parents=True)
        (other/(self.digest+'.dat')).write_bytes(b'other room')
        (self.directory/(self.digest+'-other.dat')).write_bytes(b'wrong file')
        self.assertEqual(self.read()['resources'],[])
    @unittest.skipIf(not SYMLINK_OK, 'Windows 需管理员/开发者模式才能创建符号链接')
    def test_symlink_outside_group_is_not_a_resource(self):
        outside=self.root/'private.dat';outside.write_bytes(b'other image')
        (self.directory/(self.digest+'.dat')).symlink_to(outside)
        self.assertEqual(self.read()['resources'],[])
    def test_truncated_packed_metadata_does_not_guess_from_text(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute(f'UPDATE "{self.table}" SET packed_info_data=?',(b'\x1a\xff'+self.digest.encode(),))
        db.close()
        with self.assertRaisesRegex(RuntimeError,'image_metadata_invalid'):self.read()
    def test_worker_uses_fallback_and_preserves_original_preview_preference(self):
        original=self.directory/(self.digest+'.dat');original.write_bytes(b'invalid dat')
        preview=self.directory/(self.digest+'_t.dat');preview.write_bytes(b'invalid dat')
        from PIL import Image
        Image.new('RGB',(12,8),'green').save(preview,format='PNG')
        def run(cmd,**kwargs):
            payload={'data':self.read()} if 'image-resource' in cmd else {'data':{'resources':[]}}
            return SimpleNamespace(returncode=0,stdout=json.dumps(payload),stderr='')
        row={'talker':'fixture-room','local_id':54,'server_id':456,'create_time':1234,'kind_name':'image','media_dir':str(self.root/'staged')}
        with patch.object(mirror.subprocess,'run',side_effect=run):
            path,degraded=mirror.stage_media(row,'image')
        self.assertTrue(degraded)
        with Image.open(path) as image:self.assertEqual(image.size,(12,8))
