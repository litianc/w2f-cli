"""win_store：本地微信库直读层。加密夹具库真实构建（sqlcipher），下载全部 mock，不触网。"""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlcipher3
import zstandard

_SPEC = importlib.util.spec_from_file_location(
    "win_store", Path(__file__).resolve().parents[1] / "integrations" / "win_store.py")
win_store = importlib.util.spec_from_file_location and importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(win_store)

EMOJI_BYTES = b'GIF89a' + b'fixture-sticker-bytes'
EMOJI_MD5 = hashlib.md5(EMOJI_BYTES).hexdigest()


def emoji_xml(cdn=True):
    url = f' cdnurl="http://fixture.invalid/{EMOJI_MD5[:8]}&amp;tail=1"' if cdn else ''
    return (f'<?xml version="1.0"?><msg><emoji md5="{EMOJI_MD5}" type="2" '
            f'len="{len(EMOJI_BYTES)}"{url}/></msg>')


class FixtureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.master = os.urandom(32)
        db_dir = self.root / 'db_storage' / 'message'
        db_dir.mkdir(parents=True)
        self.db_path = db_dir / 'message_0.db'
        con = sqlcipher3.connect(str(self.db_path))
        con.execute(f"PRAGMA key = \"x'{self.master.hex()}'\"")
        con.execute('CREATE TABLE Msg_fixture(local_id INTEGER, message_content BLOB)')
        con.execute('INSERT INTO Msg_fixture VALUES (1, ?)', (zstandard.ZstdCompressor().compress(emoji_xml().encode()),))
        con.commit(); con.close()
        self.keys_path = self.root / 'account_keys.json'
        self.keys_path.write_text(json.dumps({
            'wxid_fixture': {'db_key': self.master.hex(), 'image_aes_key': 'k' * 16,
                             'image_xor_key': '0x19',
                             'db_key_source_wxid_dir': str(self.root)}}), encoding='utf-8')
        patcher = patch.dict(os.environ, {'W2F_WDA_KEYS': str(self.keys_path)})
        patcher.start(); self.addCleanup(patcher.stop)

    def fake_urlopen(self, status_body):
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self, n=-1): return self.body
        return lambda request, timeout=60: Response(status_body)


class EmojiDownloadTest(FixtureBase):
    def test_downloads_via_cdnurl_and_verifies_digest(self):
        good = EMOJI_BYTES
        self.assertEqual(hashlib.md5(good).hexdigest(), EMOJI_MD5)
        with patch.object(win_store.urllib.request, 'urlopen', self.fake_urlopen(good)):
            data = win_store.emoji_download('wxid_fixture', EMOJI_MD5, db_key=self.master)
        self.assertEqual(data, good)

    def test_digest_mismatch_is_rejected(self):
        with patch.object(win_store.urllib.request, 'urlopen', self.fake_urlopen(b'GIF89a-tampered')):
            with self.assertRaises(win_store.WinStoreError):
                win_store.emoji_download('wxid_fixture', EMOJI_MD5, db_key=self.master)

    def test_no_cdnurl_anywhere_is_unresolvable(self):
        con = sqlcipher3.connect(str(self.db_path))
        con.execute(f"PRAGMA key = \"x'{self.master.hex()}'\"")
        con.execute('UPDATE Msg_fixture SET message_content=?',
                    (zstandard.ZstdCompressor().compress(emoji_xml(cdn=False).encode()),))
        con.commit(); con.close()
        with patch.object(win_store.urllib.request, 'urlopen', self.fake_urlopen(EMOJI_BYTES)):
            with self.assertRaisesRegex(win_store.WinStoreError, 'emoji_unresolvable'):
                win_store.emoji_download('wxid_fixture', EMOJI_MD5, db_key=self.master)

    def test_wrong_master_key_is_reported(self):
        with self.assertRaises(win_store.WinStoreError):
            win_store.emoji_download('wxid_fixture', EMOJI_MD5, db_key=os.urandom(32))


if __name__ == '__main__':
    unittest.main()
