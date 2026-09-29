"""Merged-forward image acquisition and production delivery regression tests."""
import hashlib
import html
import json
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from integrations.forward_records import parse_record
from tests.test_forward_links import forward_xml
from tests.test_recovery import History,Sink

buffer=BytesIO();Image.new('RGB',(80,60),'blue').save(buffer,format='PNG')
IMAGE=buffer.getvalue();DIGEST=hashlib.md5(IMAGE).hexdigest()
MEDIA={'md5':DIGEST,'size':len(IMAGE),'extension':'.png'}

def image_item():
    return ('<dataitem datatype="2"><sourcename>图片发言人</sourcename><fullmd5>'+DIGEST+
            '</fullmd5><datasize>'+str(len(IMAGE))+'</datasize><datafmt>.png</datafmt>'
            '<cdndataurl>private-ticket</cdndataurl><cdndatakey>private-key</cdndatakey></dataitem>')

def row(content=None):
    return {'talker':'room','local_id':11,'server_id':'911','create_time':1011,'sender_wxid':'outer',
            'sender':'外层','kind_name':'forward_chat','text':forward_xml(content or image_item())}

class ForwardImageTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=MirrorStore(self.root/'mirror.db')
        self.config={'binding_id':1,'source_account':'demo','wechat_talker':'room','feishu_chat_id':'chat',
                     'lark_profile':'fixture','lark_app_id':'app','initial_cursor':10,'sender_map':{'outer':'转发人'},
                     'media_dir':str(self.root/'stage')}
        self.store.initialize(self.config);self.store.set_control(1,'running');self.sink=Sink()
        self.image=self.root/'source.dat';self.image.write_bytes(IMAGE)
        self.media=dict(MEDIA);self.resources=[{'local_paths':[str(self.image)]}]
        def resource(cmd,**kwargs):
            self.assertIn('forward-resource',cmd)
            data={'contract':'forward_resource_v1','talker':'room','local_id':11,'server_id':911,
                  'create_time':1011,'item_path':cmd[cmd.index('--item-path')+1],'media':self.media,
                  'resources':self.resources}
            return SimpleNamespace(returncode=0,stdout=json.dumps({'data':data}),stderr='')
        for mock in (patch.object(mirror,'run_lark',self.sink),patch.object(mirror.subprocess,'run',side_effect=resource),patch('builtins.print')):
            mock.start();self.addCleanup(mock.stop)
    def send(self,value=None):mirror.recover_once(self.config,self.store,History([value or row()]))
    def jobs(self):
        with self.store.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM media_jobs ORDER BY id')]
    def test_actual_forward_image_is_uploaded_with_speaker_and_receipt(self):
        self.send()
        uploads=[args for _,args in self.sink.sent if '--image' in args]
        self.assertEqual(len(uploads),1,'embedded image must be uploaded, not only [图片] text')
        self.assertIn('+messages-reply',uploads[0])
        self.assertEqual(self.jobs()[0]['state'],'uploaded')
        self.assertTrue(any('图片发言人' in ' '.join(a) for _,a in self.sink.sent))
    def test_parser_keeps_image_identity_without_cdn_tickets(self):
        item=parse_record(row()['text'])[0]
        self.assertEqual(item['media'],MEDIA)
        self.assertNotIn('private-',json.dumps(item))

    def test_v2_cache_decrypts_exact_image_and_keeps_original_dimensions(self):
        from tests.test_image_resources import v2
        self.image.write_bytes(v2(IMAGE,b'0123456789abcdef'))
        self.config['macos_image_key_derivation']=True
        checked=[]
        def sink(config,args):
            if '--image' in args:
                data=Path(args[args.index('--image')+1]).read_bytes();checked.append(data)
            return self.sink(config,args)
        with patch('agents.media_images._macos_candidates',return_value=[(b'0123456789abcdef',0x51)]),patch.object(mirror,'run_lark',side_effect=sink):self.send()
        self.assertEqual(checked,[IMAGE])
        self.assertFalse(list((self.root/'stage').glob('*')))

    def test_wrong_valid_image_rejected_by_digest_and_no_key_opt_in_no_upload(self):
        from tests.test_image_resources import v2,picture
        for data in (picture(),v2(IMAGE,b'0123456789abcdef')):
            with self.subTest(encrypted=data.startswith(b'\x07')):
                self.image.write_bytes(data)
                with patch('agents.media_images._macos_candidates',side_effect=AssertionError('not authorized')):
                    self.send()
                self.assertFalse(any('--image' in a for _,a in self.sink.sent))
                self.assertEqual(self.jobs()[0]['state'],'placeholder')
                # Retry the same failure, not a new fabricated message.
                with self.store.connect() as db:db.execute("UPDATE messages SET state='failed'");db.execute("UPDATE media_jobs SET state='pending'")

    def test_only_verified_thumbnail_is_labelled_and_original_is_preferred(self):
        out=BytesIO();Image.new('RGB',(8,6),'red').save(out,format='PNG');thumb=out.getvalue()
        desc={'md5':hashlib.md5(thumb).hexdigest(),'size':len(thumb)}
        self.media={**MEDIA,'thumbnail':desc}
        preview=self.root/'thumb';preview.write_bytes(thumb)
        self.resources=[{'local_paths':[str(preview)],'thumbnail_only':True},{'local_paths':[str(self.image)]}]
        content=image_item().replace('</dataitem>',f'<thumbfullmd5>{desc["md5"]}</thumbfullmd5><thumbsize>{desc["size"]}</thumbsize></dataitem>')
        self.send(row(content))
        self.assertFalse(any('已发送缩略图' in ' '.join(a) for _,a in self.sink.sent))
        self.image.unlink()
        # Exercise the staging seam with the exact resource identity contract.
        from agents.forward_images import stage_image
        item={'path':'0','media':self.media}
        path,degraded=stage_image(mirror,self.config,row(content),item)
        self.assertTrue(degraded);self.assertEqual(Path(path).read_bytes(),thumb)
        # The actual send path shows the quality warning when only preview remains.
        self.sink.sent.clear();self.sink.receipts.clear()
        with self.store.connect() as db:
            db.execute('DELETE FROM deliveries');db.execute('DELETE FROM media_jobs');db.execute("UPDATE messages SET state='failed'")
        self.send(row(content))
        self.assertTrue(any('已发送缩略图' in ' '.join(a) for _,a in self.sink.sent))

    def test_missing_cache_then_retry_keeps_text_and_name_snapshot(self):
        self.image.unlink();self.send();before=list(self.sink.sent)
        self.image.write_bytes(IMAGE)
        with self.store.connect() as db:db.execute("UPDATE media_jobs SET state='pending',updated_at=0")
        mirror.retry_media_jobs(self.config,self.store)
        added=[a for _,a in self.sink.sent[len(before):]]
        self.assertEqual(sum('--image' in a for a in added),1)
        self.assertFalse(any('转发聊天记录（' in ' '.join(a) for a in added))
        self.assertEqual(self.jobs()[0]['state'],'uploaded')

    def test_lost_upload_receipt_survives_cache_eviction(self):
        self.sink.timeout_after=3
        with self.assertRaises(mirror.CliError):self.send()
        self.image.unlink();self.send()
        self.assertEqual(sum('--image' in a for _,a in self.sink.sent),1)
        self.assertEqual(self.jobs()[0]['state'],'uploaded')

    def test_two_images_same_bytes_and_name_have_independent_jobs_and_correct_parts(self):
        gap='<dataitem datatype="1"><datadesc>'+'字'*3980+'</datadesc></dataitem>'
        self.send(row(image_item()+gap+image_item()))
        message=self.store.message('room:11',1);plan=self.store.setting(f'render:{message["id"]}')
        images=plan['images'];self.assertLess(images[0]['text_part'],images[1]['text_part'])
        uploads=[a for _,a in self.sink.sent if '--image' in a]
        self.assertEqual(len(uploads),2)
        self.assertNotEqual(uploads[0][uploads[0].index('--image')+1],uploads[1][uploads[1].index('--image')+1])
        for item,args in zip(images,uploads):
            parent=self.store.delivery(message['id'],f'text-{item["text_part"]}')
            self.assertEqual(args[args.index('--message-id')+1],parent['feishu_message_id'])
        self.assertEqual([j['state'] for j in self.jobs()],['uploaded','uploaded'])

    def test_legacy_plan_image_repair_preserves_original_text_receipt(self):
        from agents.forward_media import queue_media_repair
        with patch('agents.forward_media.image_plan',return_value=[]):self.send()
        message=self.store.message('room:11',1);key=f'render:{message["id"]}'
        plan=self.store.setting(key);plan.pop('images');self.store.save_setting(key,plan)
        with self.store.connect() as db:before=[dict(r) for r in db.execute('SELECT * FROM deliveries')]
        queue_media_repair(mirror,self.store,self.config,message['id'],'image')
        mirror.retry_media_jobs(self.config,self.store)
        with self.store.connect() as db:after=[dict(r) for r in db.execute("SELECT * FROM deliveries WHERE part='text-0'")]
        self.assertEqual(before,after);self.assertEqual(self.jobs()[0]['state'],'uploaded')
        sent=len(self.sink.sent)
        queue_media_repair(mirror,self.store,self.config,message['id'],'image');mirror.retry_media_jobs(self.config,self.store)
        self.assertEqual(len(self.sink.sent),sent)

    def test_image_repair_keeps_existing_video_receipt(self):
        from agents.forward_media import queue_media_repair
        from tests.test_forward_video import video_item,VIDEO
        video=self.root/'clip.mp4';video.write_bytes(VIDEO)
        content=image_item()+video_item()
        with patch('agents.forward_media.image_plan',return_value=[]),patch('agents.forward_media.stage_video',return_value=str(video)),patch.object(mirror,'video_cover',return_value=None):
            self.send(row(content))
        message=self.store.message('room:11',1);key=f'render:{message["id"]}'
        plan=self.store.setting(key);plan.pop('images');self.store.save_setting(key,plan)
        with self.store.connect() as db:before=[dict(r) for r in db.execute('SELECT * FROM deliveries ORDER BY part')]
        queue_media_repair(mirror,self.store,self.config,message['id'],'image')
        mirror.retry_media_jobs(self.config,self.store)
        with self.store.connect() as db:after=[dict(r) for r in db.execute("SELECT * FROM deliveries WHERE part NOT LIKE 'forward-image-%' ORDER BY part")]
        self.assertEqual(before,after)
        self.assertEqual(sum('--file' in a for _,a in self.sink.sent),1)
        self.assertEqual(sum('--image' in a for _,a in self.sink.sent),1)

    def test_mixed_images_and_videos_upload_in_record_order(self):
        from tests.test_forward_video import video_item,VIDEO
        video=self.root/'video.mp4';video.write_bytes(VIDEO)
        with patch('agents.forward_media.stage_video',return_value=str(video)),patch.object(mirror,'video_cover',return_value=None):
            self.send(row(image_item()+video_item()+image_item()))
        self.assertEqual([next(f for f in ('--image','--file') if f in a) for _,a in self.sink.sent if '--image' in a or '--file' in a],['--image','--file','--image'])

    def test_wrong_outer_resource_identity_and_source_symlink_never_uploaded(self):
        foreign=self.root/'foreign';foreign.write_bytes(IMAGE);self.image.unlink();self.image.symlink_to(foreign)
        self.send();self.assertFalse(any('--image' in a for _,a in self.sink.sent))
        self.assertEqual(self.jobs()[0]['state'],'placeholder')

    def test_wxgf_identity_is_checked_before_conversion(self):
        from agents.media_images import decode_dat,ImageResourceError
        from tests.test_wxgf_images import WXGF
        self.image.write_bytes(WXGF)
        with patch('agents.media_images.decode_wxgf',return_value=IMAGE) as convert:
            result=decode_dat(self.image,expected_md5=hashlib.md5(WXGF).hexdigest(),expected_size=len(WXGF))
            self.assertEqual(result,IMAGE);convert.assert_called_once()
        with patch('agents.media_images.decode_wxgf',side_effect=AssertionError('must reject first')):
            with self.assertRaisesRegex(ImageResourceError,'identity_mismatch'):
                decode_dat(self.image,expected_md5='0'*32,expected_size=len(WXGF))

    def test_retry_one_image_leaves_other_failure_visible(self):
        self.image.unlink();self.send(row(image_item()*2));second=self.jobs()[1]
        self.image.write_bytes(IMAGE)
        with self.store.connect() as db:db.execute("UPDATE media_jobs SET state='pending',updated_at=0 WHERE id=?",(second['id'],))
        mirror.retry_media_jobs(self.config,self.store)
        self.assertEqual([j['state'] for j in self.jobs()],['placeholder','uploaded'])
        self.assertEqual(self.store.message_state('room:11'),'placeholder')

    def test_upload_rejection_stays_visible_and_does_not_send_link(self):
        def reject(config,args):
            if '--image' in args:raise mirror.CliError('upload rejected')
            return self.sink(config,args)
        with patch.object(mirror,'run_lark',side_effect=reject):self.send()
        self.assertEqual(self.jobs()[0]['state'],'placeholder')
        self.assertEqual(len(self.store.list_media_failures()),1)
        self.assertFalse(any('private-' in ' '.join(a) for _,a in self.sink.sent))
        self.assertTrue(any('待人工排查' in ' '.join(a) for _,a in self.sink.sent))

    def test_nested_image_uses_distinct_path_and_preserves_speaker(self):
        content='<dataitem datatype="17"><sourcename>父记录</sourcename><recorditem>'+html.escape('<datalist>'+image_item()+'</datalist>')+'</recorditem></dataitem>'
        self.send(row(content))
        self.assertEqual(self.jobs()[0]['source_ref'],'forward-image:0.0')
        self.assertEqual(self.jobs()[0]['state'],'uploaded')

    def test_tampered_stage_manifest_cannot_reuse_unrelated_image(self):
        from agents.forward_images import stage_image
        item={'path':'0','media':MEDIA}
        path,_=stage_image(mirror,self.config,row(),item)
        Path(path).write_bytes(b'changed');self.image.unlink()
        with self.assertRaises(mirror.CliError):stage_image(mirror,self.config,row(),item)

    def test_expired_image_receipt_blocks_without_duplicate_or_cursor_advance(self):
        self.sink.timeout_after=3
        with self.assertRaises(mirror.CliError):self.send()
        with self.store.connect() as db:db.execute("UPDATE deliveries SET attempted_at=1 WHERE state='uncertain'")
        sent=len(self.sink.sent)
        with self.assertRaises(mirror.DeliveryUncertain):self.send()
        self.assertEqual(len(self.sink.sent),sent);self.assertEqual(self.store.get_cursor(),10)

    def thumbnail_fixture(self,fmt):
        output=BytesIO();Image.new('RGB',(8,6),'red').save(output,format=fmt);data=output.getvalue()
        desc={'md5':hashlib.md5(data).hexdigest(),'size':len(data)}
        self.media={**MEDIA,'thumbnail':desc}
        preview=self.root/'preview';preview.write_bytes(data)
        self.resources=[{'local_paths':[str(self.image)]},{'local_paths':[str(preview)],'thumbnail_only':True}]
        content=image_item().replace('</dataitem>',f'<thumbfullmd5>{desc["md5"]}</thumbfullmd5><thumbsize>{desc["size"]}</thumbsize></dataitem>')
        return row(content),data

    def test_unknown_upload_cannot_switch_from_original_to_same_format_thumbnail(self):
        value,thumb=self.thumbnail_fixture('PNG');self.sink.fail_at=2
        with self.assertRaises(mirror.CliError):self.send(value)
        self.image.unlink()
        for p in (self.root/'stage').iterdir():p.unlink()
        before=len(self.sink.sent)
        with self.assertRaises(mirror.DeliveryUncertain):self.send(value)
        self.assertEqual(len(self.sink.sent),before);self.assertEqual(self.store.get_cursor(),10)
        self.assertEqual(self.jobs()[0]['state'],'manual')

    def test_definite_rejection_can_change_to_jpeg_preview_with_correct_path_and_notice(self):
        value,thumb=self.thumbnail_fixture('JPEG')
        def reject(config,args):
            if '--image' in args:raise mirror.CliError('definitive rejection')
            return self.sink(config,args)
        with patch.object(mirror,'run_lark',side_effect=reject):self.send(value)
        self.image.unlink()
        for p in (self.root/'stage').iterdir():p.unlink()
        with self.store.connect() as db:db.execute("UPDATE media_jobs SET state='pending',updated_at=0")
        sent=[]
        def accept(config,args):
            if '--image' in args:sent.append(Path(args[args.index('--image')+1]).read_bytes())
            return self.sink(config,args)
        with patch.object(mirror,'run_lark',side_effect=accept):mirror.retry_media_jobs(self.config,self.store)
        self.assertEqual(sent,[thumb]);self.assertEqual(self.jobs()[0]['state'],'uploaded')
        self.assertTrue(any('本次补发缩略图' in ' '.join(a) for _,a in self.sink.sent))

    def test_definite_thumbnail_rejection_prefers_newly_available_original(self):
        value,thumb=self.thumbnail_fixture('PNG');self.image.unlink()
        def reject(config,args):
            if '--image' in args:raise mirror.CliError('definitive rejection')
            return self.sink(config,args)
        with patch.object(mirror,'run_lark',side_effect=reject):self.send(value)
        self.image.write_bytes(IMAGE)
        with self.store.connect() as db:db.execute("UPDATE media_jobs SET state='pending',updated_at=0")
        sent=[]
        def accept(config,args):
            if '--image' in args:sent.append(Path(args[args.index('--image')+1]).read_bytes())
            return self.sink(config,args)
        with patch.object(mirror,'run_lark',side_effect=accept):mirror.retry_media_jobs(self.config,self.store)
        self.assertEqual(sent,[IMAGE]);self.assertEqual(self.jobs()[0]['state'],'uploaded')
        self.assertTrue(any('本次补发原图' in ' '.join(a) for _,a in self.sink.sent))

    def test_unknown_thumbnail_rebuilds_same_preview_even_when_original_arrives(self):
        value,thumb=self.thumbnail_fixture('PNG');self.image.unlink();self.sink.fail_at=2
        with self.assertRaises(mirror.CliError):self.send(value)
        for p in (self.root/'stage').iterdir():p.unlink()
        self.image.write_bytes(IMAGE)
        sent=[]
        def accept(config,args):
            if '--image' in args:sent.append(Path(args[args.index('--image')+1]).read_bytes())
            return self.sink(config,args)
        with patch.object(mirror,'run_lark',side_effect=accept):self.send(value)
        self.assertEqual(sent,[thumb]);self.assertEqual(self.jobs()[0]['state'],'uploaded')
        self.assertEqual(self.store.get_cursor(),11)


