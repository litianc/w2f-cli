"""win_keys：WDA 捕获并保存的账号密钥读取层（Windows 底层读取的钥匙来源）。

密钥内容（db_key/image_aes_key）绝不写入测试、日志或输出——测试只用合成夹具。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.win_keys import WinKeysError, default_keys_path, keys_for

DB_KEY = 'ab' * 32
AES_KEY = '410c6a4b5d6e7f8091a2b3c4d5e6f708'
FIXTURE = {
    'wxid_demo_290e': {'db_key': DB_KEY, 'image_aes_key': AES_KEY,
                       'image_xor_key': '0x19', 'image_key_verified': True},
    'wxid_other': {'db_key': 'cd' * 32, 'image_aes_key': AES_KEY, 'image_xor_key': '0x19'},
    'wxid_broken': {'db_key': 'zz'},
}


def write_keys(tmp, data):
    path = Path(tmp) / 'account_keys.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


class KeysForTest(unittest.TestCase):
    def test_exact_account_match_normalizes(self):
        with tempfile.TemporaryDirectory() as td:
            keys = keys_for('wxid_demo_290e', path=write_keys(td, FIXTURE))
        self.assertEqual(keys['db_key'], bytes.fromhex(DB_KEY))
        self.assertEqual(len(keys['db_key']), 32)
        self.assertEqual(keys['image_aes_key'], AES_KEY)
        self.assertEqual(keys['image_xor_key'], 25)

    def test_suffix_variant_matches_by_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            keys = keys_for('wxid_demo', path=write_keys(td, FIXTURE))
        self.assertEqual(keys['db_key'], bytes.fromhex(DB_KEY))

    def test_missing_account_raises_actionable_error(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaisesRegex(WinKeysError, 'wxid_none'):
            keys_for('wxid_none', path=write_keys(td, FIXTURE))

    def test_invalid_db_key_raises(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaisesRegex(WinKeysError, 'db_key'):
            keys_for('wxid_broken', path=write_keys(td, FIXTURE))

    def test_default_path_honors_env_override(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {'W2F_WDA_KEYS': str(write_keys(td, FIXTURE))}):
                self.assertEqual(default_keys_path(), Path(td) / 'account_keys.json')
                keys = keys_for('wxid_other')
        self.assertEqual(keys['image_xor_key'], 25)


if __name__ == '__main__':
    unittest.main()
