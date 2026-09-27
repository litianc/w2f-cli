import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agents.manage import Manager,profile_list,preflight_binding,setup_init,write_runtime_config,run_cli,main
from agents.storage import MirrorStore


class ManagementAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=MirrorStore(self.root/'state.db');self.store.initialize()
        self.store.upsert_profile('p','app');self.manager=Manager(self.store,self.root/'config.json')
    def tearDown(self):self.temp.cleanup()
    def bind(self):return self.store.create_binding({'wechat_chat_name':'源','wechat_talker':'room','feishu_chat_name':'目标','feishu_chat_id':'chat','lark_profile':'p'})
    def test_wizard_group_list_uses_contacts_when_query_is_empty(self):
        calls=[]
        def reader(argv,**kwargs):
            calls.append(argv[1:])
            if argv[1]=='contacts':
                return {'ok':True,'data':{'contacts':[{'username':'room@chatroom','display_name':'测试群'}]}}
            if argv[1]=='resolve-chat' and not argv[2]:
                raise RuntimeError('CLI 请求失败')
            return {'ok':True,'data':{'candidates':[{'username':'room@chatroom','display_name':'测试群'}]}}
        with patch('agents.manage.run_cli',side_effect=reader):
            self.assertEqual(self.manager.get('wizard/wechat')[0]['display_name'],'测试群')
            self.assertEqual(self.manager.get('wizard/wechat',{'q':'测试'})[0]['username'],'room@chatroom')
        self.assertEqual(calls[0][0],'contacts')
        self.assertEqual(calls[1][0:2],['resolve-chat','测试'])

    def test_reader_json_error_from_stdout_is_not_hidden(self):
        response=type('Process',(),{'returncode':1,'stdout':json.dumps({'ok':False,'error':{'code':'missing_required_argument','message':'resolve-chat 需要 query'}}),'stderr':'bash locale warning'})()
        with patch('agents.manage.subprocess.run',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'resolve-chat 需要 query'):
                run_cli(['reader','resolve-chat',''])

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

    def test_feishu_identity_cannot_be_linked_with_empty_id(self):
        person=self.store.create_person('测试人员')
        profile=next(p for p in self.store.list_profiles() if p['profile_name']=='p')
        for value in ('','not-an-open-id'):
            with self.assertRaisesRegex(ValueError,'open_id'):
                self.manager.post('people',{'op':'link_feishu','person_id':person['id'],
                                            'profile_id':profile['id'],'open_id':value})
        self.assertEqual(self.store.list_feishu_identities(),[])

    def test_contact_candidates_are_app_scoped_deduplicated_and_not_auto_linked(self):
        auth={'appId':'app','identities':{'user':{'available':True}}}
        contacts={'ok':True,'data':{'users':[
            {'open_id':'ou_exact','localized_name':'王小明','department':'产品','matched_query':'王小明'},
            {'open_id':'ou_exact','localized_name':'王小明','department':'产品','matched_query':'wangxiaoming'},
            {'open_id':'ou_other','localized_name':'王小鸣','department':'研发','matched_query':'王小'},
            {'open_id':'bad','localized_name':'无效','matched_query':'王小'}],
            'queries':[{'query':'王小','has_more':True}]}}
        with patch('agents.manage.run_cli',side_effect=[auth,contacts]) as cli:
            result=self.manager.get('people/search-feishu',{'profile':'p','name':'王小明'})
        argv=cli.call_args_list[1].args[0]
        self.assertEqual(argv[:6],['lark-cli','--profile','p','contact','+search-user','--as'])
        self.assertIn('--as',argv);self.assertIn('user',argv)
        self.assertEqual(argv[argv.index('--queries')+1],'王小明,王小,小明,wangxiaoming,wxm')
        self.assertEqual([c['open_id'] for c in result['candidates']],['ou_exact','ou_other'])
        self.assertEqual(result['candidates'][0]['matched_queries'],['王小明','wangxiaoming'])
        self.assertTrue(result['has_more'])
        self.assertEqual(self.store.list_feishu_identities(),[])

    def test_contact_search_manual_pinyin_and_authorization_errors(self):
        auth={'appId':'app','identities':{'user':{'available':True}}}
        with patch('agents.manage.run_cli',side_effect=[auth,{'data':{'users':[]}}]) as cli:
            result=self.manager.get('people/search-feishu',{'profile':'p','name':'王小明','q':'li wenhao'})
        self.assertEqual(result['searched'],['li wenhao'])
        self.assertIn('--query',cli.call_args_list[1].args[0])
        for value in ('a,b','x'*51):
            with self.assertRaisesRegex(ValueError,'搜索词'):
                self.manager.get('people/search-feishu',{'profile':'p','name':'王小明','q':value})
        with patch('agents.manage.run_cli',return_value={'appId':'other','identities':{'user':{'available':True}}}):
            with self.assertRaisesRegex(ValueError,'App ID'):
                self.manager.get('people/search-feishu',{'profile':'p','name':'王小明'})
        with patch('agents.manage.run_cli',return_value={'appId':'app','identities':{'user':{'available':False}}}) as cli:
            with self.assertRaisesRegex(ValueError,'用户授权'):
                self.manager.get('people/search-feishu',{'profile':'p','name':'王小明'})
        self.assertEqual(cli.call_count,1)

    def test_cli_people_search_uses_shared_read_only_manager(self):
        argv=['agents.manage','--config',str(self.root/'config.json'),'--db',str(self.store.path),
              'people','search-feishu','--profile-name','p','--name','王小明','--query','wangxiaoming']
        with patch('sys.argv',argv),patch('sys.stdout',new_callable=io.StringIO) as output,\
             patch('agents.manage.search_feishu_people',return_value={'candidates':[]}) as search:
            self.assertEqual(main(),0)
        self.assertEqual(json.loads(output.getvalue()),{'candidates':[]})
        search.assert_called_once_with(unittest.mock.ANY,'p','王小明','wangxiaoming')

    def test_feishu_id_cannot_silently_move_between_people(self):
        first=self.store.create_person('同名')
        second=self.store.create_person('同名')
        profile=self.store.list_profiles()[0]
        self.store.link_feishu_identity(first['id'],profile['id'],'ou_same')
        with self.assertRaisesRegex(ValueError,'已关联其他人员'):
            self.store.link_feishu_identity(second['id'],profile['id'],'ou_same')
        self.assertEqual(self.store.list_feishu_identities()[0]['person_id'],first['id'])

    def test_profile_self_discovery_is_app_scoped_and_read_only(self):
        response={'appId':'app','verified':True,'identities':{'user':{'openId':'ou_self','userName':'本人','available':False,'tokenStatus':'expired'}}}
        with patch('agents.manage.run_cli',return_value=response) as cli:
            result=self.manager.get('profiles/self',{'profile':'p'})
        self.assertEqual(result['open_id'],'ou_self')
        self.assertEqual(result['display_name'],'本人')
        self.assertFalse(result['available'])
        self.assertEqual(result['user_status'],'expired')
        self.assertEqual(len(self.store.list_feishu_identities()),0)
        self.assertEqual(cli.call_args.args[0],['lark-cli','--profile','p','auth','status','--json','--verify'])
        self.assertEqual(set(result),{'profile_name','open_id','display_name','available','user_status'})

    def test_profile_self_discovery_rejects_replaced_app_and_missing_identity(self):
        with patch('agents.manage.run_cli',return_value={'appId':'different','identities':{'user':{'openId':'ou_other'}}}):
            with self.assertRaisesRegex(ValueError,'App ID'):
                self.manager.get('profiles/self',{'profile':'p'})
        with patch('agents.manage.run_cli',return_value={'appId':'app','identities':{'user':{'status':'missing'}}}):
            result=self.manager.get('profiles/self',{'profile':'p'})
        self.assertIsNone(result['open_id'])
        self.assertEqual(result['user_status'],'missing')

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
        wx2=self.store.resolve_sender('default','wx2')
        first=next(p['id'] for p in self.store.list_people() if p['id']!=wx2['person_id'])
        with self.assertRaisesRegex(ValueError,'已关联其他人员'):
            self.store.link_wechat_identity(first,'default','wx2')
        self.store.reassign_wechat_identity(wx2['id'],wx2['person_id'],first,'核对后发现是同一人')
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
