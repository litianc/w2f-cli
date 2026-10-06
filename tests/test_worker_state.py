"""Worker state stays cheap as archived mappings accumulate."""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agents.storage import MirrorStore
from agents import wechat_feishu_mirror as mirror
from agents.manage import profile_list


class WorkerStateTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MirrorStore(Path(temp.name) / 'state.db')
        self.config = {'binding_id': 1, '_guard': True, 'wechat_talker': 'room',
                       'feishu_chat_id': 'chat', 'lark_profile': 'fixture',
                       'lark_app_id': 'app_fixture'}
        self.store.initialize(self.config)
        self.store.set_control(1, 'running')

    def archive_fixtures(self, count):
        with self.store.connect() as db:
            for bid in range(2, count + 1):
                db.execute('''INSERT OR IGNORE INTO bindings(id,source_account,
                    wechat_chat_name,wechat_talker,feishu_chat_name,feishu_chat_id,
                    lark_profile,lark_app_id,archived,created_at,updated_at)
                    VALUES(?,'fixture','source',?,'target',?,'fixture','app_fixture',1,0,0)''',
                    (bid, f'room-{bid}', f'chat-{bid}'))

    def test_single_binding_details_cost_does_not_grow_with_archived_groups(self):
        self.store.save_setting('member_sync:1', {'enabled': True})
        self.store.save_setting('name_sync_enabled:1', True)
        self.store.save_setting('name_sync:1', {'status': 'baseline'})
        with patch.object(self.store, 'connect', wraps=self.store.connect) as connects:
            expected = self.store.get_binding(1)
            baseline = connects.call_count
        self.assertTrue(expected['member_sync_enabled'])
        self.assertTrue(expected['name_sync_enabled'])
        self.assertEqual(expected['name_sync'], {'status': 'baseline'})
        for count in (10, 21, 100):
            with self.subTest(bindings=count):
                self.archive_fixtures(count)
                with patch.object(self.store, 'connect', wraps=self.store.connect) as connects:
                    actual = self.store.get_binding(1)
                self.assertEqual(actual, expected)
                self.assertEqual(connects.call_count, baseline)
        self.assertIsNone(self.store.get_binding(999))

    def test_send_guard_reads_current_binding_without_expanding_management_details(self):
        self.archive_fixtures(100)
        with patch.object(self.store, 'connect', wraps=self.store.connect) as connects:
            mirror.check_control(self.config, self.store)
        self.assertLessEqual(connects.call_count, 2)
        self.assertEqual(self.config['wechat_talker'], 'room')
        self.assertEqual(self.config['feishu_chat_id'], 'chat')

    def test_unchanged_profiles_do_not_rewrite_applications_or_bindings(self):
        self.archive_fixtures(21)
        real_connect = self.store.connect
        writes = []

        @contextmanager
        def observe_writes():
            with real_connect() as db:
                yield db
                writes.append(db.total_changes)

        with patch.object(self.store, 'connect', side_effect=observe_writes), \
             patch('agents.manage.run_cli', return_value=[{'name': 'fixture', 'appId': 'app_fixture'}]):
            for _ in range(2):
                profile_list(self.store)
        self.assertEqual(sum(writes), 0)

    def test_pause_stop_and_application_disable_block_next_send_and_resume_allows_it(self):
        other = MirrorStore(self.store.path)
        row = {'talker': 'room', 'local_id': 1, 'create_time': 1, 'text': 'fixture'}
        self.store.upsert_message('room:1', row, 'text', binding_id=1)
        message = self.store.message('room:1', 1)
        args = ['+messages-send', '--chat-id', 'chat', '--text', 'fixture']
        with patch.object(mirror, 'run_lark', return_value={'data': {'message_id': 'om_fixture'}}) as send:
            for state in ('paused', 'stopped'):
                other.set_control(1, state)
                with self.subTest(state=state), self.assertRaises(mirror.ControlChanged):
                    mirror.send_part(self.config, self.store, message, 'text', args)
                send.assert_not_called()
            other.set_control(1, 'running')
            other.set_profile_enabled('fixture', False)
            other.set_control(1, 'running')  # A disabled app still blocks a running binding.
            with self.assertRaises(mirror.ControlChanged):
                mirror.send_part(self.config, self.store, message, 'text', args)
            send.assert_not_called()
            other.set_profile_enabled('fixture', True)
            mirror.send_part(self.config, self.store, message, 'text', args)
            send.assert_called_once()
        self.assertEqual(self.store.delivery(message['id'], 'text')['feishu_message_id'], 'om_fixture')

    def test_external_profile_rename_is_fresh_and_replacement_blocks_send(self):
        self.store.upsert_profile('feishu-mcp', 'app_protected', protected=True)
        common = {'name': 'feishu-mcp', 'appId': 'app_protected'}
        with patch('agents.manage.run_cli', return_value=[common, {'name': 'renamed', 'appId': 'app_fixture'}]):
            profile_list(self.store)
        mirror.check_control(self.config, self.store)
        self.assertEqual(self.config['lark_profile'], 'renamed')
        self.assertEqual(self.config['lark_app_id'], 'app_fixture')
        with patch('agents.manage.run_cli', return_value=[common, {'name': 'renamed', 'appId': 'app_replacement'}]):
            profile_list(self.store)
        with self.assertRaises(mirror.ControlChanged):
            mirror.check_control(self.config, self.store)
        protected = next(p for p in self.store.list_profiles() if p['app_id'] == 'app_protected')
        self.assertTrue(protected['enabled'])
        self.assertTrue(protected['protected'])

    def test_missing_profile_stops_binding_and_repeated_check_does_not_write(self):
        with patch('agents.manage.run_cli', return_value=[]):
            profile_list(self.store)
        with self.assertRaises(mirror.ControlChanged):
            mirror.check_control(self.config, self.store)
        real_connect = self.store.connect
        writes = []

        @contextmanager
        def observe_writes():
            with real_connect() as db:
                yield db
                writes.append(db.total_changes)

        with patch.object(self.store, 'connect', side_effect=observe_writes), \
             patch('agents.manage.run_cli', return_value=[]):
            profile_list(self.store)
        self.assertEqual(sum(writes), 0)

    def test_worker_state_stops_missing_control_and_does_not_return_another_binding(self):
        self.archive_fixtures(10)
        self.assertIsNone(self.store.get_worker_state(999))
        with self.store.connect() as db:
            db.execute('DELETE FROM runtime_control WHERE binding_id=1')
        self.assertEqual(self.store.get_worker_state(1)['control_status'], 'stopped')
        with self.assertRaises(mirror.ControlChanged):
            mirror.check_control(self.config, self.store)


if __name__ == '__main__':
    unittest.main()
