import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agents.storage import MirrorStore


class GroupNamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=MirrorStore(Path(self.tmp.name)/'state.db')
        self.s.initialize({'wechat_chat_name':'A','wechat_talker':'room','feishu_chat_name':'Custom target','feishu_chat_id':'chat','lark_profile':'test','lark_app_id':'app'})
        self.s.save_setting('name_sync_enabled:1',True)
        self.s.set_control(1,'running');self.source='A';self.target='Custom target';self.writes=[];self.failure=False
    def cli(self,args,**kwargs):
        if 'resolve-chat' in args:return {'data':{'candidates':[{'username':'room','nick_name':self.source,'display_name':'local alias'}]}}
        self.assertIn('--as',args);self.assertEqual(args[args.index('--as')+1],'bot')
        self.assertEqual(args[args.index('--profile')+1],'test');self.assertEqual(args[args.index('--chat-id')+1],'chat')
        if '+chat-update' in args:
            self.writes.append(args[args.index('--name')+1])
            if self.failure:raise RuntimeError('permission_denied')
            self.target=self.writes[-1]
        return {'data':{'name':self.target}}
    def tick(self):
        from agents.group_names import sync_group_name
        with patch('agents.group_names.run_cli',side_effect=self.cli):return sync_group_name({'binding_id':1},self.s)
    def test_baseline_change_idempotency_and_manual_refresh(self):
        self.tick();self.assertEqual(self.writes,[])
        self.source='B';self.s.rename_binding(1,wechat_chat_name='B')
        self.tick();self.assertEqual(self.writes,['B']);self.assertEqual(self.target,'B')
        self.tick();self.assertEqual(self.writes,['B'])
        state=self.s.get_binding(1)['name_sync'];self.assertEqual(state['applied'],'B');self.assertFalse(state.get('error'))
        self.assertEqual(self.s.get_cursor(1),0)
    def test_failure_tracks_latest_name_and_pause_blocks_writes(self):
        self.tick();self.source='B';self.failure=True;self.tick()
        self.assertTrue(self.s.get_binding(1)['name_sync']['error'])
        self.s.set_control(1,'paused');self.source='C';self.failure=False;self.tick();self.assertEqual(self.writes,['B'])
        self.s.save_setting('name_sync_enabled:1',True)
        self.s.set_control(1,'running');self.tick();self.assertEqual(self.target,'C')
        self.assertEqual(self.writes,['B','C'])
    def test_uncertain_success_readback_avoids_second_write(self):
        self.tick();self.source='B';self.target='B';self.tick();self.assertEqual(self.writes,[])
        self.assertEqual(self.s.get_binding(1)['name_sync']['applied'],'B')
    def test_invalid_source_keeps_remote_and_state_pending(self):
        self.tick();self.source='';self.tick();self.assertEqual(self.writes,[])
        self.assertTrue(self.s.get_binding(1)['name_sync']['error'])
        self.s.archive_binding(1);self.source='B';self.tick();self.assertEqual(self.writes,[])

    def test_default_disabled_and_explicit_enable_required(self):
        from agents.manage import Manager
        self.s.save_setting('name_sync_enabled:1',False)
        self.tick();self.source='B';self.tick();self.assertEqual(self.writes,[])
        self.assertEqual(self.s.setting('name_sync:1',{}),{})
        config=Path(self.tmp.name)/'config.json';config.write_text('{}')
        manager=Manager(self.s,config)
        with self.assertRaises(ValueError):manager.post('bindings',{'op':'name-sync','id':1,'enabled':True})
        manager.post('bindings',{'op':'name-sync','id':1,'enabled':True,'confirm':True})
        self.tick();self.assertEqual(self.writes,[])  # Enabling takes a fresh baseline.
        self.source='C';self.tick();self.assertEqual(self.writes,['C'])

    def test_lost_write_receipt_and_readback_mismatch_retry_safely(self):
        self.tick();self.source='B';original=self.cli
        def timeout_after_write(args,**kwargs):
            value=original(args,**kwargs)
            if '+chat-update' in args:raise TimeoutError('lost receipt')
            return value
        from agents.group_names import sync_group_name
        with patch('agents.group_names.run_cli',side_effect=timeout_after_write):
            sync_group_name({'binding_id':1},self.s)
        self.assertEqual(self.s.get_binding(1)['name_sync']['status'],'failed')
        self.tick();self.assertEqual(self.writes,['B'])
        self.assertEqual(self.s.get_binding(1)['name_sync']['status'],'synced')
        self.source='C'
        def wrong_readback(args,**kwargs):
            value=original(args,**kwargs)
            if '+chat-update' in args:self.target='wrong'
            return value
        with patch('agents.group_names.run_cli',side_effect=wrong_readback):
            sync_group_name({'binding_id':1},self.s)
        self.assertEqual(self.s.get_binding(1)['name_sync']['applied'],'B')
        self.tick();self.assertEqual(self.target,'C')

    def test_pause_waits_for_inflight_maintenance_then_blocks_next_write(self):
        import threading
        self.tick();self.source='B';entered=threading.Event();release=threading.Event();paused=threading.Event()
        original=self.cli
        def slow(args,**kwargs):
            if '+chat-update' in args:
                entered.set();release.wait(3)
            return original(args,**kwargs)
        from agents.group_names import sync_group_name
        def pause():self.s.set_control(1,'paused');paused.set()
        with patch('agents.group_names.run_cli',side_effect=slow):
            worker=threading.Thread(target=sync_group_name,args=({'binding_id':1},self.s));worker.start()
            self.assertTrue(entered.wait(3));stopper=threading.Thread(target=pause);stopper.start()
            self.assertFalse(paused.wait(.1));release.set();worker.join(3);stopper.join(3)
        self.assertTrue(paused.is_set());self.source='C';self.tick();self.assertEqual(self.writes,['B'])

if __name__=='__main__':unittest.main()
