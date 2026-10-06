"""Public CLI proof of durable collection with a synthetic reader executable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
READER = '''
import json, os, sys
from pathlib import Path
args=sys.argv
def arg(name, default=None):
    return args[args.index(name)+1] if name in args else default
rows=json.loads(Path(os.environ['CLIENT_TEST_SOURCE']).read_text(encoding='utf-8'))
base=int(arg('--after-local-id', '0'))
high=int(arg('--through-local-id', str(max([r['local_id'] for r in rows], default=0))))
after=int(arg('--page-after-local-id', str(base)))
if os.environ.get('CLIENT_TEST_FAIL_AFTER') and after >= int(os.environ['CLIENT_TEST_FAIL_AFTER']):
    print(json.dumps({'ok':False,'error':{'message':'synthetic failure'}}));sys.exit(1)
limit=int(arg('--limit', '100'))
rows=[r for r in rows if base < r['local_id'] <= high and r['local_id'] > after]
page=rows[:limit]
print(json.dumps({'ok':True,'data':{'contract':'ordered_sync_v1','messages':page,
 'through_local_id':high,'has_more':len(rows)>limit,
 'next':{'time':page[-1]['create_time'],'local_id':page[-1]['local_id']} if len(rows)>limit else None}}))
'''


class ClientCollection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='w2f-client-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'client-state'
        self.source = self.root / 'source.json'
        self.source.write_text('[]', encoding='utf-8')
        self.env = {**os.environ, 'CLIENT_TEST_SOURCE': str(self.source)}
        reader = self.root / 'reader'
        if os.name == 'nt':
            script = reader.with_suffix('.py')
            script.write_text(READER, encoding='utf-8')
            reader = reader.with_suffix('.cmd')
            reader.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
        else:
            reader.write_text(f'#!{sys.executable}\n' + READER, encoding='utf-8')
            reader.chmod(0o700)
        self.reader = reader

    def command(self, *args, ok=True):
        result = subprocess.run([sys.executable, '-m', 'client', '--state-dir', str(self.state), *args],
                                cwd=ROOT, env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def rows(self, count):
        rows = [dict(talker='synthetic-room', local_id=i, server_id=str(100+i),
                     sender_wxid='synthetic-sender', sender='测试者', create_time=1000+i,
                     local_type=1, kind_name='text', text=f'测试消息 {i} 😀') for i in range(1, count+1)]
        self.source.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        return rows

    def initialize(self, **limits):
        options = [item for key, value in limits.items() for item in ('--' + key.replace('_','-'), str(value))]
        self.command('init', '--reader', str(self.reader), '--account', 'synthetic-account', *options)

    def test_enable_boundary_and_restarts_preserve_pending_event_identity(self):
        self.rows(2)
        self.initialize(page_size=2)
        enabled = self.command('enable', '--talker', 'synthetic-room')
        self.assertEqual(enabled['read_cursor'], 2)
        self.assertEqual(self.command('pending')['events'], [])
        rows = self.rows(5)
        self.command('collect')
        pending = self.command('pending')['events']
        self.assertEqual([event['message'] for event in pending], rows[2:])
        self.assertEqual(len({event['event_id'] for event in pending}), 3)
        self.command('collect')
        self.assertEqual(self.command('pending')['events'], pending)
        status = self.command('status')
        self.assertEqual(status['streams'][0]['read_cursor'], 5)
        self.assertEqual(status['pending_count'], 3)
        self.assertTrue(status['upload_implemented'])

    def test_full_queue_keeps_unstored_page_and_cursor_for_retry(self):
        self.initialize(page_size=1, max_pending_bytes=350)
        self.command('enable', '--talker', 'synthetic-room')
        self.rows(3)
        blocked = self.command('collect', ok=False)
        self.assertEqual(blocked['streams'][0]['error_code'], 'queue_full')
        self.assertEqual(blocked['streams'][0]['read_cursor'], 1)
        self.assertEqual(blocked['pending_count'], 1)
        before = self.command('pending')
        self.command('collect', ok=False)
        self.assertEqual(self.command('pending'), before)
        self.assertEqual(self.command('status')['streams'][0]['read_cursor'], 1)

    def test_bad_page_is_rejected_atomically_then_retried_when_reader_recovers(self):
        self.initialize(page_size=2)
        self.command('enable', '--talker', 'synthetic-room')
        rows = self.rows(2)
        rows[1]['talker'] = 'wrong-room'
        self.source.write_text(json.dumps(rows), encoding='utf-8')
        bad = self.command('collect', ok=False)
        self.assertEqual(bad['streams'][0]['error_code'], 'reader_contract')
        self.assertEqual(bad['pending_count'], 0)
        self.assertEqual(bad['streams'][0]['read_cursor'], 0)
        self.rows(2)
        self.command('collect')
        self.assertEqual(self.command('status')['pending_count'], 2)

    def test_unavailable_reader_preserves_state_and_recovery_fills_gap(self):
        self.initialize()
        self.command('enable', '--talker', 'synthetic-room')
        self.rows(1)
        self.command('collect')
        saved = self.command('pending')
        self.source.unlink()
        status = self.command('collect', ok=False)
        self.assertEqual(status['streams'][0]['error_code'], 'reader_unavailable')
        self.assertEqual(self.command('pending'), saved)
        self.rows(3)
        self.command('collect')
        self.assertEqual(self.command('status')['pending_count'], 3)
        self.assertEqual(self.command('pending')['events'][0], saved['events'][0])

    def test_reinitialize_and_reenable_cannot_discard_existing_pending_data(self):
        self.initialize()
        original = self.command('enable', '--talker', 'synthetic-room')
        self.rows(1)
        self.command('collect')
        pending = self.command('pending')
        self.command('init', '--reader', str(self.reader), '--account', 'replacement', ok=False)
        self.assertEqual(self.command('enable', '--talker', 'synthetic-room')['id'], original['id'])
        self.assertEqual(self.command('pending'), pending)

    def test_source_reset_and_unsupported_media_do_not_advance_cursor(self):
        self.initialize()
        self.command('enable', '--talker', 'synthetic-room')
        self.rows(2)
        self.command('collect')
        self.rows(0)
        reset = self.command('collect', ok=False)
        self.assertEqual(reset['streams'][0]['error_code'], 'source_changed')
        self.assertEqual(reset['streams'][0]['read_cursor'], 2)
        rows = self.rows(3)
        rows[-1]['kind_name'] = 'image'
        self.source.write_text(json.dumps(rows), encoding='utf-8')
        media = self.command('collect', ok=False)
        self.assertEqual(media['streams'][0]['error_code'], 'unsupported_message')
        self.assertEqual(media['streams'][0]['read_cursor'], 2)
        self.assertEqual(media['pending_count'], 2)

    def test_partial_scan_restart_keeps_fixed_snapshot_before_newer_rows(self):
        self.initialize(page_size=2)
        self.command('enable', '--talker', 'synthetic-room')
        self.rows(3)
        self.env['CLIENT_TEST_FAIL_AFTER'] = '2'
        failed = self.command('collect', ok=False)
        self.assertEqual(failed['pending_count'], 2)
        self.assertEqual(failed['streams'][0]['read_cursor'], 2)
        self.assertEqual(failed['streams'][0]['snapshot_high'], 3)
        del self.env['CLIENT_TEST_FAIL_AFTER']
        self.rows(5)
        self.command('collect')
        self.assertEqual(self.command('status')['pending_count'], 3)
        self.command('collect')
        self.assertEqual(self.command('status')['pending_count'], 5)

    def test_two_collectors_cannot_duplicate_the_same_source_events(self):
        self.initialize(page_size=2)
        self.command('enable', '--talker', 'synthetic-room')
        self.rows(5)
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.command('collect'), range(2)))
        self.assertTrue(all(not r['streams'][0]['error_code'] for r in results))
        events = self.command('pending')['events']
        self.assertEqual([e['message']['local_id'] for e in events], [1, 2, 3, 4, 5])


if __name__ == '__main__':
    unittest.main()
