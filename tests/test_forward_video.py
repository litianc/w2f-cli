"""Embedded videos use exact resources and independent durable receipts."""
import hashlib
import html
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from integrations.forward_records import parse_record
from tests.test_forward_links import forward_xml
def _symlink_privilege():
    try:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'probe-target'; target.write_bytes(b'x')
            link = Path(directory) / 'probe-link'
            link.symlink_to(target)
            return True
    except (OSError, NotImplementedError):
        return False


SYMLINK_OK = _symlink_privilege()
from tests.test_recovery import History, Sink
from contextlib import closing

VIDEO = b'\x00\x00\x00\x18ftypmp42fixture-video'
DIGEST = hashlib.md5(VIDEO).hexdigest()

def video_item():
    return ('<dataitem datatype="4" dataid="fixture"><sourcename>视频发言人</sourcename>'
            '<fullmd5>'+DIGEST+'</fullmd5><datasize>'+str(len(VIDEO))+'</datasize>'
            '<datafmt>.mp4</datafmt><duration>38</duration>'
            '<cdndataurl>private-ticket</cdndataurl><cdndatakey>private-key</cdndatakey></dataitem>')

def row(raw=None):
    return {'talker':'room','local_id':11,'server_id':'911','create_time':1011,
            'sender_wxid':'outer','sender':'外层','kind_name':'forward_chat',
            'text':forward_xml(raw if raw is not None else video_item())}

class ForwardVideoTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=MirrorStore(self.root/'mirror.db')
        self.config={'binding_id':1,'source_account':'demo','wechat_talker':'room','feishu_chat_id':'chat',
                     'lark_profile':'fixture','lark_app_id':'app','initial_cursor':10,
                     'sender_map':{'outer':'转发人'},'media_dir':str(self.root/'stage')}
        self.store.initialize(self.config);self.store.set_control(1,'running')
        self.sink=Sink()
        for mock in (patch.object(mirror,'run_lark',self.sink),patch('builtins.print')):
            mock.start();self.addCleanup(mock.stop)
        self.video=self.root/'local.mp4';self.video.write_bytes(VIDEO)
        def resource(cmd,**kwargs):
            self.assertIn('forward-resource',cmd)
            data={'contract':'forward_resource_v1','talker':'room','local_id':11,'server_id':911,
                  'create_time':1011,'item_path':cmd[cmd.index('--item-path')+1],
                  'media':{'md5':DIGEST,'size':len(VIDEO),'extension':'.mp4','duration':38},
                  'resources':[{'local_paths':[str(self.video)]}]}
            return SimpleNamespace(returncode=0,stdout=json.dumps({'data':data}),stderr='')
        mock=patch.object(mirror.subprocess,'run',side_effect=resource);mock.start();self.addCleanup(mock.stop)
        def cover(path):
            target=path.with_suffix('.cover.jpg');target.write_bytes(b'cover');return target
        mock=patch.object(mirror,'video_cover',side_effect=cover);mock.start();self.addCleanup(mock.stop)
    def jobs(self):
        with self.store.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM media_jobs ORDER BY id')]
    def send(self,value=None):
        mirror.recover_once(self.config,self.store,History([value or row()]))
    def test_parser_retains_only_safe_video_descriptor(self):
        parsed=parse_record(row()['text'])[0]
        self.assertEqual(parsed['media'],{'md5':DIGEST,'size':len(VIDEO),'extension':'.mp4','duration':38})
        self.assertNotIn('private-',json.dumps(parsed))
    def test_two_identical_videos_have_separate_jobs_paths_and_receipts(self):
        self.send(row(video_item()*2))
        uploaded=[args for _,args in self.sink.sent if '--video' in args]
        self.assertEqual(len(uploaded),2)
        self.assertNotEqual(uploaded[0][uploaded[0].index('--video')+1],uploaded[1][uploaded[1].index('--video')+1])
        self.assertEqual([j['state'] for j in self.jobs()],['uploaded','uploaded'])
        self.assertEqual(len({j['source_ref'] for j in self.jobs()}),2)
        self.assertTrue(all('+messages-reply' in args for args in uploaded))
        self.assertFalse(list((self.root/'stage').glob('*')))
        self.assertEqual(self.store.get_cursor(),11)
        self.send(row(video_item()*2));self.assertEqual(len([a for _,a in self.sink.sent if '--video' in a]),2)
    def test_missing_then_manual_retry_uploads_without_resending_records(self):
        self.video.unlink();self.send()
        self.assertEqual(self.store.message_state(mirror.message_key(row())),'placeholder')
        self.assertEqual(len(self.store.list_media_failures()),1)
        before=len(self.sink.sent);self.video.write_bytes(VIDEO)
        with self.store.connect() as db:db.execute("UPDATE media_jobs SET state='pending',updated_at=0")
        mirror.retry_media_jobs(self.config,self.store)
        added=[args for _,args in self.sink.sent[before:]]
        self.assertEqual(sum('--video' in args for args in added),1)
        self.assertFalse(any('转发聊天记录（' in ' '.join(args) for args in added))
        self.assertEqual(self.jobs()[0]['state'],'uploaded')
        self.assertEqual(self.store.message_state(mirror.message_key(row())),'sent')
    def test_lost_video_receipt_restart_does_not_duplicate(self):
        self.sink.timeout_after=3
        with self.assertRaises(mirror.CliError):self.send()
        self.assertEqual(self.store.get_cursor(),10)
        self.send()
        self.assertEqual(sum('--video' in args for _,args in self.sink.sent),1)
        self.assertEqual(self.jobs()[0]['state'],'uploaded')
        self.assertEqual(self.store.get_cursor(),11)
    def test_wrong_content_never_uploaded(self):
        self.video.write_bytes(b'x'*len(VIDEO));self.send()
        self.assertFalse(any('--video' in args or '--file' in args for _,args in self.sink.sent))
        self.assertEqual(self.jobs()[0]['state'],'placeholder')
    def test_retry_one_child_preserves_other_failed_job(self):
        self.video.unlink();self.send(row(video_item()*2))
        second=self.jobs()[1]
        self.video.write_bytes(VIDEO)
        with self.store.connect() as db:db.execute("UPDATE media_jobs SET state='pending',updated_at=0 WHERE id=?",(second['id'],))
        mirror.retry_media_jobs(self.config,self.store)
        self.assertEqual([j['state'] for j in self.jobs()],['placeholder','uploaded'])
        self.assertEqual(self.store.message_state(mirror.message_key(row())),'placeholder')

    def test_nested_video_and_empty_metadata_remain_separate(self):
        nested='<datalist>'+video_item()+'</datalist>'
        content='<dataitem datatype="17"><recorditem>'+html.escape(nested)+'</recorditem></dataitem>'
        content+='<dataitem datatype="4"><sourcename>另一位</sourcename></dataitem>'
        self.send(row(content))
        self.assertEqual([j['source_ref'] for j in self.jobs()],['forward-video:0.0','forward-video:1'])
        self.assertEqual([j['state'] for j in self.jobs()],['uploaded','placeholder'])

    def test_expired_uncertain_receipt_blocks_without_fallback_or_duplicate(self):
        self.sink.timeout_after=3
        with self.assertRaises(mirror.CliError):self.send()
        with self.store.connect() as db:db.execute("UPDATE deliveries SET attempted_at=1 WHERE state='uncertain'")
        sent=len(self.sink.sent)
        with self.assertRaises(mirror.DeliveryUncertain):self.send()
        self.assertEqual(len(self.sink.sent),sent)
        self.assertEqual(self.jobs()[0]['state'],'manual')
        self.assertEqual(self.store.get_cursor(),10)

    def test_sent_receipt_recovers_after_cleanup_without_source(self):
        mark=self.store.mark_message
        def crash(key,state,**kw):
            if state=='sent':raise RuntimeError('crash after upload')
            return mark(key,state,**kw)
        with patch.object(self.store,'mark_message',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'crash'):self.send()
        self.video.unlink();sent=len(self.sink.sent)
        self.send()
        self.assertEqual(len(self.sink.sent),sent)
        self.assertEqual(self.store.get_cursor(),11)

    def test_explicit_historical_repair_keeps_original_parts_and_receipts(self):
        from agents.forward_media import queue_video_repair
        with patch('agents.forward_media.video_plan',return_value=[]):self.send()
        message=self.store.message(mirror.message_key(row()),1)
        plan=self.store.setting(f'render:{message["id"]}');plan.pop('videos')
        self.store.save_setting(f'render:{message["id"]}',plan)
        with self.store.connect() as db:
            before=[dict(r) for r in db.execute('SELECT * FROM deliveries')]
        self.assertTrue(queue_video_repair(mirror,self.store,self.config,message['id'])['queued'])
        mirror.retry_media_jobs(self.config,self.store)
        with self.store.connect() as db:
            after=[dict(r) for r in db.execute("SELECT * FROM deliveries WHERE part LIKE 'text-%'")]
        self.assertEqual(before,after)
        self.assertEqual(self.store.setting(f'render:{message["id"]}')['parts'],plan['parts'])
        self.assertEqual(self.jobs()[0]['state'],'uploaded')
        sent=len(self.sink.sent)
        queue_video_repair(mirror,self.store,self.config,message['id'])
        mirror.retry_media_jobs(self.config,self.store)
        self.assertEqual(len(self.sink.sent),sent)

    def test_reader_wrong_child_contract_never_uploads(self):
        data={'contract':'forward_resource_v1','talker':'other','local_id':11,'server_id':911,
              'create_time':1011,'item_path':'0','resources':[{'local_paths':[str(self.video)]}]}
        with patch.object(mirror.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'data':data}))):
            self.send()
        self.assertFalse(any('--video' in a or '--file' in a for _,a in self.sink.sent))
        self.assertEqual(self.jobs()[0]['state'],'placeholder')

    def test_identical_video_labels_in_later_part_reply_to_that_part(self):
        long_text='<dataitem datatype="1"><sourcename>甲</sourcename><datadesc>'+'字'*3980+'</datadesc></dataitem>'
        self.send(row(video_item()+long_text+video_item()))
        message=self.store.message(mirror.message_key(row()),1)
        plan=self.store.setting(f'render:{message["id"]}')
        first,last=plan['videos']
        self.assertGreater(last['text_part'],first['text_part'])
        uploads=[args for _,args in self.sink.sent if '--video' in args]
        for upload,video in zip(uploads,plan['videos']):
            receipt=self.store.delivery(message['id'],f'text-{video["text_part"]}')
            self.assertEqual(upload[upload.index('--message-id')+1],receipt['feishu_message_id'])

    def test_manager_failure_response_hides_resource_locations_and_keys(self):
        from agents.manage import Manager
        self.video.unlink();self.send()
        public=Manager(self.store,self.root/'config.json').get('media-failures')
        self.assertEqual(len(public),1)
        for key in ('source_ref','staging_path','private-ticket','private-key'):
            self.assertNotIn(key,json.dumps(public))

    def test_lost_fallback_receipt_cannot_be_skipped_on_recovery(self):
        self.video.unlink();self.sink.timeout_after=2
        with self.assertRaises(mirror.CliError):self.send()
        with self.store.connect() as db:db.execute("UPDATE deliveries SET attempted_at=1 WHERE state='uncertain'")
        before=len(self.sink.sent)
        with self.assertRaises(mirror.DeliveryUncertain):self.send()
        self.assertEqual(len(self.sink.sent),before)
        self.assertEqual(self.store.get_cursor(),10)

    def test_explicit_repair_requeues_existing_failure(self):
        from agents.forward_media import queue_video_repair
        self.video.unlink();self.send();self.video.write_bytes(VIDEO)
        message=self.store.message(mirror.message_key(row()),1)
        queue_video_repair(mirror,self.store,self.config,message['id'])
        self.assertEqual(self.jobs()[0]['state'],'pending')
        mirror.retry_media_jobs(self.config,self.store)
        self.assertEqual(self.jobs()[0]['state'],'uploaded')

    @unittest.skipIf(not SYMLINK_OK, 'Windows 需管理员/开发者模式才能创建符号链接')
    def test_cover_symlink_never_uploaded(self):
        from agents.forward_media import stage_video,video_plan
        records=mirror.forward_record_texts(row(),self.config,self.store)
        video=video_plan(row(),records,[0])[0]
        path=Path(stage_video(mirror,self.config,row(),video))
        secret=self.root/'private.txt';secret.write_text('secret')
        path.with_suffix('.cover.jpg').symlink_to(secret)
        self.send()
        self.assertFalse(any('--video-cover' in a for _,a in self.sink.sent))
        self.assertEqual(self.jobs()[0]['state'],'placeholder')

    def test_confirmed_name_receipt_resumes_manual_job_video(self):
        self.sink.timeout_after=2
        with self.assertRaises(mirror.CliError):self.send()
        message=self.store.message(mirror.message_key(row()),1)
        with self.store.connect() as db:
            db.execute("UPDATE media_jobs SET state='manual'")
            db.execute("UPDATE deliveries SET attempted_at=1 WHERE state='uncertain'")
        with self.assertRaises(mirror.DeliveryUncertain):self.send()
        name=self.store.delivery(message['id'],'forward-video-0-name')
        remote=self.sink.receipts[name['idempotency_key']]['data']['message_id']
        self.store.set_delivery(message['id'],'forward-video-0-name','sent',remote)
        self.send()
        self.assertEqual(sum('--video' in args for _,args in self.sink.sent),1)
        self.assertEqual(self.jobs()[0]['state'],'uploaded')
        self.assertEqual(self.store.get_cursor(),11)

