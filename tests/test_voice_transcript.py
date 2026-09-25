"""Voice transcript takes the text path; empty transcript keeps audio fallback."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from integrations.wda_reader import map_row


class VoiceTranscriptTest(unittest.TestCase):
    def test_wda_contract_only_includes_nonempty_voice_transcript(self):
        base={"type":34,"localId":8,"content":"<msg/>","senderUsername":"wx"}
        row=map_row('room', {**base,"voiceTranscript":"  你好 😀  "},{'wx':'昵称'})
        self.assertEqual(row['voice_transcript'],'你好 😀')
        self.assertNotIn('voice_transcript',map_row('room',{**base,"voiceTranscript":"  "},{}))
        self.assertNotIn('voice_transcript',map_row('room',{**base,"type":1,"voiceTranscript":"你好"},{}))

    def test_transcript_sends_named_text_without_media(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MirrorStore(Path(directory)/'state.db')
            store.initialize({'wechat_talker':'room','feishu_chat_id':'chat','sender_map':{'wx':'实名'}})
            row={'talker':'room','local_id':8,'sender_wxid':'wx','sender':'昵称','kind_name':'voice',
                 'text':'<msg/>','voice_transcript':'语音内容 😀'}
            with patch.object(mirror,'run_lark',return_value={'data':{'message_id':'om_voice'}}) as send,patch.object(mirror,'stage_media',side_effect=AssertionError('no upload')):
                mirror.send_row({'lark_profile':'p','feishu_chat_id':'chat','sender_map':{'wx':'实名'}},row,{},store)
            args=send.call_args.args[1]
            self.assertEqual(args[args.index('--text')+1],'实名：语音内容 😀')
            saved=store.message('room:8',1)
            self.assertEqual(saved['kind'],'voice')
            self.assertIn('voice_transcript',saved['payload_json'])
            with store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM media_jobs').fetchone()[0],0)

    def test_empty_transcript_falls_back_to_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MirrorStore(Path(directory)/'state.db')
            store.initialize({'wechat_talker':'room','feishu_chat_id':'chat','sender_map':{'wx':'实名'}})
            row={'talker':'room','local_id':9,'sender_wxid':'wx','sender':'昵称','kind_name':'voice','text':'<msg/>','voice_transcript':' '}
            with patch.object(mirror,'run_lark',return_value={'data':{'message_id':'om_audio'}}) as send,patch.object(mirror,'stage_media',return_value=(str(Path(directory)/'voice.ogg'),False)):
                mirror.send_row({'lark_profile':'p','feishu_chat_id':'chat','sender_map':{'wx':'实名'}},row,{},store)
            self.assertTrue(any('--audio' in call.args[1] for call in send.call_args_list))


if __name__=='__main__':unittest.main()
