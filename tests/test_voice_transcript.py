"""语音消息转写投递：行内转写优先按文本送达，无转写回落音频链路。

读取器只透传 voice_transcript 可选字段；「转写→文本」的交付决策在
worker，与具体读取器无关。全部模拟读取器行，不发真实群。
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SPEC = importlib.util.spec_from_file_location(
    "wda_reader", Path(__file__).resolve().parents[1] / "integrations" / "wda_reader.py")
wda = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wda)

from agents.storage import MirrorStore
from agents import wechat_feishu_mirror as mirror


def make_config(root, talker="room"):
    return {"binding_id": 1, "source_account": "demo", "wechat_talker": talker,
            "wechat_chat_name": "源", "feishu_chat_id": "chat", "feishu_chat_name": "目标",
            "lark_profile": "p", "lark_app_id": "app", "data_dir": str(root),
            "sqlite_file": str(root / "state.db"), "sender_map": {"wx": "实名"}}


class AdapterTranscriptTest(unittest.TestCase):
    def test_voice_row_carries_transcript(self):
        row = wda.map_row("room@chatroom", {"type": 34, "localId": 5, "content": "<msg/>", "voiceTranscript": "大家好"}, {})
        self.assertEqual(row["kind_name"], "voice")
        self.assertEqual(row["voice_transcript"], "大家好")

    def test_voice_row_without_transcript_has_no_field(self):
        row = wda.map_row("room@chatroom", {"type": 34, "localId": 5, "content": "<msg/>"}, {})
        self.assertEqual(row["kind_name"], "voice")
        self.assertNotIn("voice_transcript", row)

    def test_blank_transcript_is_dropped(self):
        row = wda.map_row("room@chatroom", {"type": 34, "localId": 5, "content": "<msg/>", "voiceTranscript": "  "}, {})
        self.assertNotIn("voice_transcript", row)

    def test_non_voice_message_ignores_transcript(self):
        row = wda.map_row("room@chatroom", {"type": 1, "localId": 5, "content": "hi", "voiceTranscript": "x"}, {})
        self.assertNotIn("voice_transcript", row)


class WorkerTranscriptDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = MirrorStore(self.root / "state.db")
        self.store.initialize(make_config(self.root))
        self.config = make_config(self.root)

    def voice_row(self, **extra):
        return {"talker": "room", "local_id": 10, "server_id": "s10", "sender_wxid": "wx",
                "sender": "昵称", "kind_name": "voice", "text": "<msg/>", "create_time": 1, **extra}

    def test_transcribed_voice_is_delivered_as_text_without_media(self):
        with patch.object(mirror, "run_lark", return_value={"data": {"message_id": "om_t"}}) as send, \
             patch.object(mirror, "stage_media", side_effect=AssertionError("转写优先时不应走媒体链路")):
            mirror.send_row(self.config, self.voice_row(voice_transcript="大家好"), {}, self.store)
        self.assertEqual(send.call_count, 1)
        args = send.call_args.args[1]
        self.assertEqual(args[args.index("--text") + 1], "实名：大家好")
        self.assertEqual(self.store.message_state("room:10"), "sent")
        stored = self.store.message("room:10", 1)
        self.assertEqual(stored["kind"], "voice")  # 交付为文本，存储类型仍如实是语音

    def test_voice_without_transcript_falls_back_to_audio(self):
        with patch.object(mirror, "run_lark", return_value={"data": {"message_id": "om_a"}}) as send, \
             patch.object(mirror, "stage_media", return_value=("/tmp/fake.ogg", False)) as stage:
            mirror.send_row(self.config, self.voice_row(), {}, self.store)
        stage.assert_called_once()
        self.assertEqual(send.call_count, 2)  # 姓名行 + 音频
        args = send.call_args.args[1]
        self.assertIn("--audio", args)
        self.assertEqual(self.store.message_state("room:10"), "sent")


if __name__ == "__main__":
    unittest.main()
