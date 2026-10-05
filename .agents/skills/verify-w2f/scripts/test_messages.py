"""Contract and real-process checks for the synthetic-message verification mode."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import message_fixture as fixture
import messages


class ReaderContract(unittest.TestCase):
    def test_fixed_snapshot_excludes_later_injection_and_advances_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.sqlite3'
            fixture.initialize(path)
            for text in ('一', '二', '三'):
                fixture.inject(path, text)
            args = ['sync-page', '--talker', fixture.TALKER, '--after-local-id', '0', '--limit', '2']
            with fixture.connect(path) as db:
                first = fixture.read_page(db, args)['data']
            self.assertEqual([r['local_id'] for r in first['messages']], [1, 2])
            self.assertTrue(first['has_more'])
            fixture.inject(path, '快照之后的新消息')
            with fixture.connect(path) as db:
                second = fixture.read_page(db, args + ['--through-local-id', str(first['through_local_id']),
                    '--page-after-time', str(first['next']['time']), '--page-after-local-id', '2'])['data']
                self.assertEqual([r['local_id'] for r in second['messages']], [3])
                self.assertFalse(second['has_more'])
                self.assertEqual(second['through_local_id'], 3)
                with self.assertRaises(ValueError):
                    fixture.read_page(db, ['sync-page', '--talker', 'another-room',
                                          '--after-local-id', '0', '--limit', '2'])
                with self.assertRaises(ValueError):
                    fixture.read_page(db, args + ['--through-local-id', '99'])


class WorkerProof(unittest.TestCase):
    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(Path(messages.__file__)), *map(str, args)],
                           cwd=messages.ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode == 0, ok, p.stderr + p.stdout)
        return p.stdout.strip() if ok else p.stderr

    def launch(self):
        run = Path(self.cli('launch'))
        self.addCleanup(self.cli, 'cleanup', run)
        return run

    def test_live_arrival_restart_offline_paging_and_independent_runs(self):
        first, second = self.launch(), self.launch()
        self.assertNotEqual(first, second)
        self.cli('doctor', first)
        self.cli('drive', first, '--text', '实时中文消息 😀\n第二行')
        one = json.loads((first / 'evidence/drive-0001.json').read_text())
        self.assertEqual(one['sink'][0]['text'], '测试发送者：实时中文消息 😀\n第二行')
        self.assertEqual(len(messages.source(second)), 0)
        self.cli('drive', first)
        replay = json.loads((first / 'evidence/drive-0002.json').read_text())
        self.assertEqual(len([e for e in replay['events'] if e['kind'] == 'send']), 1)
        self.cli('collect', first)
        queue = json.loads((first / 'evidence/client-queue-0001.json').read_text())
        self.assertEqual(queue['result'], 'passed')
        self.assertEqual(queue['status']['pending_count'], 1)
        self.assertEqual(queue['pending'], queue['after_restart'])
        for text in ('离线一', '离线二', '离线三'):
            self.cli('inject', first, '--text', text)
        # Injection updates only the source: the stopped product still has one row.
        self.assertEqual(len(messages.snapshot(first)['messages']), 1)
        self.cli('drive', first)
        self.cli('collect', first)
        backfill = json.loads((first / 'evidence/drive-0003.json').read_text())
        self.assertEqual(backfill['cursor'][0]['cursor'], 4)
        pages = [json.loads(e['response'])['data'] for e in backfill['events'] if e['kind'] == 'reader']
        self.assertTrue(any(p['has_more'] and p['through_local_id'] == 4 for p in pages))
        self.assertTrue(any([r['local_id'] for r in p['messages']] == [4] for p in pages))
        self.cli('cleanup', first)
        self.assertFalse((first / 'runtime').exists())
        self.assertTrue((first / 'evidence/drive-0003.json').is_file())
        # Cleaning one run leaves the other runnable.
        self.cli('drive', second, '--text', '独立运行')
        self.assertEqual(len(messages.snapshot(second)['sink']), 1)
        print(f'Behavioral evidence: {first}/evidence and {second}/evidence')

    def test_duplicate_attempt_is_not_hidden_by_sink_deduplication(self):
        run = self.launch()
        self.cli('drive', run, '--text', '核对重复发送检测')
        original = messages.snapshot(run)
        sent = next(e for e in original['events'] if e['kind'] == 'send')
        # Replay the real executable boundary request, not a forged sink count.
        p = subprocess.run([str(run / 'runtime/bin/lark-cli'), *json.loads(sent['request'])],
                           env=messages.environment(run), capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(messages.snapshot(run)['sink']), 1)
        self.assertIn('Duplicate send attempt', self.cli('drive', run, ok=False))
        report = json.loads((run / 'evidence/drive-0002.json').read_text())
        self.assertEqual(report['result'], 'failed')
        self.assertFalse(messages.live_registered(run))
        print(f'Expected-failure evidence (duplicate attempt): {run}/evidence')

    def test_configuration_tamper_and_network_are_rejected(self):
        run = self.launch()
        config_path = run / 'runtime/config.json'
        original = config_path.read_text()
        config = json.loads(original)
        config['wechat_reader'] = '/usr/bin/false'
        config_path.write_text(json.dumps(config))
        self.assertIn('Fixture configuration changed', self.cli('doctor', run, ok=False))
        config_path.write_text(original)
        p = subprocess.run([sys.executable, '-c', "import socket; socket.create_connection(('127.0.0.1',13040))"],
                           env=messages.environment(run), capture_output=True, text=True, timeout=10)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('no network transport', p.stderr)
        self.assertIn('Unexpected network operation', self.cli('doctor', run, ok=False))
        self.assertFalse(messages.live_registered(run))


if __name__ == '__main__':
    unittest.main(verbosity=2)
