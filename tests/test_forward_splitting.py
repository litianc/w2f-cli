"""Record boundaries and byte budgets at the real forward delivery boundary."""
import contextlib
import html
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from tests.test_forward_links import forward_xml
from tests.test_recovery import History, Sink


class ForwardSplittingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MirrorStore(Path(self.tmp.name) / 'state.db')
        self.config = {'binding_id': 1, 'source_account': 'fixture', 'wechat_talker': 'room',
                       'feishu_chat_id': 'chat', 'lark_profile': 'fixture', 'lark_app_id': 'app',
                       'initial_cursor': 10, 'sender_map': {'outer': '转发人'}}
        self.store.initialize(self.config)
        self.store.set_control(1, 'running')
        self.sink = Sink()
        sender = patch.object(mirror, 'run_lark', self.sink)
        sender.start()
        self.addCleanup(sender.stop)

    def row(self, records, local_id=11):
        raw = ''.join('<dataitem datatype="1"><sourcename>' + html.escape(name)
                      + '</sourcename><datadesc>' + html.escape(body)
                      + '</datadesc></dataitem>' for name, body in records)
        return {'talker': 'room', 'local_id': local_id, 'server_id': str(local_id),
                'sender_wxid': 'outer', 'create_time': 1000 + local_id,
                'kind_name': 'forward_chat', 'text': forward_xml(raw)}

    def send(self, records):
        with contextlib.redirect_stdout(io.StringIO()):
            mirror.recover_once(self.config, self.store, History([self.row(records)]))
        return self.texts()

    def texts(self):
        return [args[args.index('--text') + 1] for _, args in self.sink.sent]

    def test_300_records_stay_whole_in_order_with_complete_headers_within_budget(self):
        records = [('甲' if i % 2 else '乙', f'记录{i:03d}：' + '中文😀 line\n' * 35
                    + f'https://example.test/{i}?a=1&b=2') for i in range(300)]
        expected_records = [name + '：' + body for name, body in records]
        expected = '\n'.join(expected_records)
        self.assertGreater(len(expected.encode()), 100000)
        parts = self.send(records)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p.encode()) <= 12000 for p in parts))
        self.assertEqual(sum(p.count('转发人转发聊天记录') for p in parts), 1)
        chunks = [p.split('\n', 1)[1] for p in parts]
        self.assertEqual('\n'.join(chunks), expected)
        for index, (part, chunk) in enumerate(zip(parts, chunks), 1):
            self.assertIn(f'第 {index}/{len(parts)} 段', part.split('\n', 1)[0])
        for record in expected_records:
            self.assertEqual(sum(record in chunk for chunk in chunks), 1)

    def test_oversized_record_marks_continuations_and_keeps_a_fitting_url_whole(self):
        url = 'https://example.test/article?value=' + 'x' * 1800 + '&literal=%26amp%3B'
        body = '文' * 3900 + '\n' + url + '\n' + '😀尾' * 3000
        parts = self.send([('甲', body)])
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(p.encode()) <= 12000 for p in parts))
        self.assertEqual(''.join(p.split('\n', 1)[1] for p in parts), '甲：' + body)
        self.assertEqual(sum(url in p for p in parts), 1)
        self.assertNotIn('续上一条', parts[0].split('\n', 1)[0])
        self.assertTrue(all('续上一条' in p.split('\n', 1)[0] for p in parts[1:]))

    def test_first_middle_last_lost_receipts_resume_frozen_parts_then_use_new_name(self):
        records = [('甲', f'记录{i:03d} ' + '中文😀\n' * 40) for i in range(300)]
        normal_parts = self.send(records)
        row = self.row(records)
        following = {**row, 'local_id': 12, 'server_id': '12', 'kind_name': 'text',
                     'text': '后续消息', 'create_time': 1012}
        for failed_part in (0, len(normal_parts) // 2, len(normal_parts) - 1):
            with self.subTest(failed_part=failed_part):
                store = MirrorStore(Path(self.tmp.name) / f'restart-{failed_part}.db')
                store.initialize(self.config)
                store.set_control(1, 'running')
                sink = Sink()
                sink.timeout_after = failed_part + 1
                source = History([row, following])
                with patch.object(mirror, 'run_lark', sink), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(mirror.CliError):
                        mirror.recover_once(self.config, store, source)
                    self.assertEqual(store.get_cursor(), 10)
                    store.rename_person(store.list_people()[0]['id'], '新实名')
                    restarted = MirrorStore(store.path)
                    mirror.recover_once(self.config, restarted, source)
                    self.assertEqual(mirror.recover_once(self.config, restarted, source), 0)
                texts = [args[args.index('--text') + 1] for _, args in sink.sent]
                self.assertEqual(texts, normal_parts + ['新实名：后续消息'])
                self.assertEqual(len(sink.receipts), len(texts))
                self.assertEqual(restarted.get_cursor(), 12)

    def test_impossibly_long_link_fails_before_any_part_is_sent(self):
        records = [('甲', '先前正文' * 6000),
                   ('乙', 'https://example.test/' + 'x' * 13000)]
        with self.assertRaisesRegex(ValueError, '单个链接超过消息字节预算'):
            self.send(records)
        self.assertEqual(self.sink.sent, [])
        self.assertEqual(self.store.get_cursor(), 10)
        message = self.store.message(mirror.message_key(self.row(records)), 1)
        self.assertEqual(message['state'], 'failed')
        self.assertNotIn('https://', message['last_error'])

    def test_record_that_exactly_fits_one_complete_message_is_not_split(self):
        prefix = '转发人转发聊天记录（第 1/1 段）\n甲：'
        body = 'x' * (12000 - len(prefix.encode()))
        parts = self.send([('甲', body)])
        self.assertEqual(parts, [prefix + body])

    def test_multiple_exact_fitting_records_use_actual_headers_and_keep_link_whole(self):
        first = '转发人转发聊天记录（第 1/2 段）\n甲：'
        second = '转发聊天记录（第 2/2 段）\n乙：'
        body = 'x' * (12000 - len(first.encode()))
        url = 'https://example.test/'
        url += 'x' * (12000 - len(second.encode()) - len(url.encode()))
        parts = self.send([('甲', body), ('乙', url)])
        self.assertEqual(parts, [first + body, second + url])


if __name__ == '__main__':
    unittest.main()
