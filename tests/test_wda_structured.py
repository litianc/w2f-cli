"""wda_reader 透传 WDA 结构化字段：视频号 objectId、转发 recordItem、表情 md5（#34 数据层修复）。

WDA 的 content 字段是渲染后的纯文本，原始 XML/摘要放在独立结构化字段里
（finderFeed 侧为 objectId/objectNonceId，转发为 recordItem，表情为 emojiMd5）。
适配层必须把它们还原成 macOS reader 的行契约，镜像端既有安全校验
（_channel_share_url 拒绝签名 CDN URL）继续生效。
"""
import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "wda_reader", Path(__file__).resolve().parents[1] / "integrations" / "wda_reader.py")
wda = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wda)

from agents import wechat_feishu_mirror as mirror


def message(base, sub=0, **fields):
    payload = {"type": ((sub << 32) | base) if sub else base,
               "localId": 1, "serverId": "7984644575835973134",
               "senderUsername": "wxid_x", "content": fields.pop("content", "")}
    payload.update(fields)
    return payload


class ChannelFeedXml(unittest.TestCase):
    def test_object_ids_render_shareable_page_without_signed_urls(self):
        m = message(49, 51, title="一财AI分享", objectId="15020616717565566368",
                    objectNonceId="581263754782936647_0_140_0_0", **{"from": "一财商学"})
        row = wda.map_row("room@chatroom", m, {})
        self.assertEqual(row["kind_name"], "channel_video")
        body = mirror.channel_video_body(row["text"])
        self.assertIn("视频号：一财AI分享", body)
        self.assertIn("作者：一财商学", body)
        self.assertIn("https://channels.weixin.qq.com/web/pages/feed?oid=", body)
        self.assertNotIn("stodownload", body)
        self.assertNotIn("encfilekey", body)

    def test_xml_text_values_are_escaped(self):
        m = message(49, 51, title="标题</title><script>", objectId="15020616717565566368",
                    objectNonceId="581263754782936647_0_140_0_0")
        row = wda.map_row("room@chatroom", m, {})
        self.assertNotIn("<script>", row["text"])

    def test_missing_ids_keep_plain_content(self):
        m = message(49, 51, content="只有描述文本")
        row = wda.map_row("room@chatroom", m, {})
        self.assertEqual(row["text"], "只有描述文本")


RECORD = ('<recordinfo><title>群聊的聊天记录</title><datalist count="1">'
          '<dataitem datatype="1" dataid="a1"><sourcename>张三</sourcename>'
          '<datadesc>你好</datadesc><sourcetime>2026-09-27 21:31</sourcetime>'
          '</dataitem></datalist></recordinfo>')


class ForwardRecordItem(unittest.TestCase):
    def test_recorditem_field_becomes_contract_text_and_items(self):
        m = message(49, 19, content="张三: 你好", recordItem=RECORD)
        row = wda.map_row("room@chatroom", m, {})
        self.assertEqual(row["kind_name"], "forward_chat")
        self.assertEqual(row["text"], RECORD)
        self.assertEqual(len(row["forward_items"]), 1)
        self.assertEqual(row["forward_items"][0].get("sender"), "张三")

    def test_without_recorditem_keeps_outline_and_no_items(self):
        m = message(49, 19, content="张三: 你好")
        row = wda.map_row("room@chatroom", m, {})
        self.assertEqual(row["text"], "张三: 你好")
        self.assertEqual(row["forward_items"], [])


class EmojiDigestPassthrough(unittest.TestCase):
    def test_emoji_md5_passes_through(self):
        m = message(47, content="[表情]", emojiMd5="a4427dc9b3e9a872538b57706f3be117")
        row = wda.map_row("room@chatroom", m, {})
        self.assertEqual(row["emoji_md5"], "a4427dc9b3e9a872538b57706f3be117")

    def test_invalid_digest_is_omitted(self):
        row = wda.map_row("room@chatroom", message(47, content="[表情]", emojiMd5=""), {})
        self.assertNotIn("emoji_md5", row)


class EmojiMediaDigestGuard(unittest.TestCase):
    def media(self, md5):
        args = wda.build_parser().parse_args(
            ["media", "--talker", "room@chatroom", "--local-id", "88",
             "--server-id", "7984644575835973134"])
        stored = message(47, content="[表情]", emojiMd5=md5, localId=88)
        return args, stored

    def test_mismatched_bytes_are_never_returned_or_cached(self):
        args, stored = self.media("a" * 32)
        with tempfile.TemporaryDirectory() as td, \
             patch.dict(os.environ, {"W2F_WDA_CACHE": td}), \
             patch.object(wda, "find_message", return_value=stored), \
             patch.object(wda, "fetch_media", return_value=(b"undecryptable-garbage", "application/octet-stream")):
            result = wda.cmd_media(args)
            self.assertEqual(result["data"]["resources"], [])
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_matching_bytes_are_cached_and_returned(self):
        payload = b"\x89PNG\r\n\x1a\nfake-but-digest-matches"
        args, stored = self.media(hashlib.md5(payload).hexdigest())
        with tempfile.TemporaryDirectory() as td, \
             patch.dict(os.environ, {"W2F_WDA_CACHE": td}), \
             patch.object(wda, "find_message", return_value=stored), \
             patch.object(wda, "fetch_media", return_value=(payload, "image/png")):
            result = wda.cmd_media(args)
            paths = result["data"]["resources"][0]["local_paths"]
            self.assertTrue(Path(paths[0]).is_file())


class ForwardParseLogging(unittest.TestCase):
    def test_unparseable_datalist_logs_to_stderr(self):
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            items = wda._forward_items("<datalist count='1'><broken")
        self.assertEqual(items, [])
        self.assertTrue(err.getvalue().strip())


class StickerMetadataFallback(unittest.TestCase):
    def test_emoji_md5_field_used_when_no_xml(self):
        row = {"content": "[表情]", "emoji_md5": "a4427dc9b3e9a872538b57706f3be117"}
        self.assertEqual(mirror.sticker_metadata(row), {"md5": "a4427dc9b3e9a872538b57706f3be117"})

    def test_xml_still_wins_over_field(self):
        xml_md5 = "b" * 32
        row = {"content": f'<msg><emoji md5="{xml_md5}" /></msg>',
               "emoji_md5": "a4427dc9b3e9a872538b57706f3be117"}
        self.assertEqual(mirror.sticker_metadata(row).get("md5"), xml_md5)

    def test_absent_everywhere_stays_empty(self):
        self.assertEqual(mirror.sticker_metadata({"content": "[表情]"}), {})


if __name__ == "__main__":
    unittest.main()
