"""Source notifications are coalesced without losing a pending scan."""
import unittest
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from agents.scan_schedule import ScanSchedule
from agents.storage import MirrorStore
from agents import wechat_feishu_mirror as mirror


class ScanScheduleTest(unittest.TestCase):
    def test_continuous_notifications_do_not_rescan_early_or_postpone_deadline(self):
        clock = ScanSchedule(minimum_interval=10, poll_interval=600)
        self.assertTrue(clock.ready(0))
        clock.begin(0)
        clock.finish(1, delivered=0)
        for now in range(1, 10):
            clock.request(now)
            self.assertFalse(clock.ready(now))
        self.assertTrue(clock.ready(10))
        clock.begin(10)
        clock.finish(11, delivered=0)
        self.assertFalse(clock.ready(20), 'No notifications means no extra source scan')
        self.assertTrue(clock.ready(611), 'Normal polling must remain a safety net')

    def test_failure_backoff_cannot_be_bypassed_by_wakeup(self):
        clock = ScanSchedule()
        clock.begin(0)
        clock.fail(2)
        for now in range(3, 62):
            clock.request(now)
            self.assertFalse(clock.ready(now))
        self.assertTrue(clock.ready(62))

    def test_bindings_are_independent_and_backlog_continues_at_next_window(self):
        first, second = ScanSchedule(), ScanSchedule()
        first.begin(0)
        first.finish(2, delivered=5)
        self.assertFalse(first.ready(9))
        self.assertTrue(first.ready(10))
        self.assertTrue(second.ready(0))

    def test_short_poll_and_resume_request_still_respect_minimum_interval(self):
        clock = ScanSchedule(minimum_interval=10, poll_interval=2)
        clock.begin(0);clock.finish(1, delivered=0)
        clock.request(3)  # resume within the current window
        self.assertFalse(clock.ready(9))
        self.assertTrue(clock.ready(10))

    def test_invalid_intervals_are_rejected(self):
        for value in (-1, 601, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ScanSchedule(minimum_interval=value)


class StreamCoalescingTest(unittest.TestCase):
    def test_notifications_during_read_are_retained_and_messages_stay_in_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {'binding_id': 1, 'wechat_talker': 'room', 'feishu_chat_id': 'oc_fixture',
                      'lark_profile': 'fixture', 'lark_app_id': 'app_fixture',
                      'sqlite_file': str(root / 'state.db'), 'media_dir': str(root / 'media'),
                      'source_min_interval': .6, 'poll_interval': 600, 'reader_wakeup': True,
                      'sender_map': {'wx_fixture': '测试成员'}}
            store = MirrorStore(config['sqlite_file']);store.initialize(config)
            store.initialize_cursor(1, 0);store.set_control(1, 'running')
            scans, sent = [], []
            def notify(config, wakeup, stopped):
                while not stopped.wait(.02):wakeup.set()
            def reader(config, cursor, high=None, after=None):
                scans.append(time.monotonic())
                if len(scans) == 2:store.set_control(1, 'stopped')
                time.sleep(.05)  # notifications arriving inside a scan must survive it
                high = len(scans) * 2
                rows = [{'talker': 'room', 'local_id': n, 'create_time': n,
                         'sender_wxid': 'wx_fixture', 'kind_name': 'text', 'text': f'消息{n}'}
                        for n in range(cursor + 1, high + 1)]
                return {'contract': 'ordered_sync_v1', 'through_local_id': high,
                        'messages': rows, 'has_more': False, 'next': None}
            def send(config, args):
                sent.append(args[args.index('--text') + 1])
                return {'data': {'message_id': f'om_fixture_{len(sent)}'}}
            recover = mirror.recover_once
            with patch.object(mirror, 'reader_wakeup_loop', side_effect=notify), \
                 patch('agents.group_names.group_name_loop'), patch('agents.manage.profile_list'), \
                 patch.object(mirror, 'recover_once', side_effect=lambda cfg, db: recover(cfg, db, reader)), \
                 patch.object(mirror, 'run_lark', side_effect=send):
                worker = threading.Thread(target=mirror.stream, args=(config,), daemon=True)
                worker.start()
                try:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                finally:
                    store.set_control(1, 'stopped');worker.join(3)
            self.assertEqual(len(scans), 2)
            self.assertGreaterEqual(scans[1] - scans[0], .6)
            self.assertEqual(sent, [f'测试成员：消息{n}' for n in range(1, 5)])
            self.assertEqual(store.get_cursor(1), 4)


if __name__ == '__main__':
    unittest.main()
