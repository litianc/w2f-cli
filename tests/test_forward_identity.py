import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch
from agents.storage import MirrorStore
from agents import wechat_feishu_mirror as m
from integrations.forward_records import parse_record


class ForwardIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MirrorStore(Path(self.tmp.name)/'state.sqlite3')
        self.config={'wechat_talker':'room','feishu_chat_id':'chat','lark_profile':'test','source_account':'one'}
        self.store.initialize(self.config)
        p=self.store.create_person('外层实名')
        self.store.link_wechat_identity(p['id'],'one','outer')

    def test_repeated_nested_nickname_only_forward_has_no_identity_side_effects(self):
        raw='<datalist><dataitem datatype="1"><sourcename>同名</sourcename><datadesc>正文</datadesc></dataitem><dataitem datatype="17"><sourcename>同名</sourcename><recorditem>&lt;datalist&gt;&lt;dataitem datatype="1"&gt;&lt;sourcename&gt;同名&lt;/sourcename&gt;&lt;datadesc&gt;内层&lt;/datadesc&gt;&lt;/dataitem&gt;&lt;/datalist&gt;</recorditem></dataitem></datalist>'
        items=parse_record(raw);baseline=self.store.list_wechat_identities()
        with patch.object(m,'run_lark',return_value={'data':{'message_id':'om_fixture'}}) as send,patch.object(m,'notify_confirmation') as notify:
            for lid in (28,29):
                m.send_row(self.config,{'talker':'room','local_id':lid,'sender_wxid':'outer','kind_name':'forward_chat','forward_items':items}, {},self.store)
            self.assertEqual(send.call_count,2)
            self.assertIn('同名：内层',send.call_args.args[1][send.call_args.args[1].index('--text')+1])
            notify.assert_not_called()
        self.assertEqual(self.store.list_wechat_identities(),baseline)
        self.assertEqual(self.store.list_confirmations(),[])
        self.assertEqual(len(self.store.list_people()),1)

    def test_stable_ids_reuse_account_mapping_and_ignore_group_identifiers(self):
        for account,name in [('one','第一账号实名'),('two','第二账号实名')]:
            p=self.store.create_person(name);self.store.link_wechat_identity(p['id'],account,'inner')
        items=parse_record('<datalist><dataitem datatype="1"><sourcename>昵称</sourcename><dataitemsource><realchatname>group@chatroom</realchatname><fromusr>inner</fromusr></dataitemsource><datadesc>正文</datadesc></dataitem></datalist>')
        self.assertEqual(items[0]['sender_id'],'inner')
        body=m.forward_body({'forward_items':items},self.config,self.store)
        self.assertIn('第一账号实名',body);self.assertNotIn('第二账号实名',body)
        self.assertEqual(self.store.list_confirmations(),[])

    def test_real_unknown_outer_still_confirms_once(self):
        with patch.object(m,'run_lark',return_value={'data':{'message_id':'om_fixture'}}),patch.object(m,'notify_confirmation'):
            for lid in (8,9):
                m.send_row(self.config,{'talker':'room','local_id':lid,'sender_wxid':'real_unknown','sender':'昵称','text':'正文'}, {},self.store)
        self.assertEqual(len(self.store.list_confirmations()),1)

    def test_legacy_forward_records_quarantined_without_deleting_people(self):
        fake='unidentified:room:28:forward:2'
        self.store.record_sender('one',fake,'昵称',binding_id=1)
        # Seed the old bug directly; new record_sender must reject this input.
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO wechat_identities(source_account,wechat_sender_id,last_display_name,created_at,updated_at) VALUES('one',?,'昵称',0,0)",(fake,))
            db.execute("INSERT OR IGNORE INTO confirmations(source_account,wechat_sender_id,display_name,binding_id,created_at,updated_at) VALUES('one',?,'昵称',1,0,0)",(fake,))
        with self.store.connect() as db:
            db.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
            db.execute("UPDATE wechat_identities SET person_id=1 WHERE wechat_sender_id=?",(fake,))
        before=self.store.list_people()
        self.store.initialize()
        self.assertFalse(any(x['wechat_sender_id']==fake for x in self.store.list_wechat_identities()))
        self.assertEqual(self.store.list_confirmations(),[])
        self.assertIsNone(self.store.resolve_sender('one',fake))
        self.assertEqual(self.store.list_people(),before)
        self.assertEqual(self.store.list_quarantined_identities()[0]['person_id'],1)
        backups=list((self.store.path.parent/'backups').glob('*.bak'))
        self.assertEqual(len(backups),1)
        import sqlite3
        with closing(sqlite3.connect(backups[0])) as old:
            self.assertEqual(old.execute("SELECT status FROM wechat_identities WHERE wechat_sender_id=?",(fake,)).fetchone()[0],'pending')
        rejected=self.store.list_confirmations('rejected')
        self.assertEqual(len(rejected),1)
        with self.assertRaises(ValueError):self.store.confirm_sender(rejected[0]['id'],'不应新建')
        self.store.initialize();self.assertEqual(self.store.list_people(),before)

    def test_migration_failure_rolls_back_quarantine_and_version(self):
        import sqlite3
        fake='unidentified:room:28:forward:2'
        with self.store.connect() as db:
            db.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
            db.execute("INSERT INTO wechat_identities(source_account,wechat_sender_id,last_display_name,created_at,updated_at) VALUES('one',?,'昵称',0,0)",(fake,))
        original=self.store._quarantine_forward_identities
        def fail(db):
            original(db)
            raise RuntimeError('injected migration failure')
        with patch.object(self.store,'_quarantine_forward_identities',side_effect=fail):
            with self.assertRaises(RuntimeError):self.store.initialize()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],'3')
            self.assertEqual(db.execute("SELECT status FROM wechat_identities WHERE wechat_sender_id=?",(fake,)).fetchone()[0],'pending')
        self.assertTrue(list((self.store.path.parent/'backups').glob('*.bak')))

if __name__=='__main__':unittest.main()
