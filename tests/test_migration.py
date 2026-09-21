"""Schema migration must preserve committed WAL data and roll back on failure."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agents.storage import MirrorStore


class MigrationAcceptance(unittest.TestCase):
    def test_T56_v2_wal_backup_and_failed_upgrade_are_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MirrorStore(Path(directory)/'state.db')
            store.initialize({'wechat_talker':'room','feishu_chat_id':'chat','initial_cursor':88})
            with store.connect() as db:
                db.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
                db.execute('ALTER TABLE runtime_state DROP COLUMN initialized')
                db.execute('ALTER TABLE runtime_state DROP COLUMN recovery_json')
                db.execute("DELETE FROM schema_meta WHERE key='config_seeded'")
            # Keep a connection open so new committed rows remain in the WAL.
            writer=sqlite3.connect(store.path)
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('UPDATE runtime_state SET cursor=89');writer.commit()
            real_upgrade=store._upgrade_v3
            def fail(db):
                real_upgrade(db)
                raise RuntimeError('injected migration failure')
            with patch.object(store,'_upgrade_v3',side_effect=fail):
                with self.assertRaisesRegex(RuntimeError,'injected'):store.initialize()
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],'2')
                self.assertNotIn('initialized',{r[1] for r in db.execute('PRAGMA table_info(runtime_state)')})
            backup=next((store.path.parent/'backups').glob('*.bak'))
            reader=sqlite3.connect(backup)
            try:
                self.assertEqual(reader.execute('SELECT cursor FROM runtime_state').fetchone()[0],89)
            finally:
                reader.close()
            store.initialize({'wechat_talker':'room','feishu_chat_id':'chat','initial_cursor':1})
            self.assertEqual(store.get_cursor(),89)
            self.assertTrue(store.recovery(1)['initialized'])
            writer.close()
