"""开机自启：恢复 running 绑定 + 管理台/watchdog 拉起 + 计划任务注册。"""
import json
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import autostart, manage
from agents.storage import MirrorStore


def make_config(root, talker="room"):
    return {"binding_id": 1, "source_account": "demo", "wechat_talker": talker,
            "wechat_chat_name": "源", "feishu_chat_id": "chat", "feishu_chat_name": "目标",
            "lark_profile": "p", "lark_app_id": "app", "data_dir": str(root),
            "sqlite_file": str(root / "state.db")}


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = MirrorStore(self.root / "state.db")
        self.store.initialize(make_config(self.root))
        self.store.set_control(1, "running")
        self.second = self.store.create_binding({**make_config(self.root, "room2"), "wechat_talker": "room2", "feishu_chat_id": "chat2"})["id"]
        self.store.set_control(self.second, "stopped")

    def test_restores_only_running_enabled_bindings(self):
        paused = self.store.create_binding({**make_config(self.root, "room3"), "wechat_talker": "room3", "feishu_chat_id": "chat3"})["id"]
        self.store.set_control(paused, "paused")
        calls = []
        restored, failed = autostart.restore_running_bindings(self.store, self.root / "config.json", start=calls.append)
        self.assertEqual(restored, [1])
        self.assertEqual(failed, {})
        self.assertEqual(calls, [1])
        self.assertIn("boot.restore", [a["action"] for a in self.store.actions()])

    def test_skips_archived_and_disabled_bindings(self):
        self.store.archive_binding(self.second)
        calls = []
        restored, failed = autostart.restore_running_bindings(self.store, self.root / "config.json", start=calls.append)
        self.assertEqual(calls, [1])

    def test_skips_worker_already_alive(self):
        self.store.acquire_lease(1, pid=4321)
        self.store.heartbeat(self.store.get_lease(1)["token"], 1)
        calls = []
        restored, failed = autostart.restore_running_bindings(self.store, self.root / "config.json", start=calls.append)
        self.assertEqual(calls, [])

    def test_failure_is_recorded_and_does_not_stop_others(self):
        third = self.store.create_binding({**make_config(self.root, "room4"), "wechat_talker": "room4", "feishu_chat_id": "chat4"})["id"]
        self.store.set_control(third, "running")

        def flaky(bid):
            if bid == 1:
                raise RuntimeError("Worker 启动失败，请查看绑定日志")
        restored, failed = autostart.restore_running_bindings(self.store, self.root / "config.json", start=flaky)
        self.assertEqual(restored, [third])
        self.assertIn(1, failed)
        actions = [(a["action"], a["result"]) for a in self.store.actions()]
        self.assertIn(("boot.restore", "failed"), actions)
        self.assertIn(("boot.restore", "accepted"), actions)

    def test_default_start_invokes_mirror_cli_with_boot_reason(self):
        with patch.object(manage, "run_cli", return_value={}) as run:
            autostart.restore_running_bindings(self.store, self.root / "config.json")
        argv = run.call_args.args[0]
        self.assertIn("agents.wechat_feishu_mirror", argv)
        self.assertIn("start", argv)
        self.assertEqual(argv[argv.index("--start-reason") + 1], "boot restore")


class BootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = MirrorStore(self.root / "state.db")
        self.store.initialize(make_config(self.root))
        self.store.set_control(1, "running")
        self.config_path = self.root / "config.json"
        self.db_path = self.store.path

    def test_boot_restores_bindings_and_spawns_console_watchdog(self):
        started, spawned = [], []
        summary = autostart.run_boot(self.store, self.config_path, self.db_path,
                                     start=started.append, spawn=lambda args, log: spawned.append(args))
        self.assertEqual(started, [1])
        joined = [" ".join(map(str, args)) for args in spawned]
        self.assertEqual(len(spawned), 2)
        self.assertTrue(any("serve" in a for a in joined))
        self.assertTrue(any("watchdog" in a for a in joined))
        self.assertEqual(summary["restored"], [1])
        self.assertEqual(summary["console"], "spawned")

    def test_boot_skips_console_when_session_alive(self):
        session = self.store.path.parent / "manager-session.json"
        session.write_text(json.dumps({"port": 54321, "pid": 1, "token": "t", "url": "u"}), encoding="utf-8")
        spawned = []
        summary = autostart.run_boot(self.store, self.config_path, self.db_path,
                                     start=lambda bid: None, spawn=lambda args, log: spawned.append(args),
                                     console_check=lambda state: True)
        joined = [" ".join(map(str, args)) for args in spawned]
        self.assertFalse(any("serve" in a for a in joined))
        self.assertTrue(any("watchdog" in a for a in joined))
        self.assertEqual(summary["console"], "already-running")

    def test_boot_spawns_even_when_restore_fails(self):
        spawned = []
        summary = autostart.run_boot(self.store, self.config_path, self.db_path,
                                     start=lambda bid: (_ for _ in ()).throw(RuntimeError("读取器不可用")),
                                     spawn=lambda args, log: spawned.append(args))
        self.assertEqual(len(spawned), 2)
        self.assertEqual(len(summary["failed"]), 1)

    def test_boot_writes_autostart_log(self):
        autostart.run_boot(self.store, self.config_path, self.db_path,
                           start=lambda bid: None, spawn=lambda args, log: None)
        log = self.root / "logs" / "autostart.log"
        self.assertTrue(log.exists())
        self.assertIn("restored", log.read_text(encoding="utf-8"))

    def test_boot_survives_restore_crash_and_still_spawns(self):
        spawned = []
        with patch.object(autostart, "restore_running_bindings", side_effect=RuntimeError("database is locked")):
            summary = autostart.run_boot(self.store, self.config_path, self.db_path,
                                         spawn=lambda args, log: spawned.append(args))
        self.assertEqual(len(spawned), 2)
        self.assertIn("database is locked", summary["error"])

    def test_boot_survives_console_probe_crash(self):
        spawned = []
        def exploding_probe(state):
            raise RuntimeError("BadStatusLine")
        summary = autostart.run_boot(self.store, self.config_path, self.db_path,
                                     start=lambda bid: None, spawn=lambda args, log: spawned.append(args),
                                     console_check=exploding_probe)
        self.assertEqual(len(spawned), 2)
        self.assertEqual(summary["console"], "spawned")


class TaskCommandTest(unittest.TestCase):
    def test_boot_command_uses_windowless_python_and_autostart(self):
        cmd = autostart.boot_command(Path("C:/cfg.json"), Path("C:/data/state.db"))
        self.assertTrue(cmd["execute"].endswith("pythonw.exe") or cmd["execute"].endswith("python.exe"))
        self.assertIn("autostart", cmd["argument"])
        self.assertIn("--config", cmd["argument"])
        self.assertIn("--db", cmd["argument"])
        self.assertEqual(cmd["working_directory"], str(autostart.ROOT))

    def test_task_register_builds_logon_task(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='{"registered": true}', stderr="")

        with patch.object(autostart, "subprocess") as fake:
            fake.run.side_effect = fake_run
            fake.CompletedProcess = subprocess.CompletedProcess
            result = autostart.task_register(Path("C:/cfg.json"), Path("C:/data/state.db"))
        self.assertTrue(result["registered"])
        script = calls[0][-1]
        self.assertIn(autostart.TASK_NAME, script)
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("AtLogOn", script)
        self.assertIn("WorkingDirectory", script)
        self.assertIn("whoami", script)

    def test_task_unregister_is_idempotent(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='{"registered": false}', stderr="")

        with patch.object(autostart, "subprocess") as fake:
            fake.run.side_effect = fake_run
            fake.CompletedProcess = subprocess.CompletedProcess
            result = autostart.task_unregister()
        self.assertIn("Unregister-ScheduledTask", calls[0][-1])
        self.assertFalse(result["registered"])

    def test_task_unregister_failure_is_not_swallowed(self):
        with patch.object(autostart, "subprocess") as fake:
            fake.run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="Access is denied")
            with self.assertRaises(RuntimeError):
                autostart.task_unregister()

    def test_unregister_and_status_scripts_surface_service_failure(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout='{"registered": false}', stderr="")

        with patch.object(autostart, "subprocess") as fake:
            fake.run.side_effect = fake_run
            fake.CompletedProcess = subprocess.CompletedProcess
            autostart.task_unregister()
            autostart.task_status()
        for script in calls:
            self.assertIn("Get-Service Schedule", script)
        self.assertIn("MSFT_ScheduledTask", calls[0])

    def test_console_session_reads_running_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manager-session.json").write_text(json.dumps({"url": "http://127.0.0.1:59792/#token=t", "port": 59792}), encoding="utf-8")
            store = type("S", (), {"path": root / "state.db"})()
            self.assertEqual(autostart.console_session(store)["url"], "http://127.0.0.1:59792/#token=t")

    def test_task_unregister_reports_leftover_task(self):
        with patch.object(autostart, "subprocess") as fake:
            fake.run.return_value = subprocess.CompletedProcess([], 0, stdout='{"registered": true}', stderr="")
            self.assertTrue(autostart.task_unregister()["registered"])

    def test_task_status_parses_query(self):
        with patch.object(autostart, "subprocess") as fake:
            fake.run.return_value = subprocess.CompletedProcess([], 0, stdout='{"registered": true, "state": "Ready"}', stderr="")
            result = autostart.task_status()
        self.assertTrue(result["registered"])
        self.assertEqual(result["state"], "Ready")

    def test_non_windows_register_is_rejected(self):
        with patch.object(autostart.os, "name", "posix"):
            with self.assertRaises(ValueError):
                autostart.task_register(Path("c.json"), Path("s.db"))
            self.assertFalse(autostart.task_status()["supported"])


class ManagerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = MirrorStore(self.root / "state.db")
        self.store.initialize(make_config(self.root))

    def test_get_autostart_reports_status(self):
        manager = manage.Manager(self.store, self.root / "config.json")
        with patch.object(autostart, "task_status", return_value={"registered": True, "state": "Ready", "supported": True}):
            status = manager.get("autostart")
        self.assertEqual(status["task"], autostart.TASK_NAME)
        self.assertTrue(status["registered"])

    def test_post_register_and_unregister(self):
        manager = manage.Manager(self.store, self.root / "config.json")
        with patch.object(autostart, "task_register", return_value={"registered": True}) as reg:
            manager.post("autostart", {"op": "register"})
        reg.assert_called_once()
        with patch.object(autostart, "task_unregister", return_value={"registered": False}) as unreg:
            manager.post("autostart", {"op": "unregister"})
        unreg.assert_called_once()
        self.assertIn("autostart.register", [a["action"] for a in self.store.actions()])

    def test_post_autostart_rejects_unknown_op(self):
        manager = manage.Manager(self.store, self.root / "config.json")
        with self.assertRaises(ValueError):
            manager.post("autostart", {"op": "restart"})

    def test_status_cache_pays_powershell_once_within_ttl(self):
        manager = manage.Manager(self.store, self.root / "config.json")
        with patch.object(autostart, "task_status", return_value={"registered": False, "supported": True}) as query:
            first = manager.autostart_status()
            second = manager.autostart_status()
        self.assertEqual(query.call_count, 1)
        self.assertEqual(first, second)

    def test_status_cache_contended_returns_value_not_tuple(self):
        manager = manage.Manager(self.store, self.root / "config.json")
        value = {"registered": True, "supported": True}
        manager._autostart_cache = (time.time() - 999, value)
        self.assertTrue(manager._autostart_refresh.acquire(blocking=False))
        try:
            self.assertEqual(manager.autostart_status(), value)
        finally:
            manager._autostart_refresh.release()

    def test_post_populates_cache_for_following_reads(self):
        manager = manage.Manager(self.store, self.root / "config.json")
        with patch.object(autostart, "task_register", return_value={"registered": True, "supported": True}):
            manager.post("autostart", {"op": "register"})
        with patch.object(autostart, "task_status", side_effect=AssertionError("should serve cache")):
            self.assertTrue(manager.autostart_status()["registered"])

    def test_binding_restorable_is_shared_rule(self):
        binding = self.store.list_bindings()[0]
        self.assertFalse(manage.binding_restorable(self.store, binding))
        self.store.set_control(binding["id"], "running")
        self.assertTrue(manage.binding_restorable(self.store, binding))


class WatchdogSingletonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_singleton_lock_is_exclusive(self):
        first = autostart.singleton_lock(self.root / "locks" / "watchdog.lock")
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(autostart.singleton_lock(self.root / "locks" / "watchdog.lock"))
        finally:
            first.close()

    def test_singleton_lock_released_on_close(self):
        first = autostart.singleton_lock(self.root / "locks" / "watchdog.lock")
        first.close()
        self.assertIsNotNone(autostart.singleton_lock(self.root / "locks" / "watchdog.lock"))


if __name__ == "__main__":
    unittest.main()
