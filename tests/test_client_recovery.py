"""The persisted pre-upload state and public receipt boundary survive upgrade/retry."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

from client.collector import CollectionError, Collector
from client.upload import Uploader
from w2f_protocol.ingest import VERSION, digest


class ClientRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='w2f-client-upgrade-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stream = str(uuid.uuid4())
        self.event = dict(event_id=str(uuid.uuid4()), stream_id=self.stream, sequence=1,
            message=dict(local_id=12, talker='synthetic-room', text='迁移保留 😀',create_time=10,kind_name='text',local_type=1))
        # Published v040-1 disk format, intentionally independent of the current initializer.
        with sqlite3.connect(self.root / 'collector.sqlite3') as db:
            db.executescript('''
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE streams(id TEXT PRIMARY KEY,talker TEXT UNIQUE NOT NULL,
                    read_cursor INTEGER NOT NULL,scan_base INTEGER,snapshot_high INTEGER,
                    page_time INTEGER,page_id INTEGER,error_code TEXT,error TEXT);
                CREATE TABLE outbox(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
                    stream_id TEXT NOT NULL,local_id INTEGER NOT NULL,payload TEXT NOT NULL,payload_bytes INTEGER NOT NULL,
                    UNIQUE(stream_id,local_id));
            ''')
            db.executemany('INSERT INTO metadata VALUES(?,?)', dict(format='w2f-client-v040-1',device_id=str(uuid.uuid4()),
                source_account='synthetic-account',reader='/synthetic-reader',page_size='100',max_pending_bytes='4096').items())
            db.execute('INSERT INTO streams(id,talker,read_cursor) VALUES(?,?,12)',(self.stream,'synthetic-room'))
            payload=json.dumps(self.event['message'],ensure_ascii=False)
            db.execute('INSERT INTO outbox VALUES(1,?,?,?,?,?)',(self.event['event_id'],self.stream,12,payload,len(payload.encode())))
        self.collector=Collector(self.root)
        self.response=dict(protocol=VERSION,generation=str(uuid.uuid4()),receipts=[dict(
            event_id=self.event['event_id'],payload_hash=digest(self.event),binding_id=str(uuid.uuid4()),state='stored')])

    def test_upgrade_preserves_identity_cursor_and_pending_then_first_ack_time(self):
        self.assertEqual(self.collector.pending()['events'],[self.event])
        self.assertEqual(self.collector.status()['streams'][0]['read_cursor'],12)
        Uploader(self.collector,clock=lambda:100).record_receipt([self.event],self.response)
        Uploader(Collector(self.root),clock=lambda:200).record_receipt([self.event],self.response)
        self.assertEqual(self.collector.pending()['events'],[])
        self.assertEqual(Uploader(self.collector).retained()['events'][0]['acknowledged_at'],100)
        self.assertEqual(self.collector.status()['retained_count'],1)

    def test_incomplete_or_changed_receipt_cannot_acknowledge_local_data(self):
        self.response['receipts'][0]['payload_hash']='different'
        with self.assertRaises(CollectionError):
            Uploader(self.collector).record_receipt([self.event],self.response)
        self.assertEqual(self.collector.pending()['events'],[self.event])
        self.assertEqual(self.collector.status()['retained_count'],0)

    def test_generation_change_quarantines_copies_instead_of_reacknowledging(self):
        uploader=Uploader(self.collector,clock=lambda:100)
        uploader.record_receipt([self.event],self.response)
        self.response['generation']=str(uuid.uuid4())
        with self.assertRaises(CollectionError) as caught:
            uploader.record_receipt([self.event],self.response)
        self.assertEqual(caught.exception.code,'recovery_required')
        status=self.collector.status()
        self.assertTrue(status['recovery_required'])
        self.assertEqual(status['quarantined_count'],1)
        self.assertEqual(uploader.retained()['events'][0]['acknowledged_at'],100)

    def test_upgraded_status_remains_readable_during_a_writer_transaction(self):
        self.collector.status()
        with sqlite3.connect(self.root / 'collector.sqlite3') as writer:
            writer.execute('BEGIN IMMEDIATE')
            result = subprocess.run([sys.executable, '-m', 'client', '--state-dir', str(self.root), 'status'],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8', timeout=3)
            self.assertEqual(result.returncode,0,result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['pending_count'],1)


if __name__ == '__main__':
    unittest.main()
