"""wda_reader contacts / members：管理台（manage.py）已依赖的 reader CLI 契约，Windows 适配器补齐。

契约来源（消费方 agents/manage.py）：
- contacts --groups-only / --friends-only --limit N -> {"ok":true,"data":{"contacts":[...]}}
  行字段：username / display_name / remark / nick_name / alias
- members --chatroom-id <群ID> 与位置参数等价（manage.py 用旗标形式调用）
WDA HTTP 边界全部 mock；不触网。
"""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "wda_reader", Path(__file__).resolve().parents[1] / "integrations" / "wda_reader.py")
wda = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wda)

GROUP = {"username": "fixtureroom01@chatroom", "displayName": "示例群", "remark": "",
         "nickname": "示例群昵称", "alias": "lx", "type": "group"}
FRIEND = {"username": "wxid_abc", "displayName": "张三", "remark": "三儿",
          "nickname": "小张", "alias": "zs", "type": "friend"}


def wda_contacts(rows):
    return {"status": "success", "contacts": rows, "total": len(rows)}


def cli(argv):
    args = wda.build_parser().parse_args(argv)
    return args.handler(args)


class ContactsGroupsOnly(unittest.TestCase):
    def test_maps_contract_fields_and_filters_to_groups(self):
        with patch.object(wda, "api_get", return_value=wda_contacts([GROUP, FRIEND])) as get:
            result = cli(["contacts", "--groups-only", "--limit", "100000"])
        self.assertEqual(get.call_args.args[0], "/api/chat/contacts")
        self.assertEqual(get.call_args.kwargs.get("include_groups"), "true")
        self.assertEqual(get.call_args.kwargs.get("include_friends"), "false")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["data"]["contacts"], [
            {"username": "fixtureroom01@chatroom", "display_name": "示例群",
             "remark": "", "nick_name": "示例群昵称", "alias": "lx"}])

    def test_officials_never_leak_into_groups(self):
        official = {"username": "gh_official", "displayName": "某公众号", "type": "official"}
        with patch.object(wda, "api_get", return_value=wda_contacts([GROUP, official])):
            result = cli(["contacts", "--groups-only", "--limit", "100000"])
        self.assertEqual([c["username"] for c in result["data"]["contacts"]],
                         ["fixtureroom01@chatroom"])


class ContactsFriendsOnly(unittest.TestCase):
    def test_excludes_groups_and_maps_names(self):
        with patch.object(wda, "api_get", return_value=wda_contacts([GROUP, FRIEND])) as get:
            result = cli(["contacts", "--friends-only", "--limit", "100000"])
        self.assertEqual(get.call_args.kwargs.get("include_friends"), "true")
        self.assertEqual(get.call_args.kwargs.get("include_groups"), "false")
        self.assertEqual(result["data"]["contacts"], [
            {"username": "wxid_abc", "display_name": "张三",
             "remark": "三儿", "nick_name": "小张", "alias": "zs"}])

    def test_display_name_falls_back_through_remark_nickname_alias_username(self):
        bare = {"username": "wxid_bare", "displayName": "", "remark": "",
                "nickname": "", "alias": "备用", "type": "friend"}
        with patch.object(wda, "api_get", return_value=wda_contacts([bare])):
            result = cli(["contacts", "--friends-only", "--limit", "100000"])
        self.assertEqual(result["data"]["contacts"][0]["display_name"], "备用")

    def test_limit_truncates(self):
        rows = [dict(FRIEND, username=f"wxid_{i}") for i in range(5)]
        with patch.object(wda, "api_get", return_value=wda_contacts(rows)):
            result = cli(["contacts", "--friends-only", "--limit", "2"])
        self.assertEqual(len(result["data"]["contacts"]), 2)


class MembersChatroomFlag(unittest.TestCase):
    def test_chatroom_id_flag_matches_positional_contract(self):
        payload = {"members": [{"username": "wxid_a"}], "total": 1, "username": "room@chatroom"}
        with patch.object(wda, "api_get", return_value=payload) as get:
            result = cli(["members", "--chatroom-id", "room@chatroom", "--limit", "50"])
        self.assertEqual(get.call_args.args[0], "/api/chat/contacts/group_members")
        self.assertEqual(get.call_args.kwargs.get("username"), "room@chatroom")
        self.assertEqual(result["data"]["chatroom_id"], "room@chatroom")

    def test_positional_talker_still_works(self):
        payload = {"members": [{"username": "wxid_a"}], "total": 1, "username": "room@chatroom"}
        with patch.object(wda, "api_get", return_value=payload) as get:
            result = cli(["members", "room@chatroom"])
        self.assertEqual(get.call_args.kwargs.get("username"), "room@chatroom")
        self.assertEqual(result["data"]["chatroom_id"], "room@chatroom")


if __name__ == "__main__":
    unittest.main()
