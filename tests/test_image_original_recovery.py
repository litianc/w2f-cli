"""Preview delivery and explicit original recovery through the manager/worker seam."""
import json
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from agents import wechat_feishu_mirror as mirror
from agents.manage import Manager
from agents.storage import MirrorStore


class OriginalRecoveryFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / 'original.png'
        self.preview = self.root / 'preview.png'
        Image.new('RGB', (320, 160), 'blue').save(self.original)
        Image.new('RGB', (16, 8), 'blue').save(self.preview)
        self.original_available = False
        self.config = {'binding_id': 1, 'wechat_talker': 'fixture-room',
                       'feishu_chat_id': 'oc_fixture', 'lark_profile': 'fixture',
                       'image_original_wait_seconds': 0, 'image_original_auto_retry': False,
                       'sender_map': {'wx_fixture': '原姓名'}, 'media_dir': str(self.root / 'media')}
        self.store = MirrorStore(self.root / 'state.db')
        self.store.initialize(self.config)
        cfg = self.root / 'config.json'
        cfg.write_text('{}')
        self.manager = Manager(self.store, cfg)
        self.row = {'talker': 'fixture-room', 'local_id': 8, 'server_id': 456,
                    'create_time': 1234, 'sender_wxid': 'wx_fixture', 'kind_name': 'image'}
        self.sent = []
        self.addCleanup(patch.stopall)
        patch.object(mirror.subprocess, 'run', side_effect=self.reader).start()
        patch.object(mirror, 'run_lark', side_effect=self.send).start()

    def reader(self, cmd, **kwargs):
        resources = [{'local_path': str(self.preview), 'thumbnail_only': True}]
        if self.original_available:
            resources.append({'local_path': str(self.original), 'thumbnail_only': False})
        data = {'resources': resources}
        if 'image-resource' in cmd:
            data.update(contract='image_resource_v1', talker='fixture-room', local_id=8,
                        server_id=456, create_time=1234)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'data': data}), stderr='')

    def send(self, config, args):
        value = {'args': args, 'chat_id': config['feishu_chat_id']}
        if '--image' in args:
            path = Path(args[args.index('--image') + 1])
            value['bytes'] = path.read_bytes()
            with Image.open(path) as image:
                value['dimensions'] = image.size
        self.sent.append(value)
        return {'data': {'message_id': f'om_fixture_{len(self.sent)}'}}


