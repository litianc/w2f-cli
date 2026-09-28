"""Regression fixtures contain generated pixels and synthetic identities only."""
import hashlib
import json
import struct
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from agents import wechat_feishu_mirror as mirror


def picture(fmt='PNG'):
    out = BytesIO()
    Image.new('RGB', (320, 160), '#72ab38').save(out, format=fmt)
    return out.getvalue()


def v2(data, key, xor=0x51, head=32, tail=16):
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    pad = 16 - head % 16
    first = encryptor.update(data[:head] + bytes([pad]) * pad) + encryptor.finalize()
    return b'\x07\x08V2\x08\x07' + struct.pack('<II', head, tail) + b'\x00' + first + data[head:len(data)-tail] + bytes(x ^ xor for x in data[len(data)-tail:])


class ImageResourceRegression(unittest.TestCase):
    def test_v2_decodes_original_pixels_and_exact_block_padding(self):
        from agents.media_images import decode_dat
        original = picture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.dat'
            for head in (31, 32):
                path.write_bytes(v2(original, b'0123456789abcdef', head=head))
                decoded = decode_dat(path, keys=[(b'0123456789abcdef', 0x51)])
                self.assertEqual(decoded, original)

    def test_v2_missing_key_and_corrupt_lengths_are_distinct(self):
        from agents.media_images import decode_dat, ImageResourceError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.dat'
            path.write_bytes(v2(picture(), b'0123456789abcdef'))
            with self.assertRaisesRegex(ImageResourceError, 'image_key_missing'):
                decode_dat(path)
            with self.assertRaisesRegex(ImageResourceError, 'decode_failed'):
                decode_dat(path, keys=[(b'wrong-key-1234567', 0x51)])
            path.write_bytes(b'\x07\x08V2\x08\x07' + struct.pack('<II', 2**32-1, 10) + b'\x00' + b'bad')
            with self.assertRaisesRegex(ImageResourceError, 'invalid_image_header'):
                decode_dat(path, keys=[(b'0123456789abcdef', 0x51)])

    def test_mac_key_derivation_requires_opt_in_and_validates_account(self):
        from agents.media_images import decode_dat, ImageResourceError
        original = picture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account = root/'xwechat_files'/'example_1234'
            path = account/'msg/attach/group/image.dat';path.parent.mkdir(parents=True)
            cache = root/'app_data/net/kvcomm';cache.mkdir(parents=True)
            (cache/'key_123456_report.statistic').touch()
            key = hashlib.md5(b'123456example').hexdigest()[:16].encode()
            path.write_bytes(v2(original, key, 123456 & 255))
            with patch('agents.media_images.sys.platform', 'darwin'):
                with self.assertRaisesRegex(ImageResourceError, 'image_key_missing'):
                    decode_dat(path)
                self.assertEqual(decode_dat(path, allow_macos_derivation=True), original)
                path.write_bytes(v2(original, b'0123456789abcdef'))
                with self.assertRaisesRegex(ImageResourceError, 'decode_failed'):
                    decode_dat(path, allow_macos_derivation=True)

    def test_dat_plain_and_legacy_xor_remain_supported(self):
        from agents.media_images import decode_dat
        original = picture('JPEG')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'image.dat'
            for data in (original, bytes(x ^ 0x37 for x in original)):
                path.write_bytes(data)
                self.assertEqual(decode_dat(path), original)

    def test_sticker_uses_content_when_text_is_display_placeholder(self):
        row = {'text':'[表情]', 'content':'<msg><emoji md5="'+'a'*32+'" cdnurl="https://example.test/sticker?a=1&amp;b=2"/></msg>'}
        self.assertEqual(mirror.sticker_url(row), 'https://example.test/sticker?a=1&b=2')

    def test_sticker_without_url_resolves_same_digest_in_same_group(self):
        data=picture('GIF');digest=hashlib.md5(data).hexdigest()
        donor={'talker':'test-room','kind_name':'sticker','content':f'<msg><emoji md5="{digest}" cdnurl="https://example.test/a"/></msg>'}
        result=SimpleNamespace(returncode=0,stdout=json.dumps({'data':{'messages':[donor]}}),stderr='')
        with tempfile.TemporaryDirectory() as directory:
            row={'talker':'test-room','local_id':35,'binding_id':2,'media_dir':directory,'content':f'<msg><emoji md5="{digest}"/></msg>'}
            with patch.object(mirror, 'reader_media_paths', return_value=[]), patch.object(mirror.subprocess,'run',return_value=result) as cli, patch.object(mirror.urllib.request,'urlopen',return_value=BytesIO(data)):
                path=mirror.stage_sticker(row)
            self.assertTrue(path)
            self.assertIn('--talker',cli.call_args.args[0]);self.assertIn('test-room',cli.call_args.args[0])
            with Image.open(path) as im:self.assertEqual(im.size,(200,100))

    def test_sticker_does_not_use_wrong_digest_or_other_group(self):
        donor={'talker':'other-room','kind_name':'sticker','content':'<msg><emoji md5="'+'a'*32+'" cdnurl="https://example.test/a"/></msg>'}
        result=SimpleNamespace(returncode=0,stdout=json.dumps({'data':{'messages':[donor]}}),stderr='')
        row={'talker':'test-room','local_id':35,'content':'<msg><emoji md5="'+'b'*32+'"/></msg>'}
        with patch.object(mirror,'reader_media_paths',return_value=[]),patch.object(mirror.subprocess,'run',return_value=result),patch.object(mirror.urllib.request,'urlopen') as download:
            with self.assertRaisesRegex(mirror.CliError,'sticker_resource_missing'):
                mirror.stage_sticker(row)
            download.assert_not_called()

    def test_sticker_checks_download_digest_and_does_not_leak_url(self):
        row={'content':'<msg><emoji md5="'+'a'*32+'" cdnurl="https://example.test/a?token=hidden"/></msg>'}
        with patch.object(mirror,'reader_media_paths',return_value=[]),patch.object(mirror.urllib.request,'urlopen',return_value=BytesIO(picture())):
            with self.assertRaisesRegex(mirror.CliError,'sticker_digest_mismatch') as error:
                mirror.stage_sticker(row)
            self.assertNotIn('hidden',str(error.exception))

    def test_animated_webp_remains_animated_without_upscaling(self):
        images=[Image.new('RGB',(32,24),color) for color in ('red','blue')]
        buf=BytesIO();images[0].save(buf,format='WEBP',save_all=True,append_images=images[1:],duration=[80,120],loop=0)
        data=buf.getvalue();digest=hashlib.md5(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            row={'media_dir':directory,'content':f'<msg><emoji md5="{digest}" cdnurl="https://example.test/a"/></msg>'}
            with patch.object(mirror.urllib.request,'urlopen',return_value=BytesIO(data)):
                path=mirror.stage_sticker(row)
            with Image.open(path) as im:
                self.assertEqual(im.n_frames,2);self.assertEqual(im.size,(32,24))

    def test_failed_sticker_retry_replies_once_and_keeps_name_snapshot(self):
        from agents.storage import MirrorStore
        data = picture('GIF'); digest = hashlib.md5(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);store = MirrorStore(root/'mirror.sqlite3')
            config = {'wechat_talker':'room','feishu_chat_id':'target','lark_profile':'test','media_dir':str(root/'media')}
            store.initialize(config)
            row = {'talker':'room','local_id':35,'sender_wxid':'sender','sender':'测试昵称','kind_name':'sticker',
                   'content':f'<msg><emoji md5="{digest}"/></msg>'}
            with patch.object(mirror, 'stage_sticker', side_effect=mirror.CliError('sticker_resource_missing')), patch.object(mirror,'run_lark',return_value={'data':{'message_id':'om_placeholder'}}):
                mirror.send_row(config,row,{},store)
            with patch.object(mirror,'reader_media_paths',return_value=[]), patch.object(mirror,'historical_sticker_sources',return_value=[('https://example.test/a',digest)]), patch.object(mirror.urllib.request,'urlopen',return_value=BytesIO(data)), patch.object(mirror,'run_lark',return_value={'data':{'message_id':'om_repaired'}}) as send:
                mirror.send_row({**config,'_retry_media':True},row,{},store)
                mirror.send_row({**config,'_retry_media':True},row,{},store)
            self.assertEqual(send.call_count,1)
            args=send.call_args.args[1]
            self.assertIn('--image',args);self.assertIn('+messages-reply',args);self.assertIn('om_placeholder',args)
            message=store.message('room:35',1)
            self.assertEqual(message['state'],'sent');self.assertEqual(message['sender_name'],'测试昵称')
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT state FROM media_jobs').fetchone()[0],'uploaded')



if __name__ == '__main__': unittest.main()
