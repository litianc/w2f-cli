"""Watchdog：只拉起 running 且死亡的 Worker，退避 + 连续失败后放弃。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.manage import Watchdog
from agents.storage import MirrorStore


def make_config(root, talker="room"):
    return {"binding_id": 1, "source_account": "demo", "wechat_talker": talker,
            "wechat_chat_name": "源", "feishu_chat_id": "chat", "feishu_chat_name": "目标",
            "lark_profile": "p", "lark_app_id": "app", "data_dir": str(root),
            "sqlite_file": str(root / "state.db")}


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = MirrorStore(self.root / "state.db")
        self.store.initialize(make_config(self.root))
        self.store.set_control(1, "running")
        self.second = self.store.create_binding({**make_config(self.root, "room2"), "wechat_talker": "room2", "feishu_chat_id": "chat2"})["id"]
        self.store.set_control(self.second, "stopped")

    def test_restarts_dead_running_binding_and_annotates(self):
        calls = []
        dog = Watchdog(self.store, self.root / "config.json", restart=calls.append)
        self.assertEqual(dog.once(now=1000.0), [1])
        self.assertEqual(calls, [1])
        control = self.store.status(1)["control"]
        self.assertEqual(control["reason"], "watchdog restart")
        actions = [a["action"] for a in self.store.actions()]
        self.assertIn("watchdog.restart", actions)

    def test_healthy_worker_and_stopped_binding_are_untouched(self):
        self.store.acquire_lease(1, pid=4321)
        self.store.heartbeat(self.store.get_lease(1)["token"], 1)
        calls = []
        dog = Watchdog(self.store, self.root / "config.json", restart=calls.append)
        self.assertEqual(dog.once(now=1000.0), [])
        self.assertEqual(calls, [])

    def test_min_gap_suppresses_immediate_retry(self):
        calls = []
        dog = Watchdog(self.store, self.root / "config.json", restart=calls.append, min_gap=60.0)
        self.assertEqual(dog.once(now=1000.0), [1])
        self.assertEqual(dog.once(now=1030.0), [])
        self.assertEqual(dog.once(now=1061.0), [1])
        self.assertEqual(calls, [1, 1])

    def test_consecutive_failures_park_binding_as_stopped(self):
        def failing_restart(bid):
            raise RuntimeError("profile disabled")
        dog = Watchdog(self.store, self.root / "config.json", restart=failing_restart, max_fails=3)
        for i in range(3):
            dog.once(now=1000.0 + i * 61.0)
        control = self.store.status(1)["control"]
        self.assertEqual(control["status"], "stopped")
        self.assertIn("watchdog 放弃", control["reason"])
        actions = [a["action"] for a in self.store.actions()]
        self.assertIn("watchdog.give_up", actions)
        # Parked binding is no longer retried.
        self.assertEqual(dog.once(now=1200.0), [])

    def test_default_spawner_invokes_mirror_cli_start(self):
        """默认启动器走 mirror CLI 的 start 动作（真实拉起由进程锁保证幂等）。"""
        from unittest.mock import patch
        from agents import manage
        with patch.object(manage, "run_cli", return_value={}) as run:
            dog = manage.Watchdog(self.store, self.root / "config.json")
            self.assertEqual(dog.once(now=1000.0), [1])
        argv = run.call_args.args[0]
        self.assertIn("agents.wechat_feishu_mirror", argv)
        self.assertIn("start", argv)
        self.assertIn("--binding-id", argv)


if __name__ == "__main__":
    unittest.main()