class OriginalRecoveryTest(OriginalRecoveryFixture, unittest.TestCase):
    def test_late_original_is_manually_replied_once_without_repeating_preview_or_name(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(self.sent[1]['dimensions'], (16, 8))
        pending = self.manager.get('media-failures')
        self.assertEqual(len(pending), 1, 'A delivered preview must remain recoverable in the console')
        self.original_available = True
        self.manager.post('media-failures', {'op': 'retry', 'id': pending[0]['id']})
        mirror.retry_media_jobs(self.config, self.store)
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[2]['dimensions'], (320, 160))
        args = self.sent[2]['args']
        self.assertEqual(args[0], '+messages-reply')
        self.assertEqual(args[args.index('--message-id') + 1], 'om_fixture_2')
        self.assertNotIn('--text', args)
        message = self.store.message('fixture-room:8', 1)
        self.assertEqual(message['sender_name'], '原姓名')
        self.assertEqual(message['feishu_message_id'], 'om_fixture_2')
        self.assertEqual(self.manager.get('media-failures'), [])

    def queue_original(self):
        job = self.manager.get('media-failures')[0]
        self.manager.post('media-failures', {'op': 'retry', 'id': job['id']})
        return job

    def test_preview_only_retry_keeps_manual_item_and_does_not_block_next_message(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        for _ in range(2):
            self.queue_original()
            mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 2)
        job = self.manager.get('media-failures')[0]
        self.assertEqual(job['state'], 'manual')
        self.assertEqual(job['attempts'], 2)
        mirror.send_row(self.config, {**self.row, 'local_id': 9, 'kind_name': 'text', 'text': '下一条'}, {}, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertIn('下一条', self.sent[-1]['args'][-3])

    def test_available_original_has_no_degradation_notice_or_repair_item(self):
        self.original_available = True
        mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(self.sent[1]['dimensions'], (320, 160))
        self.assertNotIn('缩略图', str(self.sent[0]['args']))
        self.assertEqual(self.manager.get('media-failures'), [])

    def test_failed_original_decode_does_not_resend_preview(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.original = self.root / 'broken.dat'
        self.original.write_bytes(b'broken-image')
        self.original_available = True
        self.queue_original()
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.manager.get('media-failures')[0]['state'], 'manual')

    def test_initial_unknown_upload_keeps_preview_bytes_after_original_arrives(self):
        def timeout(config, args):
            result = self.send(config, args)
            if '--image' in args:
                raise mirror.CliError('timeout', uncertain=True)
            return result
        with patch.object(mirror, 'run_lark', side_effect=timeout), self.assertRaises(mirror.CliError):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.original_available = True
        mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[1]['bytes'], self.sent[2]['bytes'])
        self.assertEqual(self.sent[1]['args'], self.sent[2]['args'])
        self.assertEqual(len(self.manager.get('media-failures')), 1)

    def test_original_unknown_upload_reuses_snapshot_and_idempotency_key(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.original_available = True
        self.queue_original()
        def timeout(config, args):
            self.send(config, args)
            raise mirror.CliError('timeout', uncertain=True)
        with patch.object(mirror, 'run_lark', side_effect=timeout):
            mirror.retry_media_jobs(self.config, self.store)
        Image.new('RGB', (600, 300), 'red').save(self.original)
        with self.store.connect() as db:
            db.execute('UPDATE media_jobs SET updated_at=0')
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.sent[2]['bytes'], self.sent[3]['bytes'])
        self.assertEqual(self.sent[2]['args'], self.sent[3]['args'])
        self.assertEqual(self.manager.get('media-failures'), [])

    def test_missing_snapshot_or_expired_receipt_requires_review_without_new_upload(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.original_available = True
        self.queue_original()
        def timeout(config, args):
            self.send(config, args)
            raise mirror.CliError('timeout', uncertain=True)
        with patch.object(mirror, 'run_lark', side_effect=timeout):
            mirror.retry_media_jobs(self.config, self.store)
        with self.store.connect() as db:
            db.execute('UPDATE media_jobs SET updated_at=0')
            db.execute("UPDATE deliveries SET attempted_at=? WHERE part='image-original'", (time.time() - 4000,))
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.manager.get('media-failures')[0]['state'], 'manual')
        self.queue_original()
        message = self.store.message('fixture-room:8', 1)
        snapshot = self.store.setting(f'image-original:{message["id"]}')
        Path(snapshot['path']).write_bytes(b'changed')
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertIn('快照', self.manager.get('media-failures')[0]['error'])

    def test_historical_registration_does_not_queue_or_resend(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        message = self.store.message('fixture-room:8', 1)
        # Simulate a pre-upgrade delivery: only its successful receipts remain.
        with self.store.connect() as db:
            db.execute("DELETE FROM media_jobs WHERE media_type='image_original'")
            db.execute("DELETE FROM settings WHERE key LIKE 'image-selection:%'")
        for _ in range(2):
            result = self.manager.post('media-failures', {'op': 'original-image', 'id': message['id']})
            self.assertFalse(result['queued'])
        self.original_available = True
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.manager.get('media-failures')), 1)
        self.queue_original()
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))

    def test_crash_after_preview_receipt_recovers_manual_item_without_resending(self):
        with patch('agents.image_original.register_preview', side_effect=RuntimeError('crash')), self.assertRaises(RuntimeError):
            mirror.send_row(self.config, self.row, {}, self.store)
        mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.manager.get('media-failures')), 1)

    def test_recovery_does_not_cross_bindings_or_replace_historical_name(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.queue_original()
        self.original_available = True
        mirror.retry_media_jobs({**self.config, 'binding_id': 2}, self.store)
        self.assertEqual(len(self.sent), 2)
        with self.store.connect() as db:
            db.execute("UPDATE people SET confirmed_name='新姓名'")
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.store.message('fixture-room:8', 1)['sender_name'], '原姓名')
        self.assertEqual(self.sent[-1]['chat_id'], 'oc_fixture')

    def test_legacy_definite_failure_can_use_new_snapshot_without_losing_target(self):
        self.original_available = True
        def reject(config, args):
            if '--image' in args:
                raise mirror.CliError('upload rejected')
            return self.send(config, args)
        # Pre-upgrade delivery staged directly, without a quality snapshot.
        with patch('agents.image_original.stage_initial', side_effect=lambda m, s, msg, row, **kw: m.stage_media(row, 'image')):
            with patch.object(mirror, 'run_lark', side_effect=reject):
                mirror.send_row(self.config, self.row, {}, self.store)
        message = self.store.message('fixture-room:8', 1)
        before = self.store.delivery(message['id'], 'media')
        self.assertEqual(before['state'], 'pending')
        self.assertIsNone(before['attempted_at'])
        job = self.manager.get('media-failures')[0]
        self.manager.post('media-failures', {'op': 'retry', 'id': job['id']})
        mirror.retry_media_jobs(self.config, self.store)
        after = self.store.delivery(message['id'], 'media')
        self.assertEqual(after['state'], 'sent')
        self.assertEqual(after['idempotency_key'], before['idempotency_key'])
        before_args, after_args = json.loads(before['payload_json']), json.loads(after['payload_json'])
        self.assertEqual(before_args[:3], after_args[:3])
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))

    def test_definite_original_rejection_can_retry_after_snapshot_expiry(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.original_available = True
        job = self.queue_original()
        with patch.object(mirror, 'run_lark', side_effect=mirror.CliError('upload rejected')):
            for _ in range(3):
                with self.store.connect() as db:
                    db.execute('UPDATE media_jobs SET updated_at=0')
                mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(self.manager.get('media-failures')[0]['state'], 'manual')
        with self.store.connect() as db:
            db.execute('UPDATE media_jobs SET updated_at=0 WHERE id=?', (job['id'],))
        mirror.expire_failed_media(self.config, self.store)
        self.queue_original()
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))
        self.assertEqual(self.manager.get('media-failures'), [])

    def test_placeholder_repair_labels_preview_without_repeating_sender(self):
        with patch.object(mirror, 'stage_media', side_effect=mirror.CliError('resource_missing')):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.queue_original()  # The only current item is the ordinary missing-image job.
        mirror.retry_media_jobs(self.config, self.store)
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertIn('本次补发缩略图', str(self.sent[1]['args']))
        self.assertNotIn('原姓名', str(self.sent[1]['args']))
        self.assertEqual(self.sent[2]['dimensions'], (16, 8))
        self.assertEqual(self.sent[2]['args'][2], 'om_fixture_1')
        self.assertEqual(self.manager.get('media-failures')[0]['media_type'], 'image_original')

    def test_expired_definite_preview_rejection_can_show_corrected_original_quality(self):
        def reject(config, args):
            if '--image' in args:
                raise mirror.CliError('upload rejected')
            return self.send(config, args)
        with patch.object(mirror, 'run_lark', side_effect=reject):
            mirror.send_row(self.config, self.row, {}, self.store)
        with self.store.connect() as db:
            db.execute('UPDATE media_jobs SET updated_at=0')
        mirror.expire_failed_media(self.config, self.store)
        self.original_available = True
        self.queue_original()
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))
        self.assertIn('本次补发原图', str(self.sent[-2]['args']))
        self.assertEqual(self.manager.get('media-failures'), [])

    def test_definite_preview_rejection_can_upgrade_to_available_original(self):
        def reject(config, args):
            if '--image' in args:
                raise mirror.CliError('upload rejected')
            return self.send(config, args)
        with patch.object(mirror, 'run_lark', side_effect=reject):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.original_available = True
        self.queue_original()
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))
        self.assertIn('本次补发原图', str(self.sent[-2]['args']))
        self.assertEqual(self.manager.get('media-failures'), [])

    def test_placeholder_preview_rejection_then_original_corrects_quality_notice(self):
        with patch.object(mirror, 'stage_media', side_effect=mirror.CliError('resource_missing')):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.queue_original()
        def reject(config, args):
            if '--image' in args:
                raise mirror.CliError('upload rejected')
            return self.send(config, args)
        with patch.object(mirror, 'run_lark', side_effect=reject):
            mirror.retry_media_jobs(self.config, self.store)
        self.assertIn('本次补发缩略图', str(self.sent[-1]['args']))
        self.original_available = True
        with self.store.connect() as db:
            db.execute('UPDATE media_jobs SET updated_at=0')
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))
        self.assertIn('本次补发原图', str(self.sent[-2]['args']))

    def test_uncertain_quality_notice_is_reconciled_before_original_correction(self):
        with patch.object(mirror, 'stage_media', side_effect=mirror.CliError('resource_missing')):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.queue_original()
        def timeout(config, args):
            self.send(config, args)
            raise mirror.CliError('timeout', uncertain=True)
        with patch.object(mirror, 'run_lark', side_effect=timeout):
            mirror.retry_media_jobs(self.config, self.store)
        # Uncertain notice persists even if the never-uploaded snapshot expires.
        message = self.store.message('fixture-room:8', 1)
        snapshot = self.store.setting(f'image-selection:{message["id"]}')
        Path(snapshot['path']).unlink()
        self.original_available = True
        with self.store.connect() as db:
            db.execute('UPDATE media_jobs SET updated_at=0')
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(self.sent[1]['args'], self.sent[2]['args'])
        self.assertIn('本次补发原图', str(self.sent[-2]['args']))
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))


if __name__ == '__main__':
    unittest.main()
