import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from io import BytesIO

from PIL import Image

from agents.storage import MirrorStore
from agents import wechat_feishu_mirror as mirror
from agents import manage


class MirrorStoreTest(unittest.TestCase):
    def test_setup_init_writes_non_secret_runtime_config_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config" / "mirror.json"
            db = root / "data" / "mirror.sqlite3"
            first = manage.setup_init(config, db, {"wechat_talker": "talker", "feishu_chat_id": "chat", "lark_profile": "profile", "app_secret": "must-not-persist"})
            self.assertTrue(first["initialized"])
            config_text = config.read_text(encoding="utf-8")
            self.assertNotIn("must-not-persist", config_text)
            manage.setup_init(config, db, {"wechat_chat_name": "新源", "wechat_talker": "talker", "feishu_chat_id": "chat", "lark_profile": "profile"})
            self.assertTrue(list((db.parent / "backups").glob("mirror.sqlite3.*.bak")))

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

    def test_multiple_bindings_are_isolated_and_duplicates_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MirrorStore(Path(directory) / "mirror.sqlite3")
            store.initialize({"wechat_chat_name": "源1", "wechat_talker": "talker-1", "feishu_chat_name": "目标1", "feishu_chat_id": "chat-1", "sender_map": {}})
            store.upsert_profile("second", "app-second")
            b2 = store.create_binding({"wechat_chat_name": "源2", "wechat_talker": "talker-2", "feishu_chat_name": "目标2", "feishu_chat_id": "chat-2", "lark_profile": "second"})
            self.assertEqual(b2["id"], 2)
            with self.assertRaises(ValueError):
                store.create_binding({"wechat_chat_name": "重复源", "wechat_talker": "talker-1", "feishu_chat_name": "目标3", "feishu_chat_id": "chat-3", "lark_profile": "second"})
            with self.assertRaises(ValueError):
                store.create_binding({"wechat_chat_name": "源3", "wechat_talker": "talker-3", "feishu_chat_name": "重复目标", "feishu_chat_id": "chat-1", "lark_profile": "second"})
            row = {"talker": "talker-1", "local_id": 9, "sender_wxid": "wx", "sender": "甲", "kind_name": "text", "text": "a"}
            store.upsert_message("talker-1:9", row, "text", binding_id=1)
            row["talker"] = "talker-2"
            store.upsert_message("talker-2:9", row, "text", binding_id=2)
            self.assertEqual(store.status(1)["messages"], {"received": 1})
            self.assertEqual(store.status(2)["messages"], {"received": 1})

    def test_binding_name_audit_and_profile_disable_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MirrorStore(Path(directory) / "mirror.sqlite3")
            store.initialize({"wechat_chat_name": "源", "wechat_talker": "talker", "feishu_chat_name": "目标", "feishu_chat_id": "chat", "lark_profile": "profile", "lark_app_id": "app", "sender_map": {}})
            renamed = store.rename_binding(1, wechat_chat_name="新源", feishu_chat_name="新目标")
            self.assertEqual((renamed["wechat_chat_name"], renamed["feishu_chat_name"]), ("新源", "新目标"))
            store.set_control(1, "running", actor="test")
            store.set_profile_enabled("profile", False)
            self.assertEqual(store.get_control(1)["status"], "stopped")


