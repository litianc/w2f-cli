import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.storage import MirrorStore
from agents import wechat_feishu_mirror as mirror


class MirrorStoreTest(unittest.TestCase):
    def test_cursor_control_lease_and_idempotent_message(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MirrorStore(Path(directory) / "mirror.sqlite3")
            store.initialize({
                "wechat_chat_name": "源群", "wechat_talker": "talker",
                "feishu_chat_name": "目标群", "feishu_chat_id": "chat",
                "initial_cursor": 7, "sender_map": {"wxid": "实名"},
            })
            self.assertEqual(store.get_cursor(), 7)
            row = {"talker": "talker", "local_id": 8, "sender_wxid": "wxid", "sender": "昵称", "kind_name": "text", "text": "你好"}
            first = store.upsert_message("talker:8", row, "text")
            second = store.upsert_message("talker:8", row, "text")
            self.assertEqual(first, second)
            store.mark_message("talker:8", "sent")
            self.assertEqual(store.status()["messages"], {"sent": 1})
            store.set_cursor(8)
            self.assertEqual(store.get_cursor(), 8)
            store.set_control("paused", actor="codex")
            self.assertEqual(store.get_control()["status"], "paused")
            token = store.acquire_lease(pid=4242)
            self.assertTrue(store.status()["worker"]["alive"])
            store.release_lease(token)
            self.assertFalse(store.status()["worker"]["alive"])


class MirrorFormattingTest(unittest.TestCase):
    def test_xml_url_and_unknown_sender_fallback(self):
        row = {"text": '<msg><img cdnurl = "https://example.test/a.png&amp;x=1" /></msg>'}
        self.assertEqual(mirror.media_url(row), "https://example.test/a.png&x=1")
        self.assertEqual(mirror.clean_body({"kind_name": "sticker", "text": "ignored"}), "[表情]")
        with tempfile.TemporaryDirectory() as directory:
            store = MirrorStore(Path(directory) / "mirror.sqlite3")
            store.initialize({"wechat_chat_name": "源", "wechat_talker": "talker", "feishu_chat_name": "目标", "feishu_chat_id": "chat", "sender_map": {}})
            config = {"lark_profile": "test", "feishu_chat_id": "chat", "sender_map": {}}
            message = {"talker": "talker", "local_id": 1, "sender_wxid": "unknown", "sender": "微信昵称", "kind_name": "text", "text": "内容"}
            with patch.object(mirror, "run_lark", return_value={"data": {"message_id": "om_test"}}) as send:
                mirror.send_row(config, message, {"pending_senders": {}}, store)
            self.assertEqual(send.call_args.args[1][send.call_args.args[1].index("--text") + 1], "微信昵称：内容")
            self.assertEqual(store.message_state("talker:1"), "sent")
            self.assertEqual(store.status()["pending_senders"], 1)


if __name__ == "__main__":
    unittest.main()
