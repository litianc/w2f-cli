import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agents.manage import Manager
from agents.storage import MirrorStore
from agents import wechat_feishu_mirror as mirror


class MediaRetryRoundsTest(unittest.TestCase):
    def test_manual_retry_after_limit_preserves_history_and_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=MirrorStore(root/'state.db')
            config={'binding_id':1,'wechat_talker':'room','feishu_chat_id':'chat','lark_profile':'demo','sender_map':{'wx':'实名'}}
            store.initialize(config);cfg=root/'config.json';cfg.write_text('{}');manager=Manager(store,cfg)
            row={'talker':'room','local_id':11,'sender_wxid':'wx','kind_name':'image'}
            with patch.object(mirror,'run_lark',return_value={'data':{'message_id':'om_original'}}) as send:
                with patch.object(mirror,'stage_media',side_effect=mirror.CliError('resource_missing')):
                    mirror.send_row(config,row,{},store)
                    job=store.list_media_failures()[0]
                    with store.connect() as db:db.execute("UPDATE media_jobs SET attempts=3,state='manual'")
                    for request in range(2):manager.post('media-failures',{'op':'retry','id':job['id']})
                    self.assertEqual(store.setting(f"media_retry_base:{job['id']}"),3)
                    for attempt in range(3):
                        with store.connect() as db:db.execute('UPDATE media_jobs SET updated_at=0')
                        mirror.retry_media_jobs(config,store)
                failed=store.list_media_failures()[0]
                self.assertEqual(failed['attempts'],6);self.assertEqual(failed['state'],'manual')
                self.assertEqual(send.call_count,1)
                manager.post('media-failures',{'op':'retry','id':job['id']})
                image=root/'fixed.png';image.write_bytes(b'fixture')
                with patch.object(mirror,'stage_media',return_value=(str(image),False)):
                    mirror.retry_media_jobs(config,store);mirror.retry_media_jobs(config,store)
                self.assertEqual(send.call_count,2)
                self.assertEqual(send.call_args.args[1][0],'+messages-reply')
                self.assertEqual(store.list_media_failures(),[])
                with store.connect() as db:
                    saved=db.execute('SELECT attempts FROM media_jobs').fetchone()[0]
                self.assertEqual(saved,7)

if __name__=='__main__':unittest.main()
