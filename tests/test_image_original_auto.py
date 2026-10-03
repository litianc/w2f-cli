"""Finite original recovery through the public send/worker/manager pipeline."""
import unittest
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from agents import image_original
from agents import storage
from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from test_image_original_recovery import OriginalRecoveryFixture


class AutomaticOriginalTest(OriginalRecoveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config.pop('image_original_wait_seconds')
        self.config.pop('image_original_auto_retry')
        self.now = 1000.0
        self.available_at = None
        self.sleep_hook = None
        clock = SimpleNamespace(time=lambda: self.now, monotonic=lambda: self.now, sleep=self.sleep)
        patch.object(image_original, 'time', clock).start()
        patch.object(mirror, 'time', clock).start()
        patch.object(storage, 'time', clock).start()

    def sleep(self, seconds):
        self.now += seconds
        if self.available_at is not None and self.now >= self.available_at:
            self.original_available = True
        if self.sleep_hook:
            self.sleep_hook()

    def test_briefly_late_original_is_sent_without_preview_or_notice(self):
        self.available_at = 1000.6
        mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))
        self.assertNotIn('缩略图', str(self.sent))
        self.assertLessEqual(self.now, 1005)
        self.assertEqual(self.manager.get('media-failures'), [])

    def test_preview_is_replied_with_original_automatically_after_30_seconds(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(self.now, 1005)
        self.assertEqual(self.sent[-1]['dimensions'], (16, 8))
        self.original_available = True
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 2)
        self.now += 30
        mirror.retry_media_jobs(self.config, self.store)
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))
        self.assertEqual(self.sent[-1]['args'][:3], ['+messages-reply', '--message-id', 'om_fixture_2'])
        self.assertEqual(self.manager.get('media-failures'), [])

    def test_three_checks_are_spaced_persist_across_restart_and_end_in_manual(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        pending = self.manager.get('media-failures')
        self.assertEqual(len(pending), 1, 'Scheduled original recovery must be visible in the console')
        self.assertTrue(pending[0]['auto_retry'])
        self.assertEqual(pending[0]['next_attempt_at'], 1035)
        for count, due in enumerate((1035, 1155, 1755), 1):
            self.now = due - 1
            mirror.retry_media_jobs(self.config, MirrorStore(self.store.path))
            self.assertEqual(self.manager.get('media-failures')[0]['attempts'], count - 1)
            self.now = due
            mirror.retry_media_jobs(self.config, MirrorStore(self.store.path))
            job = self.manager.get('media-failures')[0]
            self.assertEqual(job['attempts'], count)
            self.assertEqual(job['state'], 'manual' if count == 3 else 'pending')
        self.now += 86400
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(self.manager.get('media-failures')[0]['auto_retry'])
        mirror.send_row(self.config, {**self.row, 'local_id': 9, 'kind_name': 'text', 'text': '下一条'}, {}, self.store)
        self.assertEqual(len(self.sent), 3)

    def test_idle_worker_wakes_for_original_without_a_new_chat_message(self):
        self.config.update(image_original_wait_seconds=0, sqlite_file=str(self.store.path), poll_interval=600)
        self.store.initialize_cursor(1, 0)
        self.store.set_control(1, 'running')
        completed = threading.Event()
        def reader(cmd, **kwargs):
            if 'sync-page' in cmd:
                data = {'contract': 'ordered_sync_v1', 'through_local_id': 0, 'messages': [], 'has_more': False}
                return SimpleNamespace(returncode=0, stdout=json.dumps({'data': data}), stderr='')
            return self.reader(cmd, **kwargs)
        def send(config, args):
            result = self.send(config, args)
            if args[0] == '+messages-reply':
                self.store.set_control(1, 'stopped')
                completed.set()
            return result
        with patch.object(image_original, 'time', time), patch.object(mirror, 'time', time), patch.object(storage, 'time', time), \
             patch.object(image_original, 'AUTO_DELAYS', (.3, .3, .3)), \
             patch.object(mirror.subprocess, 'run', side_effect=reader), \
             patch.object(mirror, 'run_lark', side_effect=send), \
             patch('agents.manage.run_cli', return_value=[{'name': 'fixture', 'appId': 'fixture'}]):
            mirror.send_row(self.config, self.row, {}, self.store)
            self.original_available = True
            worker = threading.Thread(target=mirror.stream, args=(self.config,), daemon=True)
            worker.start()
            try:
                self.assertTrue(completed.wait(3), 'Original retry waited for the 600-second source poll')
                self.assertEqual(self.sent[-1]['dimensions'], (320, 160))
            finally:
                self.store.set_control(1, 'stopped')
                worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_pause_during_short_wait_sends_nothing_and_resume_does_not_wait_again(self):
        self.config['_guard'] = True
        self.store.set_control(1, 'running')
        self.sleep_hook = lambda: self.store.set_control(1, 'paused')
        with self.assertRaises(mirror.ControlChanged):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(self.sent, [])
        self.sleep_hook = None
        self.now = 1020
        self.original_available = True
        self.store.set_control(1, 'running')
        mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(self.now, 1020)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))

    def test_completed_attempt_keeps_spacing_even_if_schedule_write_was_interrupted(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        job = self.manager.get('media-failures')[0]
        before = self.store.setting(f'image-original-auto:{job["id"]}')
        self.now = 1035
        mirror.retry_media_jobs(self.config, self.store)
        # Model a crash after the attempt counter commit, before schedule write.
        self.store.save_setting(f'image-original-auto:{job["id"]}', before)
        self.now = 1036
        mirror.retry_media_jobs(self.config, MirrorStore(self.store.path))
        current = self.manager.get('media-failures')[0]
        self.assertEqual(current['attempts'], 1)
        self.assertEqual(current['next_attempt_at'], 1155)

    def test_pause_during_auto_lookup_does_not_send_or_consume_an_attempt(self):
        self.config['_guard'] = True
        self.store.set_control(1, 'running')
        mirror.send_row(self.config, self.row, {}, self.store)
        self.now = 1035
        self.original_available = True
        def reader(cmd, **kwargs):
            result = self.reader(cmd, **kwargs)
            self.store.set_control(1, 'paused')
            return result
        with patch.object(mirror.subprocess, 'run', side_effect=reader), self.assertRaises(mirror.ControlChanged):
            mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.manager.get('media-failures')[0]['attempts'], 0)
        self.store.set_control(1, 'running')
        mirror.retry_media_jobs(self.config, MirrorStore(self.store.path))
        self.assertEqual(len(self.sent), 3)

    def test_pause_during_preview_only_lookup_does_not_consume_an_attempt(self):
        self.assert_paused_lookup_keeps_attempts()

    def test_pause_during_broken_resource_lookup_does_not_consume_an_attempt(self):
        self.assert_paused_lookup_keeps_attempts(broken=True)

    def assert_paused_lookup_keeps_attempts(self, *, broken=False):
        self.config['_guard'] = True
        self.store.set_control(1, 'running')
        mirror.send_row(self.config, self.row, {}, self.store)
        self.now = 1035
        if broken:
            self.preview.write_bytes(b'')
        def reader(cmd, **kwargs):
            result = self.reader(cmd, **kwargs)
            self.store.set_control(1, 'paused')
            return result
        with patch.object(mirror.subprocess, 'run', side_effect=reader), self.assertRaises(mirror.ControlChanged):
            mirror.retry_media_jobs(self.config, self.store)
        job = self.manager.get('media-failures')[0]
        self.assertEqual(job['attempts'], 0)
        self.assertEqual(job['next_attempt_at'], 1035)
        self.assertEqual(len(self.sent), 2)

    def test_three_missing_original_checks_still_allow_manual_recovery(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        for due in (1035, 1155, 1755):
            self.now = due
            mirror.retry_media_jobs(self.config, self.store)
        job = self.manager.get('media-failures')[0]
        self.manager.post('media-failures', {'op': 'retry', 'id': job['id']})
        self.original_available = True
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[-1]['dimensions'], (320, 160))

    def test_old_manual_item_does_not_become_automatic_when_enabled(self):
        mirror.send_row({**self.config, 'image_original_auto_retry': False}, self.row, {}, self.store)
        self.original_available = True
        self.now += 3600
        mirror.retry_media_jobs(self.config, MirrorStore(self.store.path))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.manager.get('media-failures')[0]['state'], 'manual')

    def test_disabling_automatic_recovery_leaves_manual_item(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.original_available = True
        self.now = 1035
        mirror.retry_media_jobs({**self.config, 'image_original_auto_retry': False}, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.manager.get('media-failures')[0]['state'], 'manual')

    def test_slow_recheck_uses_remaining_grace_budget_and_keeps_preview(self):
        observed_timeouts = []
        def reader(cmd, **kwargs):
            if self.now > 1000:
                observed_timeouts.append(kwargs['timeout'])
                self.now += kwargs['timeout']
                raise mirror.subprocess.TimeoutExpired(cmd, kwargs['timeout'])
            return self.reader(cmd, **kwargs)
        with patch.object(mirror.subprocess, 'run', side_effect=reader):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.assertLessEqual(self.now, 1005)
        self.assertTrue(observed_timeouts)
        self.assertLessEqual(max(observed_timeouts), 4)
        self.assertEqual(self.sent[-1]['dimensions'], (16, 8))

    def test_grace_does_not_start_a_fresh_url_download_after_reader_timeout(self):
        self.row['text'] = '<img cdnurl="https://fixture.invalid/image" />'
        def reader(cmd, **kwargs):
            if self.now > 1000:
                self.now += kwargs['timeout']
                raise mirror.subprocess.TimeoutExpired(cmd, kwargs['timeout'])
            return self.reader(cmd, **kwargs)
        def download(*args, **kwargs):
            self.now += kwargs['timeout']
            raise TimeoutError('synthetic download timeout')
        with patch.object(mirror.subprocess, 'run', side_effect=reader), \
             patch.object(mirror.urllib.request, 'urlopen', side_effect=download) as network:
            mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(self.now, 1005)
        network.assert_not_called()
        self.assertEqual(self.sent[-1]['dimensions'], (16, 8))

    def test_auto_timeout_preserves_original_bytes_and_uuid_then_stops_if_too_old(self):
        mirror.send_row(self.config, self.row, {}, self.store)
        self.original_available = True
        self.now = 1035
        def timeout(config, args):
            self.send(config, args)
            raise mirror.CliError('timeout', uncertain=True)
        with patch.object(mirror, 'run_lark', side_effect=timeout):
            mirror.retry_media_jobs(self.config, self.store)
        from PIL import Image
        Image.new('RGB', (640, 320), 'red').save(self.original)
        self.now = 1155
        with patch.object(mirror, 'run_lark', side_effect=timeout):
            mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(self.sent[-1]['bytes'], self.sent[-2]['bytes'])
        self.assertEqual(self.sent[-1]['args'], self.sent[-2]['args'])
        self.now = 5000
        mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.manager.get('media-failures')[0]['state'], 'manual')
        self.assertFalse(self.manager.get('media-failures')[0]['auto_retry'])


if __name__ == '__main__':
    unittest.main()