class MirrorFormattingTest(unittest.TestCase):
    def test_group_invite_renders_names_and_current_profile_mentions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MirrorStore(Path(directory) / "mirror.sqlite3")
            store.initialize({"wechat_chat_name": "源", "wechat_talker": "room@chatroom", "feishu_chat_name": "目标",
                              "feishu_chat_id": "chat", "lark_profile": "p", "lark_app_id": "app"})
            person = store.create_person("张三")
            store.link_wechat_identity(person["id"], "default", "wx_a", "旧昵称")
            profile = next(p for p in store.list_profiles() if p["profile_name"] == "p")
            store.link_feishu_identity(person["id"], profile["id"], "ou_a")
            row = {"talker": "room@chatroom", "local_id": 99, "sender_wxid": "room@chatroom", "sender": "room@chatroom",
                   "kind_name": "system", "text": '"$username$"邀请"$names$"加入了群聊 wx_a 旧昵称 wx_b 小李'}
            with patch.object(mirror, "run_lark", return_value={"data": {"message_id": "om_test"}}) as send:
                mirror.send_row({"lark_profile": "p", "feishu_chat_id": "chat"}, row, {}, store)
            body = send.call_args.args[1][send.call_args.args[1].index("--text") + 1]
            self.assertIn('<at user_id="ou_a">张三</at>', body)
            self.assertIn("小李", body)
            self.assertNotIn("wx_b", body)
            self.assertNotIn("$names$", body)
            self.assertTrue(body.startswith("群事件："))
            self.assertEqual(store.status()["pending_senders"], 0)

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

    def test_channel_video_uses_stable_page_link_without_signed_cdn_urls(self):
        xml = '''<msg><appmsg><title>当前微信版本不支持展示该内容，请升级到最新版本。</title><type>51</type><url>https://support.weixin.qq.com/update/</url><finderFeed><objectId><![CDATA[14978452874204420608]]></objectId><objectNonceId><![CDATA[7434433790648304237_0_39_26_1_1789212716173352_b063c2da]]></objectNonceId><nickname><![CDATA[视频号作者]]></nickname><desc><![CDATA[京郊避暑天堂来了😀]]></desc><mediaList><media><url><![CDATA[https://wxapp.tc.qq.com/251/20302/stodownload?encfilekey=secret&token=short-lived]]></url></media></mediaList></finderFeed></appmsg></msg>'''
        body = mirror.channel_video_body(xml)
        self.assertIn('京郊避暑天堂来了😀', body)
        self.assertIn('https://channels.weixin.qq.com/web/pages/feed?oid=', body)
        self.assertIn('作者：视频号作者', body)
        self.assertNotIn('encfilekey', body)
        self.assertNotIn('stodownload', body)
        self.assertNotIn('support.weixin.qq.com/update', body)

    def test_quote_info_extracts_parent_and_sends_fallback_context(self):
        xml = '<msg><appmsg><title>回复</title><refermsg><type>1</type><svrid>12345</svrid><fromusr>talker</fromusr><displayname>原作者</displayname><content>原消息</content></refermsg></appmsg></msg>'
        self.assertEqual(mirror.quote_info({"text": xml}), {"key": "talker:12345", "sender": "原作者", "content": "原消息", "server_id": "12345"})
        with tempfile.TemporaryDirectory() as directory:
            store = MirrorStore(Path(directory) / "mirror.sqlite3")
            store.initialize({"wechat_chat_name": "源", "wechat_talker": "talker", "feishu_chat_name": "目标", "feishu_chat_id": "chat", "sender_map": {"wx": "实名"}})
            config = {"lark_profile": "test", "feishu_chat_id": "chat", "sender_map": {"wx": "实名"}}
            row = {"talker": "talker", "local_id": 2, "server_id": "new", "sender_wxid": "wx", "sender": "昵称", "kind_name": "quote", "text": xml}
            with patch.object(mirror, "run_lark", return_value={"data": {"message_id": "om_test"}}) as send:
                mirror.send_row(config, row, {"pending_senders": {}}, store)
            sent_text = send.call_args.args[1][send.call_args.args[1].index("--text") + 1]
            self.assertIn("> 原作者：原消息", sent_text)
            self.assertEqual(store.message_state("talker:2"), "sent")

    def test_merged_forward_extracts_speakers_media_and_links(self):
        xml = """<msg><appmsg><type>19</type><recorditem><![CDATA[
        <datalist><dataitem><sourcename>甲</sourcename><datadesc>第一句 😀</datadesc></dataitem>
        <dataitem><sourcename>乙</sourcename><type>3</type></dataitem>
        <dataitem><sourcename>丙</sourcename><datadesc>文章</datadesc><url>https://example.test/a</url></dataitem>
        </datalist>]]></recorditem></appmsg></msg>"""
        self.assertTrue(mirror.is_forward_row({"kind_name": "app", "text": xml}))
        body = mirror.clean_body({"kind_name": "app", "text": xml})
        self.assertIn("甲：第一句 😀", body)
        self.assertIn("乙：[图片]", body)
        self.assertIn("丙：文章", body)
        self.assertIn("https://example.test/a", body)

    def test_reader_media_paths_uses_local_resource(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": '{"resources":[{"local_path":"/tmp/example.jpg"}]}', "stderr": ""})()
        with patch.object(mirror.subprocess, "run", return_value=completed) as run:
            with patch.object(Path, "is_file", return_value=True):
                paths = mirror.reader_media_paths({"talker": "talker", "local_id": 9, "server_id": "s9"})
        self.assertEqual(paths[0], Path("/tmp/example.jpg"))
        self.assertIn("--include-local-paths", run.call_args.args[0])
        self.assertNotIn("true", run.call_args.args[0])

    def test_reader_media_paths_falls_back_for_legacy_wda_flag(self):
        missing_value = type("Completed", (), {"returncode": 2, "stdout": "", "stderr": "argument --include-local-paths: expected one argument"})()
        completed = type("Completed", (), {"returncode": 0, "stdout": '{"resources":[{"local_path":"/tmp/example.jpg"}]}', "stderr": ""})()
        with patch.object(mirror.subprocess, "run", side_effect=[missing_value, completed]) as run:
            with patch.object(Path, "is_file", return_value=True):
                paths = mirror.reader_media_paths({"talker": "talker", "local_id": 9})
        self.assertEqual(paths, [Path("/tmp/example.jpg")])
        first = run.call_args_list[0].args[0]
        second = run.call_args_list[1].args[0]
        self.assertEqual(first[first.index("--include-local-paths") + 1:], [])
        self.assertEqual(second[second.index("--include-local-paths") + 1:], ["true"])

    def test_wechat_cache_fallback_matches_message_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            match = root / "ImageTemp" / "901_1700000000_thumb.jpg"
            unrelated = root / "ImageTemp" / "1901_1700000000_thumb.jpg"
            match.parent.mkdir()
            match.write_bytes(b"jpg")
            unrelated.write_bytes(b"jpg")
            paths = mirror.wechat_cache_paths({"local_id": 901, "server_id": "s901", "wechat_cache_dirs": [str(root)]})
            self.assertEqual(paths, [match])

    def test_stage_media_uses_cache_when_reader_has_no_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "901_thumb.jpg"
            source.write_bytes(b"image")
            destination = root / "staging"
            with patch.object(mirror, "reader_media_result", return_value=([], False)):
                staged, thumbnail_only = mirror.stage_media({"binding_id": 1, "talker": "room", "local_id": 901, "wechat_cache_dirs": [str(root)], "media_dir": str(destination)}, "image")
            self.assertTrue(staged)
            self.assertFalse(thumbnail_only)
            self.assertEqual(Path(staged).read_bytes(), b"image")

    def test_stage_media_prefers_largest_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thumb = root / "901_thumb.jpg"
            original = root / "901_original.jpg"
            thumb.write_bytes(b"thumb")
            original.write_bytes(b"original-image-bytes")
            with patch.object(mirror, "reader_media_result", return_value=([thumb, original], False)):
                staged, thumbnail_only = mirror.stage_media({"binding_id": 1, "talker": "room", "local_id": 901, "media_dir": str(root / "staging")}, "image")
            self.assertEqual(Path(staged).read_bytes(), b"original-image-bytes")
            self.assertFalse(thumbnail_only)

    def test_reader_media_result_reports_thumbnail_only(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": '{"data":{"resources":[{"local_paths":["/tmp/example.jpg"],"thumbnail_only":true}]}}', "stderr": ""})()
        with patch.object(mirror.subprocess, "run", return_value=completed):
            with patch.object(Path, "is_file", return_value=True):
                paths, thumbnail_only = mirror.reader_media_result({"talker": "talker", "local_id": 9})
        self.assertEqual(paths, [Path("/tmp/example.jpg")])
        self.assertTrue(thumbnail_only)

    def test_wechat_dat_decoder_accepts_xor_image(self):
        image = Image.new("RGB", (2, 2), (255, 0, 0))
        buf = BytesIO(); image.save(buf, format="JPEG")
        original = buf.getvalue(); key = 0x37
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.dat"
            source.write_bytes(bytes(byte ^ key for byte in original))
            destination = Path(directory) / "decoded.dat"
            self.assertTrue(mirror.decode_wechat_dat(source, destination))
            with Image.open(destination.with_suffix(".jpg")) as decoded:
                self.assertEqual(decoded.size, (2, 2))

    def test_unknown_sender_reminder_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MirrorStore(Path(directory) / "mirror.sqlite3")
            store.initialize({"wechat_chat_name": "源", "wechat_talker": "talker", "feishu_chat_name": "目标", "feishu_chat_id": "chat", "sender_map": {}})
            config = {"lark_profile": "test", "feishu_chat_id": "chat", "sender_map": {}, "admin_feishu_user_id": "ou_admin"}
            row = {"talker": "talker", "local_id": 3, "sender_wxid": "unknown", "sender": "微信昵称", "kind_name": "text", "text": "内容"}
            with patch.object(mirror, "run_lark", return_value={"data": {"message_id": "om_test"}}) as send:
                mirror.send_row(config, row, {"pending_senders": {}}, store)
                row["local_id"] = 4
                mirror.send_row(config, row, {"pending_senders": {}}, store)
            self.assertEqual(send.call_count, 3)
            self.assertEqual(store.list_confirmations()[0]["notification_status"], "sent")


if __name__ == "__main__":
    unittest.main()