class ForwardReaderTest(unittest.TestCase):
    @unittest.skipIf(not SYMLINK_OK, 'Windows 需管理员/开发者模式才能创建符号链接')
    def test_reader_checks_outer_identity_and_hashes_exact_child_resource(self):
        from integrations.reader_forward import read_forward
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'messages.db';video=root/'video.mp4';video.write_bytes(VIDEO)
            with closing(sqlite3.connect(source, isolation_level=None)) as db:
                db.execute('CREATE TABLE Msg_room(local_id,server_id,create_time,text)')
                db.execute('INSERT INTO Msg_room VALUES(11,911,1011,?)',(row()['text'],))
            def connect(path):
                db=sqlite3.connect(path);db.row_factory=sqlite3.Row;return db
            database=SimpleNamespace(message_dbs=[source],resource_roots=[root],connect=connect)
            reader=SimpleNamespace(resolve_username=lambda db,t:t,message_table=lambda t:'Msg_'+t,
                 decode_message_content=lambda row:row['text'],visible_message_content=lambda s,*args:s,
                 message_projection=lambda *args:'*',ReaderError=lambda msg,code:RuntimeError(code),
                 hardlink_resources=lambda *args:[{'local_paths':[str(video)]}])
            args=SimpleNamespace(talker='room',local_id=11,server_id=911,create_time=1011,item_path='0')
            self.assertEqual(read_forward(reader,database,args)['resources'],[{'local_paths':[str(video.resolve())]}])
            args.server_id=912
            with self.assertRaisesRegex(RuntimeError,'identity_mismatch'):read_forward(reader,database,args)
            args.server_id=911;args.item_path='1'
            with self.assertRaisesRegex(RuntimeError,'item_missing'):read_forward(reader,database,args)
            args.item_path='0';video.write_bytes(b'x'*len(VIDEO))
            self.assertEqual(read_forward(reader,database,args)['resources'],[])
            # An opened forward can exist only in Rec, not the hardlink index.
            reader.hardlink_resources=lambda *args:[]
            cache=root/'attach'/hashlib.md5(b'room').hexdigest()/'2026-09'/'Rec'/'record'/'V'
            cache.mkdir(parents=True);cached=cache/'1.mp4';cached.write_bytes(VIDEO)
            self.assertEqual(read_forward(reader,database,args)['resources'],[{'local_paths':[str(cached.resolve())]}])
            cached.unlink();cached.symlink_to(video)
            video.write_bytes(VIDEO)
            self.assertEqual(read_forward(reader,database,args)['resources'],[])
