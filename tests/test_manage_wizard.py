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
            return {"data": {"chat_id": "oc_new", "link": "https://example.test/join"}}
        with patch.object(manage, "run_cli", side_effect=fake_run):
            result = self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "新群",
                                                  "add_owner": True, "owner_open_id": "ou_me"})
        self.assertEqual(result["chat_id"], "oc_new")
        self.assertEqual(result["chat_link"], "https://example.test/join")
        self.assertIn("+chat-create", calls[0])
        self.assertIn("ou_me", calls[0])
        link_call = next(c for c in calls if "/im/v1/chats/" in " ".join(c))
        self.assertIn("POST", link_call)
        self.assertIn("/open-apis/im/v1/chats/oc_new/link", " ".join(link_call))
        self.assertIn("bot", link_call)

    def test_link_failure_degrades_without_blocking_create(self):
        def fake_run(args, **kwargs):
            if "+chat-create" in args:
                return {"data": {"chat_id": "oc_new"}}
            raise RuntimeError("PermissionError: 应用未开通对应权限")
        with patch.object(manage, "run_cli", side_effect=fake_run):
            result = self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "新群"})
        self.assertEqual(result["chat_id"], "oc_new")
        self.assertIsNone(result["chat_link"])
        self.assertIn("权限", result["link_note"])

    def test_create_without_owner_omits_member_flag(self):
        with patch.object(manage, "run_cli", return_value={"data": {"chat_id": "oc_x"}}) as run:
            result = self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "群"})
        self.assertEqual(result["chat_id"], "oc_x")
        self.assertNotIn("--users", run.call_args.args[0])

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
