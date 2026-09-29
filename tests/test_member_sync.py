import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agents.storage import MirrorStore
from agents.manage import Manager
from agents.member_sync import affected_members,process_member_tasks,retry_member_task


def event(local=11,operation='add',wxid='wx_target',actor='wx_actor',names=None):
    template='$username$邀请$names$加入了群聊' if operation=='add' else '$username$将$names$移出了群聊'
    targets=''.join(f'<member><username>{x}</username><nickname>同名</nickname></member>' for x in (names or [wxid]))
    xml=f'<sysmsg type="sysmsgtemplate"><content_template><template>{template}</template><link_list><link name="names"><memberlist>{targets}</memberlist></link><link name="username"><memberlist><member><username>{actor}</username></member></memberlist></link></link_list></content_template></sysmsg>'
    return {'talker':'room','local_id':local,'local_type':10000,'kind_name':'system','content':xml}


class MemberSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.s=MirrorStore(self.root/'state.db')
        self.s.initialize({'wechat_talker':'room','feishu_chat_id':'chat','lark_profile':'demo','lark_app_id':'app','wechat_chat_name':'Source','feishu_chat_name':'Target','source_account':'one'})
        self.s.set_control(1,'running');self.s.update_recovery(1,phase='idle')
        self.s.save_setting('member_sync:1',{'enabled':True,'boundary':10})
        p=self.s.create_person('实名');self.s.link_wechat_identity(p['id'],'one','wx_target')
        self.fs=self.s.link_feishu_identity(p['id'],self.s.list_profiles()[0]['id'],'ou_target')
        self.source={'wx_target','wx_actor'};self.target={'ou_actor'};self.owner='ou_actor';self.bots={'cli_bot'}
        self.writes=[];self.lose_receipt=False;self.forbidden=False;self.partial=False;self.no_readback=False
    def put(self,row):
        self.s.upsert_message('room:'+str(row['local_id']),row,'system',binding_id=1,source_account='one')
    def cli(self,args,**kwargs):
        if 'members' in args:
            return {'data':{'chatroom_id':'room','total':len(self.source)+int(self.partial),'members':[{'username':x} for x in sorted(self.source)]}}
        self.assertEqual(args[args.index('--profile')+1],'demo')
        self.assertEqual(args[args.index('--chat-id')+1],'chat')
        self.assertEqual(args[args.index('--as')+1],'bot')
        if '+chat-members-list' in args:
            if self.forbidden:raise RuntimeError('permission denied')
            if self.no_readback and self.writes:raise TimeoutError('readback failed')
            return {'data':{'users':[{'member_id':x} for x in self.target],'bots':[{'app_id':x} for x in self.bots],'truncations':[]}}
        if 'chats' in args:return {'data':{'owner_id':self.owner}}
        if 'chat.members' in args:
            value=json.loads(args[args.index('--data')+1]);member=value['id_list'][0]
            op=args[args.index('chat.members')+1];self.writes.append((op,member))
            if op=='create':self.target.add(member)
            else:
                self.assertIn('--yes',args);self.target.discard(member)
            if self.lose_receipt:raise TimeoutError('accepted but response lost')
            return {'data':{}}
        raise AssertionError(args)
    def tick(self):
        with self.s.connect() as db:db.execute('UPDATE member_tasks SET retry_at=0')
        with patch('agents.member_sync.run_cli',side_effect=self.cli):process_member_tasks({'binding_id':1},self.s)
    def task(self):return self.s.list_member_tasks(1)[0]
    def test_only_system_events_use_affected_role_never_actor_or_plain_text(self):
        row=event(names=['wx_target','wx_other']);self.assertEqual(affected_members(row),[('wx_target','add'),('wx_other','add')])
        self.assertEqual(affected_members({**row,'local_type':1}),[])
        self.assertEqual(affected_members(event(wxid='room@chatroom')),[])
        self.assertEqual(affected_members({**row,'content':'bad XML'}),[])
    def test_add_remove_readback_and_duplicate_events(self):
        self.put(event());self.tick();self.tick()
        self.assertEqual(self.writes,[('create','ou_target')]);self.assertEqual(self.task()['state'],'applied')
        self.put(event(12,'remove'));self.source.remove('wx_target');self.tick();self.tick()
        self.assertEqual(self.writes,[('create','ou_target'),('delete','ou_target')]);self.assertEqual(self.task()['state'],'applied')
        self.assertIn('ou_actor',self.target);self.assertEqual(self.s.get_cursor(1),0)
    def test_newer_event_and_current_source_prevent_stale_removal(self):
        self.target.add('ou_target');self.put(event(11,'remove'));self.put(event(12,'add'));self.tick()
        self.assertEqual(self.writes,[])
        self.assertEqual([x['state'] for x in self.s.list_member_tasks(1)],['applied','obsolete'])
        self.put(event(13,'remove'));self.tick();self.assertEqual(self.task()['state'],'obsolete')
    def test_pause_disabled_and_baseline_do_not_replay_history(self):
        self.put(event(9));self.put(event(11));self.s.set_control(1,'paused');self.tick();self.assertEqual(self.writes,[])
        self.s.set_control(1,'running');self.s.save_setting('member_sync:1',{'enabled':False,'boundary':10});self.tick();self.assertEqual(self.writes,[])
        self.s.save_setting('member_sync:1',{'enabled':True,'boundary':10});self.tick()
        self.assertEqual(len(self.s.list_member_tasks(1)),1);self.assertEqual(self.writes,[('create','ou_target')])
    def test_unknown_and_ambiguous_profiles_do_not_guess(self):
        self.put(event(wxid='wx_unknown'));self.tick();self.assertEqual(self.task()['state'],'needs_review');self.assertEqual(self.writes,[])
        identity=self.s.resolve_sender('one','wx_target')
        self.s.link_feishu_identity(identity['person_id'],self.s.list_profiles()[0]['id'],'ou_other')
        self.put(event(12));self.tick();self.assertEqual(self.task()['state'],'needs_review');self.assertEqual(self.writes,[])
    def test_changed_identity_never_retargets_old_retry(self):
        self.put(event());self.forbidden=True;self.tick();self.assertEqual(self.task()['state'],'failed')
        wx=self.s.resolve_sender('one','wx_target');other=self.s.create_person('同名')
        self.s.reassign_wechat_identity(wx['id'],wx['person_id'],other['id'],'verified')
        self.s.link_feishu_identity(other['id'],self.s.list_profiles()[0]['id'],'ou_new')
        self.forbidden=False;self.tick();self.assertEqual(self.task()['state'],'needs_review');self.assertEqual(self.writes,[])
        with self.assertRaises(ValueError):retry_member_task(self.s,self.task()['id'])
    def test_owner_bot_and_incomplete_source_protected(self):
        self.target.add('ou_target');self.source.remove('wx_target');self.owner='ou_target';self.put(event(11,'remove'));self.tick()
        self.assertEqual(self.task()['state'],'needs_review');self.assertEqual(self.writes,[])
        self.owner='ou_actor';self.bots.add('ou_target');retry_member_task(self.s,self.task()['id']);self.tick();self.assertEqual(self.writes,[])
        self.bots.remove('ou_target');self.partial=True;retry_member_task(self.s,self.task()['id']);self.tick();self.assertEqual(self.writes,[])
    def test_bot_owned_group_omits_human_owner_field(self):
        self.target.add('ou_target');self.source.remove('wx_target');self.put(event(11,'remove'))
        original=self.cli
        def cli(args,**kwargs):
            if 'chats' in args:
                return {'data':{'chat_mode':'group','chat_status':'normal','bot_count':'1','user_count':'2'}}
            return original(args,**kwargs)
        with patch('agents.member_sync.run_cli',side_effect=cli):
            process_member_tasks({'binding_id':1},self.s)
            process_member_tasks({'binding_id':1},self.s)
        self.assertEqual(self.task()['state'],'applied')
        self.assertEqual(self.writes,[('delete','ou_target')])
        self.assertEqual(self.target,{'ou_actor'})
        self.assertEqual(self.bots,{'cli_bot'})

    def test_missing_owner_without_bot_owned_group_evidence_is_protected(self):
        self.target.add('ou_target');self.source.remove('wx_target');self.put(event(11,'remove'))
        original=self.cli
        cases=[{}, {'chat_mode':'p2p','chat_status':'normal','bot_count':'1'},
               {'chat_mode':'group','chat_status':'normal','bot_count':'0'},
               {'chat_mode':'group','chat_status':'normal','bot_count':'2'},
               {'chat_mode':'group','chat_status':'normal','bot_count':'1','owner_id':''},
               {'chat_mode':'group','chat_status':'normal','bot_count':'1','owner_id':None},
               {'chat_mode':'group','chat_status':'normal','bot_count':'1','owner_id_type':'open_id'}]
        for info in cases:
            with self.subTest(info=info):
                if self.s.list_member_tasks(1):retry_member_task(self.s,self.task()['id'])
                def cli(args,**kwargs):
                    return {'data':info} if 'chats' in args else original(args,**kwargs)
                with patch('agents.member_sync.run_cli',side_effect=cli):process_member_tasks({'binding_id':1},self.s)
                self.assertEqual(self.task()['state'],'needs_review')
                self.assertEqual(self.writes,[])

    def test_lost_receipt_recovers_via_readback_without_duplicate_write(self):
        self.put(event());self.lose_receipt=True;self.tick();self.assertEqual(self.task()['state'],'uncertain')
        self.lose_receipt=False;self.tick();self.assertEqual(self.task()['state'],'applied');self.assertEqual(len(self.writes),1)
    def test_readback_failure_and_permission_failure_are_not_success(self):
        self.put(event());self.no_readback=True;self.tick();self.assertEqual(self.task()['state'],'uncertain')
        self.no_readback=False;self.tick();self.assertEqual(self.task()['state'],'applied')
        self.put(event(12,'remove'));self.source.remove('wx_target');self.forbidden=True
        for i in range(3):self.tick()
        self.assertEqual(self.task()['state'],'needs_review');self.assertEqual(len(self.writes),1)
    def test_ui_enable_requires_confirmation_and_uses_latest_boundary(self):
        config=self.root/'config.json';config.write_text('{}');manager=Manager(self.s,config)
        self.s.save_setting('member_sync:1',{})
        with self.assertRaises(ValueError):manager.post('bindings',{'op':'member-sync','id':1,'enabled':True})
        with patch('agents.wechat_feishu_mirror.source_page',return_value={'through_local_id':123}) as page:
            result=manager.post('bindings',{'op':'member-sync','id':1,'enabled':True,'confirm':True})
        self.assertTrue(result['member_sync_enabled']);self.assertEqual(self.s.setting('member_sync:1')['boundary'],123)
        self.assertEqual(page.call_count,1)

    def test_two_profiles_and_accounts_never_share_open_ids(self):
        profile=self.s.upsert_profile('demo-two','app-two')
        other=self.s.create_person('实名')
        self.s.link_wechat_identity(other['id'],'two','wx_target')
        self.s.link_feishu_identity(other['id'],profile['id'],'ou_second')
        b=self.s.create_binding({'wechat_chat_name':'Source','wechat_talker':'room-two','feishu_chat_name':'Target','feishu_chat_id':'chat-two','lark_profile':'demo-two','lark_app_id':'app-two','source_account':'two'})
        self.s.set_control(b['id'],'running');self.s.update_recovery(b['id'],phase='idle')
        self.s.save_setting(f"member_sync:{b['id']}",{'enabled':True,'boundary':10})
        row={**event(),'talker':'room-two'};self.s.upsert_message('room-two:11',row,'system',binding_id=b['id'],source_account='two')
        other_target=set();other_writes=[]
        def cli(args,**kwargs):
            if 'members' in args and 'room-two' in args:
                return {'data':{'chatroom_id':'room-two','total':1,'members':[{'username':'wx_target'}]}}
            if 'demo-two' in args:
                self.assertEqual(args[args.index('--chat-id')+1],'chat-two')
                if '+chat-members-list' in args:return {'data':{'users':[{'member_id':x} for x in other_target],'bots':[]}}
                value=json.loads(args[args.index('--data')+1]);other_writes.extend(value['id_list']);other_target.update(value['id_list']);return {'data':{}}
            return self.cli(args,**kwargs)
        self.put(event())
        with patch('agents.member_sync.run_cli',side_effect=cli):
            process_member_tasks({'binding_id':1},self.s);process_member_tasks({'binding_id':b['id']},self.s)
        self.assertEqual(self.writes,[('create','ou_target')]);self.assertEqual(other_writes,['ou_second'])
        self.assertEqual(self.s.list_member_tasks(b['id'])[0]['state'],'applied')

    def test_rejoined_during_remote_check_is_not_removed(self):
        self.target.add('ou_target');self.source.remove('wx_target');self.put(event(11,'remove'))
        def cli(args,**kwargs):
            result=self.cli(args,**kwargs)
            if 'chats' in args:self.source.add('wx_target')
            return result
        with patch('agents.member_sync.run_cli',side_effect=cli):process_member_tasks({'binding_id':1},self.s)
        self.assertEqual(self.task()['state'],'obsolete');self.assertEqual(self.writes,[])

    def test_unmapped_task_can_retry_after_explicit_identity_link(self):
        self.source.add('wx_new');self.put(event(wxid='wx_new'));self.tick()
        self.assertEqual(self.task()['state'],'needs_review')
        person=self.s.create_person('后确认');self.s.link_wechat_identity(person['id'],'one','wx_new')
        self.s.link_feishu_identity(person['id'],self.s.list_profiles()[0]['id'],'ou_new')
        retry_member_task(self.s,self.task()['id']);self.tick()
        self.assertEqual(self.writes,[('create','ou_new')]);self.assertEqual(self.task()['state'],'applied')

    def test_profile_replacement_disables_tasks_without_cross_app_calls(self):
        self.put(event());self.s.upsert_profile('demo','different-app')
        self.tick();self.assertEqual(self.writes,[]);self.assertEqual(self.s.list_member_tasks(1),[])

    def test_real_mac_quotes_revoke_and_kickout_role(self):
        invite=event();invite['content']=invite['content'].replace('$username$邀请$names$加入了群聊','你邀请"$names$"加入了群聊  $revoke$')
        self.assertEqual(affected_members(invite),[('wx_target','add')])
        remove=event(operation='remove');remove['content']=remove['content'].replace('$username$将$names$移出了群聊','你将"$kickoutname$"移出了群聊').replace('name="names"','name="kickoutname"')
        self.assertEqual(affected_members(remove),[('wx_target','remove')])
        from integrations.wda_reader import map_row
        mapped=map_row('room',{'localId':11,'type':10000,'content':invite['content']},{})
        self.assertEqual(affected_members(mapped),[('wx_target','add')])

    def test_identity_unlink_waits_for_inflight_operation_and_blocks_future_task(self):
        import threading
        self.put(event());entered=threading.Event();release=threading.Event();unlinked=threading.Event()
        wx=self.s.resolve_sender('one','wx_target')
        def cli(args,**kwargs):
            if 'chat.members' in args:entered.set();release.wait(3)
            return self.cli(args,**kwargs)
        def unlink():
            self.s.unlink_identity('wechat',wx['id'],wx['person_id']);unlinked.set()
        with patch('agents.member_sync.run_cli',side_effect=cli):
            worker=threading.Thread(target=process_member_tasks,args=({'binding_id':1},self.s));worker.start()
            self.assertTrue(entered.wait(3));changer=threading.Thread(target=unlink);changer.start()
            self.assertFalse(unlinked.wait(.1));release.set();worker.join(3);changer.join(3)
        self.assertTrue(unlinked.is_set());self.put(event(12,'remove'));self.source.remove('wx_target');self.tick()
        self.assertEqual(self.writes,[('create','ou_target')]);self.assertEqual(self.task()['state'],'needs_review')

    def test_worker_maintenance_entry_reaches_members_with_names_disabled(self):
        from agents.group_names import group_name_loop
        import threading
        stop=threading.Event();self.put(event())
        def cli(args,**kwargs):
            result=self.cli(args,**kwargs)
            if 'chat.members' in args:stop.set()
            return result
        with patch('agents.manage.profile_list'),patch('agents.member_sync.run_cli',side_effect=cli):
            worker=threading.Thread(target=group_name_loop,args=({'binding_id':1},self.s,stop));worker.start();worker.join(4)
            stop.set();worker.join(2)
        self.assertEqual(self.writes,[('create','ou_target')]);self.assertFalse(worker.is_alive())

    def test_wda_members_without_completeness_proof_fail_closed(self):
        from integrations.wda_reader import cmd_members,WdaError
        from types import SimpleNamespace
        args=SimpleNamespace(talker='room',limit=10000,offset=0)
        with patch('integrations.wda_reader.api_get',return_value={'members':[{'username':'wx_target'}]}):
            with self.assertRaises(WdaError):cmd_members(args)
        with patch('integrations.wda_reader.api_get',return_value={'members':[{'username':'wx_target'}],'total':1,'hasMore':False,'username':'room'}):
            self.assertEqual(cmd_members(args)['data']['chatroom_id'],'room')

    def test_source_members_uses_configured_reader_override(self):
        from agents.member_sync import source_members
        with patch('agents.member_sync.run_cli',return_value={'data':{'chatroom_id':'room','total':1,'members':[{'username':'wx_target'}]}}) as read:
            self.assertEqual(source_members({'wechat_talker':'room','wechat_reader':'custom-account-reader'}),{'wx_target'})
        self.assertEqual(read.call_args.args[0][0],'custom-account-reader')

if __name__=='__main__':unittest.main()
