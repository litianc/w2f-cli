"""wda_reader：@提示的文本保真（P1 文本形式；真 @ 飞书用户留待 3a）。"""
import importlib.util
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "wda_reader", Path(__file__).resolve().parents[1] / "integrations" / "wda_reader.py")
wda = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wda)


def row_of(content, ats, names=None):
    message = {"type": 1, "localId": 1, "content": content, "atUsernames": ats}
    return wda.map_row("room@chatroom", message, names or {})


class MentionAnnotation(unittest.TestCase):
    def test_missing_mention_is_prepended(self):
        row = row_of("你好", ["wxid_abc"], {"wxid_abc": "张三"})
        self.assertEqual(row["kind_name"], "text")
        self.assertEqual(row["text"], "@张三 你好")

    def test_inline_mention_is_left_alone(self):
        row = row_of("@张三 你好", ["wxid_abc"], {"wxid_abc": "张三"})
        self.assertEqual(row["text"], "@张三 你好")

    def test_multiple_mentions_all_prepended(self):
        row = row_of("开会", ["wxid_a", "wxid_b"], {"wxid_a": "张三", "wxid_b": "李四"})
        self.assertEqual(row["text"], "@张三 @李四 开会")

    def test_unknown_username_falls_back_to_raw_id(self):
        row = row_of("在吗", ["wxid_unknown"])
        self.assertEqual(row["text"], "@wxid_unknown 在吗")

    def test_all_member_mention(self):
        row = row_of("注意", ["announceall"])
        self.assertEqual(row["text"], "@全体成员 注意")

    def test_no_mentions_is_untouched(self):
        row = row_of("普通消息", [])
        self.assertEqual(row["text"], "普通消息")

    def test_mention_only_message(self):
        row = row_of("", ["wxid_abc"], {"wxid_abc": "张三"})
        self.assertEqual(row["text"], "@张三")


if __name__ == "__main__":
    unittest.main()
