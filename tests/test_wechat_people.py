"""微信身份候选与防误关联验收：全部使用模拟数据。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.manage import Manager
from agents.storage import MirrorStore


class WechatPeopleAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.store=MirrorStore(root/'state.db')
        self.store.initialize()
        self.store.upsert_profile('mirror','app_mirror')
        self.binding=self.store.create_binding({'source_account':'my_wechat','wechat_chat_name':'项目群',
            'wechat_talker':'room@chatroom','feishu_chat_name':'目标群','feishu_chat_id':'oc_group',
            'lark_profile':'mirror'})
        self.manager=Manager(self.store,root/'config.json')

    def test_group_members_are_candidates_not_automatically_linked(self):
        response={'ok':True,'data':{'members':[
            {'username':'wx_a','display_name':'小明','nick_name':'小明','remark':'王小明','alias':'wxno_a'},
            {'username':'wx_b','display_name':'小明','nick_name':'小明','remark':'','alias':''},
            {'username':'room@chatroom','display_name':'群'}]}}
        with patch('agents.manage.run_cli',return_value=response) as cli:
            found=self.manager.get('people/search-wechat',{'binding_id':str(self.binding['id']),'q':'wangxiaoming'})
        self.assertEqual(found['source_account'],'my_wechat')
        self.assertEqual([x['wechat_sender_id'] for x in found['candidates']],['wx_a'])
        self.assertEqual(found['candidates'][0]['remark'],'王小明')
        self.assertEqual(self.store.list_wechat_identities(),[])
        self.assertIn('--chatroom-id',cli.call_args.args[0])
        self.assertNotIn('王小明',cli.call_args.args[0])
        with patch('agents.manage.run_cli',return_value=response):
            all_members=self.manager.get('people/search-wechat',{'binding_id':str(self.binding['id'])})
        self.assertEqual({x['wechat_sender_id'] for x in all_members['candidates']},{'wx_a','wx_b'})

    def test_contact_search_is_scoped_and_requires_query(self):
        payload={'ok':True,'data':{'contacts':[
            {'username':'wx_a','display_name':'阿明','remark':'王小明','nick_name':'小明'},
            {'username':'room@chatroom','display_name':'项目群'}]}}
        with self.assertRaisesRegex(ValueError,'填写昵称'):
            self.manager.get('people/search-wechat',{'source_account':'my_wechat'})
        with self.assertRaisesRegex(ValueError,'不属于当前配置'):
            self.manager.get('people/search-wechat',{'source_account':'foreign','q':'小明'})
        with patch('agents.manage.run_cli',return_value=payload) as cli:
            found=self.manager.get('people/search-wechat',{'source_account':'my_wechat','q':'王小'})
        self.assertEqual([c['wechat_sender_id'] for c in found['candidates']],['wx_a'])
        self.assertEqual(found['candidates'][0]['source_account'],'my_wechat')
        self.assertIn('--friends-only',cli.call_args.args[0])

    def test_cross_group_identity_reuses_one_person_and_rejects_namesake(self):
        first=self.store.create_person('王小明')
        namesake=self.store.create_person('王小明')
        self.store.record_sender('my_wechat','wx_a','小明',binding_id=self.binding['id'])
        pending=self.store.list_confirmations()[0]
        linked=self.manager.post('confirmations',{'op':'confirm','id':pending['id'],'person_id':first['id']},'web')
        self.assertEqual(linked['person_id'],first['id'])
        second=self.store.create_binding({'source_account':'my_wechat','wechat_chat_name':'另一个群',
            'wechat_talker':'another@chatroom','feishu_chat_name':'另一个目标','feishu_chat_id':'oc_other',
            'lark_profile':'mirror'})
        self.store.record_sender('my_wechat','wx_a','换了昵称',binding_id=second['id'])
        self.assertEqual(self.store.resolve_sender('my_wechat','wx_a')['status'],'confirmed')
        self.assertEqual(self.store.list_confirmations(),[])
        with self.assertRaisesRegex(ValueError,'已关联其他人员'):
            self.manager.post('people',{'op':'link_wechat','person_id':namesake['id'],
                'source_account':'my_wechat','wechat_sender_id':'wx_a'},'web')
        self.assertEqual(self.store.resolve_sender('my_wechat','wx_a')['person_id'],first['id'])
        with self.assertRaisesRegex(ValueError,'已确认'):
            self.store.confirm_sender(pending['id'],'另一个姓名')

    def test_explicit_reassignment_requires_confirmation_reason_and_current_owner(self):
        a=self.store.create_person('甲');b=self.store.create_person('乙')
        identity=self.store.link_wechat_identity(a['id'],'my_wechat','wx_a','原昵称',actor='web')
        request={'op':'reassign_wechat','identity_id':identity['id'],'from_person_id':a['id'],
                 'to_person_id':b['id'],'reason':'本人核对后更正'}
        with self.assertRaisesRegex(ValueError,'明确确认'):
            self.manager.post('people',request,'web')
        with self.assertRaisesRegex(ValueError,'更正原因'):
            self.manager.post('people',{**request,'reason':'','confirm':True},'web')
        result=self.manager.post('people',{**request,'confirm':True},'web')
        self.assertEqual(result['person_id'],b['id'])
        with self.assertRaisesRegex(ValueError,'归属已变化'):
            self.manager.post('people',{**request,'confirm':True},'web')
        actions=self.store.actions()
        move=next(x for x in actions if x['action']=='reassign_wechat_identity')
        self.assertEqual(move['actor'],'web')
        details=json.loads(move['details_json'])
        self.assertEqual(details['before']['person_id'],a['id'])
        self.assertEqual(details['after']['person_id'],b['id'])

    def test_invalid_or_unidentified_sender_cannot_be_linked(self):
        person=self.store.create_person('甲')
        for sender in ('','unidentified:room:1','room@chatroom','has space'):
            with self.subTest(sender=sender),self.assertRaisesRegex(ValueError,'稳定发送者 ID'):
                self.store.link_wechat_identity(person['id'],'my_wechat',sender)
        self.assertEqual(self.store.list_wechat_identities(),[])


if __name__=='__main__':unittest.main()
