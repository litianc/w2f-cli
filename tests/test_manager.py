import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agents.manage import Manager,profile_list,preflight_binding,setup_init,write_runtime_config
from agents.storage import MirrorStore


class ManagementAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=MirrorStore(self.root/'state.db');self.store.initialize()
        self.store.upsert_profile('p','app');self.manager=Manager(self.store,self.root/'config.json')
    def tearDown(self):self.temp.cleanup()
    def bind(self):return self.store.create_binding({'wechat_chat_name':'源','wechat_talker':'room','feishu_chat_name':'目标','feishu_chat_id':'chat','lark_profile':'p'})
    def test_config_credentials_and_untrusted_fields_never_saved(self):
        self.manager.post('setup',{'op':'save','wechat_talker':'room','app_secret':'secret','sqlite_file':'/outside','refreshToken':'secret'})
        value=self.store.setting('setup_draft');self.assertEqual(value,{'wechat_talker':'room'})
    def test_setup_save_applies_runtime_wakeup_and_verified_admin(self):
        self.bind()
        person=self.store.create_person('管理员')
        profile=next(p for p in self.store.list_profiles() if p['profile_name']=='p')
        self.store.link_feishu_identity(person['id'],profile['id'],'ou_admin')
        self.manager.config_path.write_text(json.dumps({'wechat_reader':'/tmp/reader', 'realtime_wakeup':True, 'realtime_stream_url':'http://127.0.0.1:10392', 'sender_map':{'wx':'实名'}}))
        result=self.manager.post('setup',{'op':'save','reader_wakeup':'true','poll_interval':600,
                                         'admin_profile':'p','admin_feishu_user_id':'ou_admin'})
        stored=json.loads((self.root/'config.json').read_text())
        self.assertEqual(stored['reader_wakeup'],'true')
        self.assertEqual(stored['admin_feishu_user_id'],'ou_admin')
        self.assertEqual(stored['wechat_reader'],'/tmp/reader')
        self.assertTrue(stored['realtime_wakeup'])
        self.assertEqual(stored['realtime_stream_url'],'http://127.0.0.1:10392')
        self.assertEqual(stored['sender_map'],{'wx':'实名'})
        self.assertEqual(result['draft']['admin_profile'],'p')
        with self.assertRaisesRegex(ValueError,'管理员身份尚未关联'):
            self.manager.post('setup',{'op':'save','admin_feishu_user_id':'ou_other','admin_profile':'p'})
        self.assertEqual(json.loads((self.root/'config.json').read_text())['admin_feishu_user_id'],'ou_admin')

    def test_profile_external_rename_preserves_app_external_replace_stops(self):
        self.bind();self.store.set_control(1,'running')
        with patch('agents.manage.run_cli',return_value=[{'name':'renamed','appId':'app'}]):profile_list(self.store)
        self.assertEqual(self.store.get_binding(1)['lark_profile'],'renamed')
        with patch('agents.manage.run_cli',return_value=[{'name':'renamed','appId':'app2'}]):profile_list(self.store)
        self.assertEqual(self.store.get_binding(1)['lark_app_id'],'app')
        self.assertEqual(self.store.get_control(1)['status'],'stopped')
        self.assertFalse(self.store.get_binding(1)['enabled'])
    def test_same_name_people_distinct_and_explicit_link_closes_confirmation(self):
        self.bind()
        self.store.record_sender('default','wx1','昵称',binding_id=1)
        self.store.record_sender('default','wx2','昵称',binding_id=1)
        for item in self.store.list_confirmations():self.store.confirm_sender(item['id'],'张三')
        self.assertEqual(len(self.store.list_people()),2)
        first=self.store.list_people()[0]['id'];self.store.link_wechat_identity(first,'default','wx2')
        self.assertEqual(self.store.resolve_sender('default','wx2')['person_id'],first)
    def test_archive_allows_new_target_binding_without_overwriting_history(self):
        old=self.bind();self.store.set_cursor(500);self.store.archive_binding(old['id'])
        new=self.bind();self.assertNotEqual(old['id'],new['id']);self.assertEqual(self.store.get_cursor(old['id']),500);self.assertFalse(self.store.recovery(new['id'])['initialized'])
    def test_source_and_target_preflight_fail_closed(self):
        self.bind()
        with patch('agents.wechat_feishu_mirror.source_page',side_effect=RuntimeError('unavailable')),patch('agents.manage.run_cli',side_effect=RuntimeError('no access')):
            result=preflight_binding({'lark_profile':'p','wechat_talker':'new','feishu_chat_id':'new'},self.store)
        self.assertFalse(result['ok']);self.assertFalse(result['checks']['source_readable'])
    def test_duplicate_unknown_id_across_groups_records_both_groups(self):
        self.bind();self.store.create_binding({'wechat_chat_name':'源2','wechat_talker':'room2','feishu_chat_name':'目标2','feishu_chat_id':'chat2','lark_profile':'p'})
        for bid in (1,2,1):self.store.record_sender('default','unknown','昵称',binding_id=bid)
        entries=self.store.list_confirmations();self.assertEqual(len(entries),1);self.assertEqual({g['id'] for g in entries[0]['groups']},{1,2})
    def test_display_names_not_overwritten_by_config_reload(self):
        self.bind();self.store.rename_binding(1,wechat_chat_name='更名')
        self.store.initialize({'wechat_talker':'room','wechat_chat_name':'旧名','feishu_chat_id':'chat'})
        self.assertEqual(self.store.get_binding(1)['wechat_chat_name'],'更名')

    def test_receipt_confirmation_rejects_other_chat_before_unblocking(self):
        self.bind()
        mid=self.store.upsert_message('room:1',{'local_id':1,'text':'正文'},'text')
        self.store.prepare_delivery(mid,'text-0',['+messages-send','--text','姓名：正文'],'uuid')
        self.store.set_delivery(mid,'text-0','uncertain')
        args={'op':'receipt','message_id':mid,'part':'text-0','remote_id':'om_real','confirm':True}
        response={'data':{'messages':[{'message_id':'om_real','chat_id':'another','sender':{'id':'app'},'content':'姓名：正文'}]}}
        with patch('agents.manage.run_cli',return_value=response):
            with self.assertRaisesRegex(ValueError,'目标群'):self.manager.post('uncertain',args)
            self.assertEqual(self.store.delivery(mid,'text-0')['state'],'uncertain')
            response['data']['messages'][0]['chat_id']='chat'
            self.manager.post('uncertain',args)
        self.assertEqual(self.store.delivery(mid,'text-0')['state'],'sent')

    def test_http_api_requires_session_and_local_origin(self):
        import threading
        import http.client
        from http.server import ThreadingHTTPServer
        from agents.manage import Handler
        handler=type('TestHandler',(Handler,),{'manager':self.manager,'token':'test-session'})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(headers):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            client.request('GET','/api/status',headers=headers)
            response=client.getresponse();body=response.read();client.close();return response.status,body
        try:
            self.assertEqual(request({})[0],401)
            self.assertEqual(request({'X-Mirror-Admin':'test-session','Origin':'https://example.test'})[0],401)
            self.assertEqual(request({'X-Mirror-Admin':'test-session','Host':'attacker.test'})[0],401)
            code,body=request({'X-Mirror-Admin':'test-session'})
            self.assertEqual(code,200);self.assertNotIn(b'test-session',body)
        finally:server.shutdown();server.server_close();thread.join()
