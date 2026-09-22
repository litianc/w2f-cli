"""wda_reader.cmd_media：原图优先与缩略图降级标记。

微信桌面端本地缓存通常只有缩略图；适配层先带 fetch_remote 让 WDA 拉 CDN
原图，失败才回落缩略图并标记 thumbnail_only，供 worker 在飞书侧提示。
全部网络调用打桩，不依赖运行中的 WDA 服务。
"""
import argparse
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "wda_reader", Path(__file__).resolve().parents[1] / "integrations" / "wda_reader.py")
wda = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wda)


def media_args():
    return argparse.Namespace(talker="room@chatroom", local_id=1, server_id="")


class WdaMediaOriginalFirst(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.message = {"type": 3, "localId": 1, "imageMd5": "abc123"}

    def result(self, fetch_side_effects):
        with patch.dict(os.environ, {"W2F_WDA_CACHE": str(self.cache)}):
            with patch.object(wda, "find_message", return_value=self.message) as find, \
                 patch.object(wda, "fetch_media", side_effect=fetch_side_effects) as fetch:
                payload = wda.cmd_media(media_args())
        self.assertEqual(find.call_args.args, ("room@chatroom", 1))
        return payload, fetch

    def test_original_fetched_from_cdn(self):
        payload, fetch = self.result([(b"original-jpeg-bytes", "image/jpeg")])
        resource = payload["data"]["resources"][0]
        path = Path(resource["local_paths"][0])
        self.assertTrue(path.name.startswith("abc123.orig."))
        self.assertEqual(path.read_bytes(), b"original-jpeg-bytes")
        self.assertNotIn("thumbnail_only", resource)
        self.assertIn("fetch_remote", str(fetch.call_args))

    def test_missing_original_falls_back_to_flagged_thumbnail(self):
        payload, fetch = self.result([None, (b"thumb-bytes", "image/jpeg")])
        resource = payload["data"]["resources"][0]
        self.assertTrue(Path(resource["local_paths"][0]).name.startswith("abc123.thumb."))
        self.assertTrue(resource["thumbnail_only"])
        self.assertEqual(fetch.call_count, 2)

    def test_cached_original_serves_without_refetch(self):
        (self.cache / "abc123.orig.jpg").write_bytes(b"original-jpeg-bytes")
        def must_not_fetch(*args, **kwargs):
            raise AssertionError("原图已缓存时不应再请求 WDA")
        payload, _ = self.result([must_not_fetch])
        resource = payload["data"]["resources"][0]
        self.assertEqual(Path(resource["local_paths"][0]).read_bytes(), b"original-jpeg-bytes")
        self.assertNotIn("thumbnail_only", resource)

    def test_cached_thumbnail_still_reports_degradation(self):
        (self.cache / "abc123.thumb.jpg").write_bytes(b"thumb-bytes")
        payload, _ = self.result([None, None])
        resource = payload["data"]["resources"][0]
        self.assertTrue(Path(resource["local_paths"][0]).name.startswith("abc123.thumb."))
        self.assertTrue(resource["thumbnail_only"])


if __name__ == "__main__":
    unittest.main()
