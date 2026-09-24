"""验收指标报表：时延分位数、媒体成功率、滞留与守护动作统计。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import manage
from agents.metrics import build_report, percentile
from agents.storage import MirrorStore


def make_config(root, talker="room"):
    return {"binding_id": 1, "source_account": "demo", "wechat_talker": talker,
            "wechat_chat_name": "源", "feishu_chat_id": "chat", "feishu_chat_name": "目标",
            "lark_profile": "p", "lark_app_id": "app", "data_dir": str(root),
            "sqlite_file": str(root / "state.db")}


def seed_message(store, key, *, binding_id=1, kind="text", state="sent", create_time=1000,
                 updated_at=1003.0, local_id=1, payload=None):
    row = {"talker": "room", "local_id": local_id, "kind_name": kind,
           "sender_wxid": "wx", "sender": "某人", "text": "hi",
           "create_time": create_time}
    if payload is not None:
        row = {**row, **payload}
    store.upsert_message(key, row, kind, binding_id=binding_id, source_account="demo")
    if state != "received":
        store.mark_message(key, state, binding_id=binding_id)
    with store.connect() as db:
        # 窗口过滤读 created_at（入库时间）；测试里让它跟随 updated_at 以便构造旧数据。
        db.execute("UPDATE messages SET created_at=?, updated_at=?, local_id=? WHERE wechat_message_key=? AND binding_id=?",
                   (updated_at, updated_at, local_id, key, binding_id))


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(percentile([1, 2, 3, 4, 5], 95), 5)
        self.assertEqual(percentile([1, 2, 3, 4, 5], 100), 5)
        self.assertEqual(percentile([4, 1, 3, 2], 50), 2)

    def test_empty_is_none(self):
        self.assertIsNone(percentile([], 50))


class BuildReportTest(unittest.TestCase):
    def test_latency_percentiles_split_by_text_and_media(self):
        rows = {"bindings": {1: {"latency": [
            ("text", 1.0), ("text", 3.0), ("text", 5.0),
            ("image", 9.0)], "excluded": 0}}}
        report = build_report(rows, window_days=7)
        latency = report["bindings"][0]["latency"]
        self.assertEqual(latency["text"]["n"], 3)
        self.assertEqual(latency["text"]["p50"], 3.0)
        self.assertEqual(latency["text"]["p95"], 5.0)
        self.assertEqual(latency["text"]["max"], 5.0)
        self.assertEqual(latency["media"]["n"], 1)
        self.assertEqual(latency["media"]["p95"], 9.0)

    def test_media_success_rate_excludes_in_flight(self):
        rows = {"bindings": {1: {"media_states": {"uploaded": 9, "placeholder": 1, "pending": 4, "processing": 2}}}}
        media = build_report(rows, window_days=7)["bindings"][0]["media_delivery"]
        self.assertEqual(media["uploaded"], 9)
        self.assertEqual(media["success_rate"], 0.9)
        self.assertEqual(media["in_flight"], 6)

    def test_stuck_and_guard_counts(self):
        rows = {"bindings": {1: {"stuck_states": {"received": 1, "processing": 2, "failed": 3},
                                 "actions": [("watchdog.restart", "watchdog"), ("watchdog.restart", "watchdog"),
                                             ("bindings.start", "web")]}}}
        binding = build_report(rows, window_days=7)["bindings"][0]
        self.assertEqual(binding["stuck"], {"received": 1, "processing": 2, "failed": 3})
        self.assertEqual(binding["guard"]["watchdog.restart"], 2)
        self.assertEqual(binding["guard"]["bindings.start"], {"web": 1})

    def test_empty_input_keeps_shape(self):
        report = build_report({"bindings": {}, "cursor": {}}, window_days=30)
        self.assertEqual(report["window_days"], 30)
        self.assertEqual(report["bindings"], [])
        self.assertEqual(report["totals"]["latency"]["text"]["n"], 0)
        self.assertIsNone(report["totals"]["latency"]["text"]["p50"])
        self.assertIsNone(report["totals"]["media_delivery"]["success_rate"])

    def test_totals_aggregate_all_bindings(self):
        rows = {"bindings": {1: {"latency": [("text", 2.0)], "media_states": {"uploaded": 1}},
                             2: {"latency": [("text", 6.0)], "media_states": {"failed": 1}}}}
        totals = build_report(rows, window_days=7)["totals"]
        self.assertEqual(totals["latency"]["text"]["n"], 2)
        self.assertEqual(totals["media_delivery"]["success_rate"], 0.5)


class StorageMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = MirrorStore(self.root / "state.db")
        self.store.initialize(make_config(self.root))
        self.second = self.store.create_binding({**make_config(self.root, "room2"), "wechat_talker": "room2", "feishu_chat_id": "chat2"})["id"]

    def test_metric_rows_collect_latency_media_stuck_actions(self):
        seed_message(self.store, "k1", kind="text", state="sent", create_time=1000, updated_at=1003.0, local_id=5)
        seed_message(self.store, "k2", kind="image", state="sent", create_time=2000, updated_at=2011.0, local_id=6)
        seed_message(self.store, "k3", kind="text", state="failed", create_time=3000, updated_at=3001.0, local_id=7)
        seed_message(self.store, "k4", kind="text", state="received", create_time=4000, updated_at=4000.0, local_id=8, payload={"create_time": 0})
        seed_message(self.store, "k5", kind="text", state="sent", create_time=1000, updated_at=1003.0, local_id=9, binding_id=self.second)
        self.store.audit("watchdog.restart", actor="watchdog", binding_id=1)
        self.store.audit("bindings.start", actor="web", binding_id=1)
        self.store.set_cursor(9, self.second)
        rows = self.store.metric_rows(since=0)
        self.assertIn(1, rows["bindings"])
        self.assertEqual(len(rows["bindings"][1]["latency"]), 2)
        self.assertEqual(rows["cursor"][self.second], 9)
        report = build_report(rows, window_days=7)
        by_id = {b["binding_id"]: b for b in report["bindings"]}
        self.assertEqual(by_id[1]["stuck"]["failed"], 1)
        self.assertEqual(by_id[1]["guard"]["watchdog.restart"], 1)
        self.assertEqual(by_id[self.second]["cursor"], 9)

    def test_metric_rows_window_and_binding_filter(self):
        seed_message(self.store, "k1", state="sent", create_time=1000, updated_at=1003.0)
        seed_message(self.store, "k2", state="sent", create_time=5000, updated_at=5003.0)
        rows = self.store.metric_rows(since=4000, binding_id=1)
        self.assertEqual(rows["bindings"][1]["latency"], [("text", 3.0)])


class ManagerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = MirrorStore(self.root / "state.db")
        self.store.initialize(make_config(self.root))

    def test_get_metrics_returns_report(self):
        manager = manage.Manager(self.store, self.root / "config.json")
        report = manager.get("metrics", {"days": "30"})
        self.assertEqual(report["window_days"], 30)
        self.assertIn("bindings", report)

    def test_get_metrics_rejects_bad_days(self):
        manager = manage.Manager(self.store, self.root / "config.json")
        with self.assertRaises(ValueError):
            manager.get("metrics", {"days": "zero"})


if __name__ == "__main__":
    unittest.main()
