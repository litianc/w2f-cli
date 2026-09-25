"""向导「新建飞书群」端点：参数校验与 lark-cli 调用（全部打桩，不触网）。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import manage
from agents.storage import MirrorStore


class WizardCreateTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = MirrorStore(root / "state.db")
        self.store.initialize({"binding_id": 1, "source_account": "demo", "wechat_talker": "room",
                               "wechat_chat_name": "源", "feishu_chat_id": "chat", "feishu_chat_name": "目标",
                               "lark_profile": "p", "lark_app_id": "app", "data_dir": str(root),
                               "sqlite_file": str(root / "state.db")})
        self.manager = manage.Manager(self.store, root / "config.json")

    def test_creates_group_and_returns_chat_id(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            return {"data": {"chat_id": "oc_new"}}
        person = self.store.create_person("本人")
        profile = next(p for p in self.store.list_profiles() if p["profile_name"] == "p")
        self.store.link_feishu_identity(person["id"], profile["id"], "ou_me")
        with patch.object(manage, "run_cli", side_effect=fake_run):
            result = self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "新群",
                                                  "add_owner": True, "owner_open_id": "ou_me"})
        self.assertEqual(result["chat_id"], "oc_new")
        self.assertEqual(result["name"], "新群")
        self.assertIn("+chat-create", calls[0])
        self.assertIn("ou_me", calls[0])

    def test_create_without_owner_omits_member_flag(self):
        with patch.object(manage, "run_cli", return_value={"data": {"chat_id": "oc_x"}}) as run:
            result = self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "群"})
        self.assertEqual(result["chat_id"], "oc_x")
        self.assertNotIn("--users", run.call_args.args[0])

    def test_owner_must_be_linked_to_selected_profile(self):
        person = self.store.create_person("测试用户")
        other = self.store.upsert_profile("other", "app-other")
        self.store.link_feishu_identity(person["id"], other["id"], "ou_foreign")
        with patch.object(manage, "run_cli") as run:
            with self.assertRaisesRegex(ValueError, "当前 Profile"):
                self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "新群",
                                                      "add_owner": True, "owner_open_id": "ou_foreign"})
        run.assert_not_called()

    def test_group_share_link_and_graceful_create_fallback(self):
        person = self.store.create_person("本人")
        profile = next(p for p in self.store.list_profiles() if p["profile_name"] == "p")
        self.store.link_feishu_identity(person["id"], profile["id"], "ou_me")
        with patch.object(manage, "run_cli", side_effect=[{"data": {"chat_id": "oc_new"}},
                                                          {"data": {"share_link": "https://applink.feishu.cn/join", "expire_time": "123"}}]) as run:
            result = self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "新群",
                                                   "add_owner": True, "owner_open_id": "ou_me"})
        self.assertEqual(result["share_link"], "https://applink.feishu.cn/join")
        self.assertEqual(result["expire_time"], "123")
        self.assertIn("chats", run.call_args_list[1].args[0])
        self.assertIn("link", run.call_args_list[1].args[0])
        with patch.object(manage, "run_cli", side_effect=[{"data": {"chat_id": "oc_new"}}, RuntimeError("无分享权限")]):
            result = self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "新群"})
        self.assertEqual(result["chat_id"], "oc_new")
        self.assertIn("无分享权限", result["share_error"])

    def test_existing_binding_share_link_uses_saved_profile(self):
        with patch.object(manage, "run_cli", return_value={"data": {"share_link": "https://applink.feishu.cn/join"}}) as run:
            result = self.manager.post("bindings", {"op": "share-link", "id": 1})
        self.assertEqual(result["share_link"], "https://applink.feishu.cn/join")
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--profile") + 1], "p")
        self.assertEqual(args[args.index("--chat-id") + 1], "chat")

    def test_validates_name_profile_and_owner_before_calling_cli(self):
        def must_not_run(args, **kwargs):
            raise AssertionError("校验失败时应先于 lark-cli 调用")
        with patch.object(manage, "run_cli", side_effect=must_not_run):
            cases = [{"profile": "", "name": "x"},
                     {"profile": "p", "name": "   "},
                     {"profile": "p", "name": "x" * 61},
                     {"profile": "p", "name": "群", "add_owner": True, "owner_open_id": "wxid_bad"},
                     {"profile": "p", "name": "群", "add_owner": True, "owner_open_id": ""}]
            for data in cases:
                with self.assertRaises(ValueError):
                    self.manager.post("wizard", {"op": "create-target", **data})


if __name__ == "__main__":
    unittest.main()