class ForwardImageReaderTest(unittest.TestCase):
    def test_encrypted_rec_candidates_are_scoped_and_verified_after_decode(self):
        from integrations.reader_forward import read_forward
        from tests.test_image_resources import v2
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'messages.db'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE Msg_room(local_id,server_id,create_time,text)')
                db.execute('INSERT INTO Msg_room VALUES(11,911,1011,?)',(row()['text'],))
            def connect(path):
                db=sqlite3.connect(path);db.row_factory=sqlite3.Row;return db
            database=SimpleNamespace(message_dbs=[source],resource_roots=[root],connect=connect)
            reader=SimpleNamespace(resolve_username=lambda db,t:t,message_table=lambda t:'Msg_'+t,
                message_projection=lambda *a:'*',decode_message_content=lambda r:r['text'],
                visible_message_content=lambda s,*a:s,ReaderError=lambda msg,code:RuntimeError(code),
                hardlink_resources=lambda *a:[])
            args=SimpleNamespace(talker='room',local_id=11,server_id=911,create_time=1011,item_path='0')
            group=hashlib.md5(b'room').hexdigest()
            folder=root/'attach'/group/'2026-09'/'Rec'/'record'/'Img';folder.mkdir(parents=True)
            image=folder/'0';image.write_bytes(v2(IMAGE,b'0123456789abcdef'))
            result=read_forward(reader,database,args)
            self.assertEqual(result['resources'],[{'local_paths':[str(image.resolve())],'thumbnail_only':False}])
            self.assertEqual(result['media'],MEDIA)
            args.server_id=999
            with self.assertRaisesRegex(RuntimeError,'identity_mismatch'):read_forward(reader,database,args)
            args.server_id=911
            other=root/'attach'/'other-group'/'2026-09'/'Rec'/'record'/'Img';other.mkdir(parents=True)
            moved=other/'0';image.rename(moved)
            self.assertEqual(read_forward(reader,database,args)['resources'],[])
            image.symlink_to(moved)
            self.assertEqual(read_forward(reader,database,args)['resources'],[])
