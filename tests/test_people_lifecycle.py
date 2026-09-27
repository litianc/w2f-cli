import json,tempfile,unittest
from pathlib import Path
from agents.storage import MirrorStore
from agents.manage import Manager


class PeopleLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.config=root/'config.json';self.config.write_text('{}')
        self.store=MirrorStore(root/'state.sqlite3');self.store.initialize({'wechat_talker':'room','feishu_chat_id':'chat','lark_profile':'test'})
        self.manager=Manager(self.store,self.config);self.person=self.store.create_person('同名')
        self.pid=self.person['id']
    def action(self,op,**data):return self.manager.post('people',{'op':op,'confirm':True,**data})
    def test_unlink_reconfirm_and_archive_preserve_history(self):
        wx=self.store.link_wechat_identity(self.pid,'default','wx_a','昵称')
        self.store.upsert_message('room:7',{'talker':'room','local_id':7,'sender_wxid':'wx_a'},'text')
        self.store.mark_message('room:7','sent',sender_name='同名',sender_person_id=self.pid)
        self.action('unlink_wechat',identity_id=wx['id'],person_id=self.pid)
        self.assertFalse(self.store.resolve_sender('default','wx_a').get('confirmed_name'))
        pending=self.store.list_confirmations();self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]['binding_id'],1)
        self.action('unlink_wechat',identity_id=wx['id'],person_id=self.pid)
        self.assertEqual(len(self.store.list_confirmations()),1)
        with self.assertRaises(ValueError):self.action('delete',id=self.pid)
        self.action('archive',id=self.pid)
        self.assertEqual(self.store.list_people(),[])
        with self.assertRaises(ValueError):self.store.link_wechat_identity(self.pid,'default','wx_b')
        self.assertEqual(len(self.manager.get('people',{'status':'archived'})['people']),1)
        self.action('restore',id=self.pid)
        self.assertFalse(self.store.resolve_sender('default','wx_a').get('confirmed_name'))
        other=self.store.create_person('同名')
        self.store.link_wechat_identity(other['id'],'default','wx_a')
        self.assertEqual(self.store.resolve_sender('default','wx_a')['person_id'],other['id'])
        self.assertEqual(self.store.list_confirmations(),[])
    def test_empty_delete_and_linked_removal_blocked(self):
        wx=self.store.link_wechat_identity(self.pid,'one','wx_a')
        for op in ('delete','archive'):
            with self.assertRaises(ValueError):self.action(op,id=self.pid)
        self.assertEqual(self.store.resolve_sender('one','wx_a')['person_id'],self.pid)
        empty=self.store.create_person('可删除');self.action('delete',id=empty['id']);self.action('delete',id=empty['id'])
        self.assertFalse(any(p['id']==empty['id'] for p in self.store.list_people()))
    def test_scoped_unlink_and_admin_protection(self):
        wx=self.store.link_wechat_identity(self.pid,'one','wx_a');self.store.link_wechat_identity(self.pid,'two','wx_a')
        self.action('unlink_wechat',identity_id=wx['id'],person_id=self.pid)
        self.assertEqual(self.store.resolve_sender('two','wx_a')['person_id'],self.pid)
        profile=self.store.list_profiles()[0];fs=self.store.link_feishu_identity(self.pid,profile['id'],'ou_test')
        self.config.write_text(json.dumps({'admin_profile':profile['profile_name'],'admin_feishu_user_id':'ou_test'}))
        with self.assertRaisesRegex(ValueError,'管理员'):self.action('unlink_feishu',identity_id=fs['id'],person_id=self.pid)
        self.assertEqual(len(self.store.list_feishu_identities()),1)
        self.config.write_text('{}');self.action('unlink_feishu',identity_id=fs['id'],person_id=self.pid)
        self.action('unlink_feishu',identity_id=fs['id'],person_id=self.pid)
        self.assertEqual(self.store.list_feishu_identities(),[])
    def test_stale_owner_and_missing_confirmation_rejected(self):
        wx=self.store.link_wechat_identity(self.pid,'one','wx_a');other=self.store.create_person('其他')
        self.store.reassign_wechat_identity(wx['id'],self.pid,other['id'],'已核实')
        with self.assertRaises(ValueError):self.action('unlink_wechat',identity_id=wx['id'],person_id=self.pid)
        with self.assertRaises(ValueError):self.manager.post('people',{'op':'archive','id':self.pid})
        self.assertEqual(self.store.resolve_sender('one','wx_a')['person_id'],other['id'])

    def test_legacy_archived_feishu_not_selectable_and_default_admin_protected(self):
        self.store.upsert_profile('wechat-mirror','app-admin')
        profile=next(p for p in self.store.list_profiles() if p['profile_name']=='wechat-mirror')
        fs=self.store.link_feishu_identity(self.pid,profile['id'],'ou_admin')
        self.config.write_text(json.dumps({'admin_feishu_user_id':'ou_admin'}))
        with self.assertRaisesRegex(ValueError,'管理员'):
            self.action('unlink_feishu',identity_id=fs['id'],person_id=self.pid)
        with self.store.connect() as db:
            db.execute("UPDATE people SET status='archived' WHERE id=?",(self.pid,))
        self.assertEqual(self.manager.get('people')['feishu'],[])
        self.assertIsNone(self.store.mention_identity('default','wx_a','wechat-mirror'))

    def test_reopened_notification_has_new_generation_and_stale_finish_ignored(self):
        self.store.record_sender('one','wx_new','昵称',binding_id=1)
        initial=self.store.claim_confirmation_notification('one','wx_new')
        self.store.confirm_sender(initial['id'],'实名')
        wx=self.store.resolve_sender('one','wx_new')
        self.action('unlink_wechat',identity_id=wx['id'],person_id=wx['person_id'])
        reopened=self.store.list_confirmations()[0]
        self.assertEqual(reopened['id'],initial['id'])
        self.assertEqual(reopened['notification_generation'],1)
        self.store.finish_confirmation_notification(initial['id'],generation=0)
        self.assertEqual(self.store.list_confirmations()[0]['notification_status'],'pending')
        from agents import wechat_feishu_mirror as mirror
        from unittest.mock import patch
        with patch.object(mirror,'run_lark') as send:
            mirror.notify_confirmation({'admin_feishu_user_id':'ou_admin'},self.store,'one','wx_new')
            mirror.notify_confirmation({'admin_feishu_user_id':'ou_admin'},self.store,'one','wx_new')
        self.assertEqual(send.call_count,1)
        self.assertIn(f"mirror-confirm-{initial['id']}-v1",send.call_args.args[1])
        self.assertEqual(self.store.list_confirmations()[0]['notification_status'],'sent')

if __name__=='__main__':unittest.main()
