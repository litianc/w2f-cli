import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from integrations.reader_files import read_file


class ReaderFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'file' / 'example.pdf'
        self.file.parent.mkdir()
        self.file.write_bytes(b'%PDF-fixture')
        self.digest = hashlib.md5(self.file.read_bytes()).hexdigest()
        self.source = self.root / 'message.db'
        self.raw = ('<msg><appmsg><type>74</type><title><![CDATA[example.pdf]]></title>'
                    '<appattach><totallen>12</totallen></appattach>'
                    f'<md5><![CDATA[{self.digest}]]></md5></appmsg></msg>')
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE Msg_fixture(local_id INTEGER,server_id INTEGER,create_time INTEGER,local_type INTEGER,message_content TEXT)')
            db.execute('INSERT INTO Msg_fixture VALUES(1,100,200,?,?)', ((74 << 32) | 49, self.raw))
            db.execute('ALTER TABLE Msg_fixture ADD COLUMN real_sender_id INTEGER DEFAULT 1')
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.execute("INSERT INTO Name2Id VALUES('wx_sender')")
        def connect(path):
            db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
            db.row_factory = sqlite3.Row
            return db
        self.lookups = []
        def resources(db, metadata, family, resource_type, local):
            self.lookups.append((metadata, family, local))
            return [{'local_paths': [str(self.file)]}]
        self.db = SimpleNamespace(message_dbs=[self.source], connect=connect, resource_roots=[self.root])
        self.reader = SimpleNamespace(resolve_username=lambda db, chat: chat,
            message_table=lambda chat: 'Msg_fixture', message_projection=lambda db, table: '*',
            decode_message_content=lambda row: row['message_content'], hardlink_resources=resources,
            visible_message_content=lambda text, talker, sender: text.removeprefix(sender + ':\n'),
            ReaderError=lambda message, code: RuntimeError(code))
        self.args = SimpleNamespace(talker='room', local_id=1, server_id=100, create_time=200)

    def test_cdata_digest_is_extracted_and_size_and_bytes_verified(self):
        result = read_file(self.reader, self.db, self.args)
        self.assertEqual(self.lookups, [({'md5': self.digest}, 'file', True)])
        self.assertEqual(result['contract'], 'file_resource_v1')
        self.assertEqual(len(result['resources']), 1)
        self.assertEqual(result['file']['name'], 'example.pdf')

    def test_source_time_or_server_id_mismatch_is_rejected(self):
        for attr in ('server_id', 'create_time'):
            args = SimpleNamespace(**vars(self.args))
            setattr(args, attr, 999)
            with self.subTest(attr=attr), self.assertRaisesRegex(RuntimeError, 'file_identity_mismatch'):
                read_file(self.reader, self.db, args)
        self.assertEqual(self.lookups, [])

    def test_wrong_bytes_or_partial_download_return_no_resource(self):
        for payload in (b'%PDF-wrong!!', b'%PDF-'):
            self.file.write_bytes(payload)
            self.assertEqual(read_file(self.reader, self.db, self.args)['resources'], [])

    def test_missing_digest_does_not_search_by_filename(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE Msg_fixture SET message_content=?', (self.raw.replace(self.digest, ''),))
        self.assertEqual(read_file(self.reader, self.db, self.args)['resources'], [])
        self.assertEqual(self.lookups, [])

    def test_symlink_outside_resource_root_is_rejected(self):
        self.db.resource_roots = [self.file.parent]
        other = self.root / 'outside.pdf'
        other.write_bytes(self.file.read_bytes())
        self.file.unlink()
        self.file.symlink_to(other)
        self.assertEqual(read_file(self.reader, self.db, self.args)['resources'], [])

    def completed_source(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE Msg_fixture SET server_id=101,local_type=?,message_content=?', ((6 << 32) | 49, self.raw.replace('<type>74</type>', '<type>6</type>')))
        self.args.pending_file_metadata = json.dumps({'name': 'example.pdf', 'size': 12, 'md5': self.digest, 'sender_wxid': 'wx_sender'})

    def test_pending_file_completion_requires_same_metadata_and_sender(self):
        self.completed_source()
        data = read_file(self.reader, self.db, self.args)
        self.assertEqual(data['source_server_id'], 101)
        self.assertTrue(data['completion_verified'])
        self.assertEqual(len(data['resources']), 1)

    def test_completion_rejects_changed_digest_name_size_or_sender(self):
        self.completed_source()
        original = json.loads(self.args.pending_file_metadata)
        for key, value in [('md5', '0' * 32), ('name', 'other.pdf'), ('size', 13), ('sender_wxid', 'wx_other')]:
            with self.subTest(key=key):
                self.args.pending_file_metadata = json.dumps({**original, key: value})
                with self.assertRaisesRegex(RuntimeError, 'file_identity_mismatch'):
                    read_file(self.reader, self.db, self.args)

    def test_group_sender_prefix_before_xml_declaration_is_removed(self):
        self.completed_source()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE Msg_fixture SET message_content=?', ('wx_sender:\n<?xml version="1.0"?>\n' + self.raw.replace('<type>74</type>', '<type>6</type>'),))
        self.assertEqual(len(read_file(self.reader, self.db, self.args)['resources']), 1)


if __name__ == '__main__':
    unittest.main()
