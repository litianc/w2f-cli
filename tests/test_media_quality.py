"""Original/preview quality must survive the reader-to-message boundary."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore


class MediaQualityTest(unittest.TestCase):
    def test_broken_primary_original_does_not_hide_attachment_original(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken, original, preview = root / 'broken.dat', root / 'original.png', root / 'preview.png'
            broken.write_bytes(b'broken-image')
            Image.new('RGB', (320, 160), 'blue').save(original)
            Image.new('RGB', (16, 8), 'blue').save(preview)
            row = {'talker': 'fixture-room', 'local_id': 8, 'server_id': 456,
                   'create_time': 1234, 'kind_name': 'image', 'media_dir': str(root / 'stage')}
            def reader(cmd, **kwargs):
                data = {'resources': [{'local_path': str(broken), 'thumbnail_only': False},
                                      {'local_path': str(preview), 'thumbnail_only': True}]}
                if 'image-resource' in cmd:
                    data = {'contract': 'image_resource_v1', 'talker': 'fixture-room',
                            'local_id': 8, 'server_id': 456, 'create_time': 1234,
                            'resources': [{'local_path': str(original), 'thumbnail_only': False}]}
                return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({'data': data}))
            with patch.object(mirror.subprocess, 'run', side_effect=reader):
                path, degraded = mirror.stage_media(row, 'image')
            with Image.open(path) as image:
                self.assertEqual(image.size, (320, 160))
            self.assertFalse(degraded)

    def test_preview_index_does_not_hide_exact_original(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, preview = root / 'original.png', root / 'preview.png'
            Image.new('RGB', (320, 160), 'blue').save(original)
            Image.new('RGB', (16, 8), 'blue').save(preview)
            row = {'talker': 'fixture-room', 'local_id': 8, 'server_id': 456,
                   'create_time': 1234, 'kind_name': 'image', 'media_dir': str(root / 'stage')}
            def reader(cmd, **kwargs):
                data = {'resources': [{'local_path': str(preview), 'thumbnail_only': True}]}
                if 'image-resource' in cmd:
                    data = {'contract': 'image_resource_v1', 'talker': 'fixture-room',
                            'local_id': 8, 'server_id': 456, 'create_time': 1234,
                            'resources': [{'local_path': str(original), 'thumbnail_only': False}]}
                return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({'data': data}))
            with patch.object(mirror.subprocess, 'run', side_effect=reader):
                path, degraded = mirror.stage_media(row, 'image')
            with Image.open(path) as image:
                self.assertEqual(image.size, (320, 160))
            self.assertFalse(degraded)

    def test_explicit_original_beats_larger_preview_without_degradation_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original.jpg'
            preview = root / 'preview.jpg'
            original.write_bytes(b'original')
            preview.write_bytes(b'preview' * 30)
            resources = [
                {'local_paths': [str(preview)], 'thumbnail_only': True},
                {'local_paths': [str(original)], 'thumbnail_only': False},
            ]
            result = SimpleNamespace(returncode=0, stdout=json.dumps({'data': {'resources': resources}}), stderr='')
            with patch.object(mirror.subprocess, 'run', return_value=result):
                staged, degraded = mirror.stage_media({'talker': 'room', 'local_id': 1, 'media_dir': str(root / 'media')}, 'image')
            self.assertEqual(Path(staged).read_bytes(), b'original')
            self.assertFalse(degraded)
            mirror.cleanup_staged(staged)

    def test_preview_lookup_rejects_original_from_a_different_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview, original = root / 'preview.jpg', root / 'original.jpg'
            preview.write_bytes(b'preview'); original.write_bytes(b'wrong-original')
            row = {'talker': 'fixture-room', 'local_id': 8, 'server_id': 456,
                   'create_time': 1234, 'kind_name': 'image', 'media_dir': str(root / 'stage')}
            for changed in ({'talker': 'another-room'}, {'local_id': 9}, {'server_id': 457},
                            {'create_time': 1235}, {'contract': 'other-contract'}):
                with self.subTest(changed=changed):
                    def reader(cmd, **kwargs):
                        data = {'resources': [{'local_path': str(preview), 'thumbnail_only': True}]}
                        if 'image-resource' in cmd:
                            data = {'contract': 'image_resource_v1', 'talker': 'fixture-room',
                                    'local_id': 8, 'server_id': 456, 'create_time': 1234, **changed,
                                    'resources': [{'local_path': str(original), 'thumbnail_only': False}]}
                        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({'data': data}))
                    with patch.object(mirror.subprocess, 'run', side_effect=reader):
                        path, degraded = mirror.stage_media(row, 'image')
                    self.assertEqual(Path(path).read_bytes(), b'preview')
                    self.assertTrue(degraded)


    def test_missing_original_sends_preview_with_sender_and_explicit_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preview = root / 'preview.jpg'
            preview.write_bytes(b'preview-only')
            store = MirrorStore(root / 'state.db')
            config = {'wechat_talker': 'room', 'feishu_chat_id': 'chat', 'lark_profile': 'fixture',
                      'image_original_wait_seconds': 0, 'image_original_auto_retry': False,
                      'sender_map': {'sender': 'Confirmed name'}, 'media_dir': str(root / 'media')}
            store.initialize(config)
            resources = [
                {'local_paths': [str(root / 'missing.jpg')], 'thumbnail_only': False},
                {'local_path': str(preview), 'thumbnail_only': True},
            ]
            result = SimpleNamespace(returncode=0, stdout=json.dumps({'data': {'resources': resources}}), stderr='')
            texts, uploads = [], []
            def send(config, args):
                if '--text' in args:
                    texts.append(args[args.index('--text') + 1])
                if '--image' in args:
                    uploads.append(Path(args[args.index('--image') + 1]).read_bytes())
                return {'data': {'message_id': f'om_fixture_{len(texts) + len(uploads)}'}}
            row = {'talker': 'room', 'local_id': 8, 'sender_wxid': 'sender', 'kind_name': 'image'}
            with patch.object(mirror.subprocess, 'run', return_value=result), patch.object(mirror, 'run_lark', side_effect=send):
                mirror.send_row(config, row, {}, store)
                mirror.send_row(config, row, {}, store)
            self.assertEqual(texts, ['Confirmed name：（原图未同步，已发送缩略图）'])
            self.assertEqual(uploads, [b'preview-only'])
            self.assertEqual(store.message('room:8', 1)['state'], 'sent')

    def test_missing_preview_does_not_mark_available_original_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original.jpg'
            original.write_bytes(b'original')
            result = SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({'data': {'resources': [
                {'local_paths': [str(original)], 'thumbnail_only': False},
                {'local_paths': [str(Path(directory) / 'missing.jpg')], 'thumbnail_only': True}
            ]}}))
            with patch.object(mirror.subprocess, 'run', return_value=result):
                paths, degraded = mirror.reader_media_result({'talker': 'room', 'local_id': 8})
            self.assertEqual(paths, [original])
            self.assertFalse(degraded)

    def test_unusable_original_falls_back_to_available_preview(self):
        for contents in (b'not-a-wechat-dat', b''):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original, preview = root / 'original.dat', root / 'preview.jpg'
                original.write_bytes(contents)
                preview.write_bytes(b'available-preview')
                result = SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({'data': {'resources': [
                    {'local_paths': [str(original)], 'thumbnail_only': False},
                    {'local_paths': [str(preview)], 'thumbnail_only': True}
                ]}}))
                with patch.object(mirror.subprocess, 'run', return_value=result):
                    path, degraded = mirror.stage_media({'talker': 'room', 'local_id': 9, 'media_dir': str(root / 'media')}, 'image')
                self.assertEqual(Path(path).read_bytes(), b'available-preview')
                self.assertTrue(degraded)

    def test_all_candidates_broken_keep_decode_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, preview = root / 'original.dat', root / 'preview.dat'
            original.write_bytes(b'not-a-wechat-dat')
            preview.write_bytes(b'broken-preview')
            result = SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({'data': {'resources': [
                {'local_paths': [str(original)], 'thumbnail_only': False},
                {'local_paths': [str(preview)], 'thumbnail_only': True}
            ]}}))
            with patch.object(mirror.subprocess, 'run', return_value=result), self.assertRaisesRegex(mirror.CliError, 'decode_failed'):
                mirror.stage_media({'talker': 'room', 'local_id': 9, 'media_dir': str(root / 'media')}, 'image')


if __name__ == '__main__':
    unittest.main()
