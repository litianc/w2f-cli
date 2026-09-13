"""Offline acceptance cases. All remote messages are captured by a fake sink.

Tests exercise the production pipeline (SQLite + per-part receipts + renderer),
not a separate replay implementation. No real chat or credentials are used.
"""
import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from agents import wechat_feishu_mirror as m
from agents.storage import MirrorStore


class History:
    def __init__(self, rows, page_size=17):
        self.rows=rows; self.page_size=page_size; self.calls=[]
    def __call__(self, config, cursor, high=None, after=None):
        high=max((r['local_id'] for r in self.rows),default=cursor) if high is None else high
        rows=sorted((r for r in self.rows if cursor<r['local_id']<=high),key=lambda r:(r['create_time'],r['local_id']))
        if after: rows=[r for r in rows if (r['create_time'],r['local_id'])>(after['time'],after['local_id'])]
        page=rows[:self.page_size];self.calls.append((cursor,high,after))
        return {'contract':'ordered_sync_v1','messages':page,'through_local_id':high,'has_more':len(rows)>len(page),
                'next':{'time':page[-1]['create_time'],'local_id':page[-1]['local_id']} if page else None}


class Sink:
    def __init__(self): self.sent=[];self.receipts={};self.fail_at=None;self.timeout_after=None
    def __call__(self, config, args):
        key=args[args.index('--idempotency-key')+1]
        if key in self.receipts: return self.receipts[key]
        if self.fail_at==len(self.sent):
            self.fail_at=None;raise m.CliError('network offline',uncertain=True)
        result={'data':{'message_id':f'om_{len(self.sent)}'}}
        self.sent.append((config['binding_id'],args.copy()));self.receipts[key]=result
        if self.timeout_after==len(self.sent):
            self.timeout_after=None;raise m.CliError('response lost',uncertain=True)
        return result


class OfflineAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=MirrorStore(self.root/'state.db')
        self.config={'binding_id':1,'source_account':'demo','wechat_talker':'room','wechat_chat_name':'源','feishu_chat_id':'chat','feishu_chat_name':'目标','lark_profile':'p','lark_app_id':'app','initial_cursor':10,'sender_map':{'wx':'已确认姓名'},'data_dir':str(self.root),'sqlite_file':str(self.root/'state.db')}
        self.store.initialize(self.config);self.store.set_control(1,'running')
        self.sink=Sink();self.mocks=[patch.object(m,'run_lark',self.sink),patch('builtins.print')]
        for p in self.mocks:p.start()
    def tearDown(self):
        for p in reversed(self.mocks):p.stop()
        self.tmp.cleanup()
    def rows(self,count=251,room='room'):
        return [{'local_id':i,'server_id':str(9000+i),'talker':room,'sender_wxid':'wx','sender':'昵称','kind_name':'text','text':f'  第{i}条\n😀👩‍💻\t**原文** https://example.test/?a=1&b=2  ','create_time':1000+i//2} for i in range(11,11+count)]
    def texts(self):return [a[a.index('--text')+1] for _,a in self.sink.sent if '--text' in a]

    def test_T41_offline_more_than_one_page_exact_order_and_format(self):
        rows=self.rows();source=History(rows)
        self.assertEqual(m.recover_once(self.config,self.store,source),251)
        self.assertGreater(len(source.calls),1)
        self.assertEqual(self.texts(),['已确认姓名：'+r['text'] for r in rows])
        self.assertEqual(self.store.get_cursor(),261)
        self.assertEqual(self.store.recovery(1)['remaining'],0)
        self.assertEqual(m.recover_once(self.config,self.store,source),0)
        self.assertEqual(len(self.sink.sent),251)

    def test_T42_offline_again_then_restart_never_overtakes_failed_message(self):
        rows=self.rows(80);source=History(rows);self.sink.fail_at=7
        with self.assertRaises(m.CliError):m.recover_once(self.config,self.store,source)
        self.assertEqual(self.store.get_cursor(),10)
        self.assertEqual(self.texts(),['已确认姓名：'+r['text'] for r in rows[:7]])
        # Offline newer arrivals wait behind the durable snapshot.
        source.rows+=self.rows(83)[80:]
        restarted=MirrorStore(self.store.path);restarted.initialize(self.config)
        m.recover_once(self.config,restarted,source)
        m.recover_once(self.config,restarted,source)
        self.assertEqual(self.texts(),['已确认姓名：'+r['text'] for r in source.rows])
        self.assertEqual(len(self.sink.sent),83)

    def test_T43_lost_response_same_uuid_and_long_offline_no_resend(self):
        source=History(self.rows(2));self.sink.timeout_after=1
        with self.assertRaises(m.CliError):m.recover_once(self.config,self.store,source)
        # The sink accepted the first message; retry sees the same key.
        m.recover_once(self.config,self.store,source)
        self.assertEqual(len(self.sink.sent),2)
        with self.store.connect() as db:
            db.execute('UPDATE deliveries SET attempted_at=?',(time.time()-7200,))
        m.recover_once(self.config,self.store,source)
        self.assertEqual(len(self.sink.sent),2)

    def test_T43b_expired_unknown_receipt_blocks_following_messages(self):
        source=History(self.rows(2));self.sink.timeout_after=1
        with self.assertRaises(m.CliError):m.recover_once(self.config,self.store,source)
        with self.store.connect() as db:db.execute('UPDATE deliveries SET attempted_at=?',(time.time()-7200,))
        with self.assertRaises(m.DeliveryUncertain):m.recover_once(self.config,self.store,source)
        self.assertEqual(len(self.sink.sent),1);self.assertEqual(self.store.get_cursor(),10)

    def test_T44_pause_during_backfill_and_resume_preserves_order(self):
        self.config['_guard']=True
        source=History(self.rows(6));sink=self.sink
        def send(config,args):
            result=sink(config,args)
            if len(sink.sent)==2:self.store.set_control(1,'paused')
            return result
        with patch.object(m,'run_lark',send):
            with self.assertRaises(m.ControlChanged):m.recover_once(self.config,self.store,source)
        self.assertEqual(len(sink.sent),2)
        self.store.set_control(1,'running');m.recover_once(self.config,self.store,source)
        self.assertEqual(self.texts(),['已确认姓名：'+r['text'] for r in source.rows])

    def test_T45_crash_between_media_parts_and_after_cleanup(self):
        rows=self.rows(2);rows[0]['kind_name']='image'
        image=self.root/'photo.png';image.write_bytes(b'fixture')
        self.sink.fail_at=1
        with patch.object(m,'stage_media',return_value=str(image)):
            with self.assertRaises(m.CliError):m.recover_once(self.config,self.store,History(rows))
            m.recover_once(self.config,self.store,History(rows))
        self.assertEqual(sum('--image' in args for _,args in self.sink.sent),1)
        self.assertEqual(self.texts(),['已确认姓名：','已确认姓名：'+rows[1]['text']])
        self.assertFalse(image.exists())
        # Crash after per-part receipt but before aggregate message state.
        with self.store.connect() as db:
            db.execute("UPDATE messages SET state='processing' WHERE local_id=11")
        with patch.object(m,'stage_media',side_effect=AssertionError('must reuse receipt')):
            m.send_row(self.config,rows[0],{},self.store)
        self.assertEqual(len(self.sink.sent),3)

    def test_T46_missing_source_anchor_cannot_advance_cursor(self):
        def source(*args):raise RuntimeError('source_gap')
        with self.assertRaisesRegex(RuntimeError,'source_gap'):m.recover_once(self.config,self.store,source)
        self.assertEqual(self.store.get_cursor(),10);self.assertFalse(self.sink.sent)

    def test_T47_scan_crash_durable_page_and_same_timestamp(self):
        rows=self.rows(30)
        for r in rows:r['create_time']=1000
        source=History(rows,7);calls=0
        def crash(*args):
            nonlocal calls
            calls+=1
            if calls==3:raise RuntimeError('reader offline')
            return source(*args)
        with self.assertRaises(RuntimeError):m.recover_once(self.config,self.store,crash)
        self.assertFalse(self.sink.sent)
        self.assertEqual(len(self.store.pending_messages(1)),14)
        m.recover_once(self.config,MirrorStore(self.store.path),source)
        self.assertEqual(self.texts(),['已确认姓名：'+r['text'] for r in rows])

    def test_T48_ten_bindings_independent_backfill(self):
        configs=[self.config]
        for i in range(2,11):
            self.store.upsert_profile(f'p{i}',f'app{i}')
            b=self.store.create_binding({'source_account':'demo','wechat_chat_name':f'源{i}','wechat_talker':f'room{i}','feishu_chat_name':f'目标{i}','feishu_chat_id':f'chat{i}','lark_profile':f'p{i}'})
            self.store.initialize_cursor(i,10)
            configs.append({**self.config,**b,'binding_id':i})
        def run(c):return m.recover_once(c,MirrorStore(self.store.path),History(self.rows(9,c['wechat_talker']),3))
        with ThreadPoolExecutor(max_workers=10) as pool:results=list(pool.map(run,configs))
        self.assertEqual(results,[9]*10)
        self.assertEqual(len(self.sink.sent),90)
        for c in configs:
            expected=['已确认姓名：'+r['text'] for r in self.rows(9,c['wechat_talker'])]
            actual=[a[a.index('--text')+1] for bid,a in self.sink.sent if bid==c['binding_id']]
            self.assertEqual(actual,expected)
            self.assertEqual(self.store.get_cursor(c['binding_id']),19)

    def test_T49_first_start_skips_history_subsequent_start_catches_up(self):
        with self.store.connect() as db:db.execute('UPDATE runtime_state SET cursor=0,initialized=0')
        source=History(self.rows(3));m.recover_once(self.config,self.store,source)
        self.assertFalse(self.sink.sent);self.assertEqual(self.store.get_cursor(),13)
        source.rows=self.rows(5);m.recover_once(self.config,self.store,source)
        self.assertEqual(self.texts(),['已确认姓名：'+r['text'] for r in source.rows[3:]])

    def test_T50_json_mapping_never_overwrites_confirmed_name_or_cursor(self):
        person=self.store.list_people()[0];self.store.rename_person(person['id'],'更正姓名')
        self.store.set_cursor(20);self.store.initialize(self.config)
        self.assertEqual(self.store.get_cursor(),20)
        self.assertEqual(self.store.resolve_sender('demo','wx')['confirmed_name'],'更正姓名')

    def test_T53_split_forward_keeps_render_snapshot_after_name_change(self):
        rows=self.rows(2)
        rows[0].update(kind_name='forward_chat',forward_items=[{'sender_id':'wx','sender':'昵称','body':'😀长记录'*4000}])
        self.sink.fail_at=1
        with self.assertRaises(m.CliError):m.recover_once(self.config,self.store,History(rows))
        self.store.rename_person(self.store.list_people()[0]['id'],'新实名')
        m.recover_once(self.config,self.store,History(rows))
        parts=self.texts()[:-1]
        self.assertEqual(len(parts),5)
        self.assertTrue(parts[0].startswith('已确认姓名转发聊天记录（第 1/5 段）'))
        self.assertTrue(parts[-1].startswith('已确认姓名转发聊天记录（第 5/5 段）'))
        self.assertEqual(''.join(p.split('\n',1)[1] for p in parts),'已确认姓名：'+'😀长记录'*4000)
        self.assertEqual(self.texts()[-1],'新实名：'+rows[1]['text'])

    def test_T54_placeholder_keeps_position_manual_repair_replies_to_it(self):
        rows=self.rows(3);rows[1]['kind_name']='image'
        with patch.object(m,'stage_media',side_effect=m.CliError('decode_failed: fixture')):
            m.recover_once(self.config,self.store,History(rows))
        self.assertEqual(self.texts(),['已确认姓名：'+rows[0]['text'],'已确认姓名：[图片资源不可用，待人工排查]','已确认姓名：'+rows[2]['text']])
        self.assertEqual(self.store.get_cursor(),13)
        with self.store.connect() as db:
            db.execute("UPDATE media_jobs SET state='pending'")
        self.store.rename_person(self.store.list_people()[0]['id'],'新实名')
        image=self.root/'repair.png';image.write_bytes(b'fixture')
        with patch.object(m,'stage_media',return_value=str(image)):
            m.retry_media_jobs(self.config,self.store)
        args=self.sink.sent[-1][1]
        self.assertEqual(args[0],'+messages-reply')
        self.assertEqual(args[args.index('--message-id')+1],'om_1')
        self.assertIn('--image',args);self.assertNotIn('--text',args)
        self.assertEqual(self.store.message(m.message_key(rows[1]),1)['sender_name'],'已确认姓名')
        m.retry_media_jobs(self.config,self.store)
        self.assertEqual(len(self.sink.sent),4)

    def test_T55_online_and_offline_same_mixed_message_rendering(self):
        rows=self.rows(9)
        for row,kind in zip(rows,('text','link','image','video','file','voice','sticker','quote','forward_chat')):
            row['kind_name']=kind
        rows[1]['text']='<msg><appmsg><title>链接😀</title><url>https://example.test/item</url></appmsg></msg>'
        rows[7]['text']='<msg><appmsg><title>引用回复😀</title><refermsg><type>1</type><svrid>9011</svrid><chatusr>wx</chatusr><displayname>昵称</displayname><content>原文</content></refermsg></appmsg></msg>'
        rows[8]['forward_items']=[{'sender_id':'wx','sender':'昵称','body':' 转发😀\n正文 '},{'sender_id':'wx','sender':'昵称','body':'[图片]'}]
        def stage(row,*args):
            suffix={'image':'.png','video':'.mp4','file':'.pdf','voice':'.ogg','sticker':'.gif'}[row['kind_name']]
            path=self.root/(str(row['local_id'])+suffix);path.write_bytes(b'fixture');return str(path)
        with patch.object(m,'stage_media',side_effect=stage),patch.object(m,'stage_sticker',side_effect=stage),patch.object(m,'video_cover',return_value=self.root/'cover.jpg'):
            m.recover_once(self.config,self.store,History(rows,2))
            offline=[a for _,a in self.sink.sent]
            # A separate fresh database receives one source arrival per poll.
            online=MirrorStore(self.root/'online.db');online.initialize(self.config)
            live_sink=Sink()
            with patch.object(m,'run_lark',live_sink):
                for n in range(1,len(rows)+1):m.recover_once(self.config,online,History(rows[:n],2))
        self.assertEqual(offline,[a for _,a in live_sink.sent])
        flags=[next(k for k in ('--text','--image','--video','--file','--audio') if k in a) for a in offline]
        self.assertEqual(flags,['--text','--text','--text','--image','--text','--video','--text','--file','--text','--audio','--text','--image','--text','--text'])
        self.assertEqual(offline[-2][0],'+messages-reply')
        self.assertEqual(offline[-1][offline[-1].index('--text')+1],'已确认姓名转发聊天记录（第 1/1 段）\n已确认姓名： 转发😀\n正文 \n已确认姓名：[图片]')
