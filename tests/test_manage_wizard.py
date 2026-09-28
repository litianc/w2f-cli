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

    def test_multiple_initial_members_are_validated_deduplicated_and_verified(self):
        profile = self.store.list_profiles()[0]
        for uid in ['ou_first', 'ou_second']:
            person = self.store.create_person('同名人员')
            self.store.link_feishu_identity(person['id'], profile['id'], uid)
        def fake(args, **kwargs):
            if '+chat-create' in args:
                self.assertEqual(args[args.index('--users') + 1], 'ou_first,ou_second')
                return {'data': {'chat_id': 'oc_new'}}
            if '+chat-members-list' in args:
                return {'data': {'users': [{'member_id': 'ou_first'}, {'member_id': 'ou_second'}]}}
            return {'data': {'share_link': 'https://example.invalid/join'}}
        with patch.object(manage, 'run_cli', side_effect=fake) as run:
            result = self.manager.post('wizard', {'op': 'create-target', 'profile': 'p', 'name': '新群',
                'member_open_ids': ['ou_first', 'ou_second', 'ou_first'], 'request_id': 'qa-multiple'})
        self.assertEqual(result['members']['joined'], ['ou_first', 'ou_second'])
        self.assertEqual(result['members']['status'], 'verified')
        self.assertEqual(sum('+chat-create' in call.args[0] for call in run.call_args_list), 1)

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
                                                          {"data": {"users": [{"member_id": "ou_me"}]}},
                                                          {"data": {"share_link": "https://applink.feishu.cn/join", "expire_time": "123"}}]) as run:
            result = self.manager.post("wizard", {"op": "create-target", "profile": "p", "name": "新群",
                                                   "add_owner": True, "owner_open_id": "ou_me"})
        self.assertEqual(result["share_link"], "https://applink.feishu.cn/join")
        self.assertEqual(result["expire_time"], "123")
        self.assertIn("chats", run.call_args_list[2].args[0])
        self.assertIn("link", run.call_args_list[2].args[0])
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



class WizardMultipleMembers(unittest.TestCase):
    setUp = WizardCreateTarget.setUp

    def linked(self, uid, profile=None):
        person = self.store.create_person('同名')
        self.store.link_feishu_identity(person['id'], (profile or self.store.list_profiles()[0])['id'], uid)
        return person

    def request(self, members, request_id='qa-request'):
        return {'op': 'create-target', 'profile': 'p', 'name': '测试群',
                'member_open_ids': members, 'request_id': request_id}

    def test_invalid_member_inputs_never_create_remote_group(self):
        person = self.linked('ou_stale')
        self.store.unlink_identity('feishu', self.store.list_feishu_identities(person['id'])[0]['id'], person['id'])
        other = self.store.upsert_profile('other', 'app_other')
        self.linked('ou_other', other)
        cases = [None, '', [''], [' '], [None], ['wxid_wrong'], ['ou_unknown'], ['ou_other'], ['ou_stale'], ['ou_x,ou_y']]
        with patch.object(manage, 'run_cli') as run:
            for members in cases:
                with self.subTest(members=members), self.assertRaises(ValueError):
                    self.manager.post('wizard', self.request(members))
            run.assert_not_called()

    def test_50_members_and_60_emoji_names_are_not_silently_truncated(self):
        ids = ['ou_member_' + str(i) for i in range(50)]
        for uid in ids: self.linked(uid)
        data = {**self.request(ids), 'name': '😀' * 60}
        def fake(args, **kwargs):
            if '+chat-create' in args:
                self.assertEqual(args[args.index('--users') + 1], ','.join(ids))
                self.assertEqual(args[args.index('--name') + 1], data['name'])
                return {'data': {'chat_id': 'oc_limit'}}
            if '+chat-members-list' in args:
                return {'data': {'users': [{'member_id': uid} for uid in ids]}}
            return {'data': {}}
        with patch.object(manage, 'run_cli', side_effect=fake):
            result = self.manager.post('wizard', data)
        self.assertEqual(result['members']['joined'], ids)

    def test_missing_or_malformed_remote_receipt_stays_uncertain(self):
        for index, payload in enumerate([{}, {'data': 'unexpected'}, {'ok': True, 'data': {}}]):
            data = self.request([], 'qa-malformed-' + str(index))
            with patch.object(manage, 'run_cli', return_value=payload) as run:
                result = self.manager.post('wizard', data)
                replay = self.manager.post('wizard', data)
            self.assertEqual(result, replay)
            self.assertEqual(result['creation_status'], 'uncertain')
            self.assertEqual(run.call_count, 1)

    def test_more_than_50_unique_members_is_rejected_without_truncation(self):
        with patch.object(manage, 'run_cli') as run, self.assertRaisesRegex(ValueError, '50'):
            self.manager.post('wizard', self.request(['ou_' + str(i) for i in range(51)]))
        run.assert_not_called()

    def test_partial_members_preserve_created_chat_and_replay_without_creation(self):
        self.linked('ou_yes');self.linked('ou_no')
        data = self.request(['ou_yes', 'ou_no'])
        with patch.object(manage, 'run_cli', side_effect=[{'data': {'chat_id': 'oc_partial'}},
                {'data': {'users': [{'member_id': 'ou_yes'}]}}, RuntimeError('无邀请链接权限')]) as run:
            result = self.manager.post('wizard', data)
        self.assertEqual(result['members']['status'], 'partial')
        self.assertEqual(result['members']['missing'], ['ou_no'])
        self.assertEqual(result['chat_id'], 'oc_partial')
        with patch.object(manage, 'run_cli', side_effect=AssertionError('duplicate remote call')):
            restarted = manage.Manager(MirrorStore(self.store.path), self.manager.config_path)
            self.assertEqual(restarted.post('wizard', data), result)
        self.assertEqual(sum('+chat-create' in c.args[0] for c in run.call_args_list), 1)

    def test_incomplete_member_list_keeps_missing_users_pending(self):
        self.linked('ou_member')
        with patch.object(manage, 'run_cli', side_effect=[{'data': {'chat_id': 'oc_pending'}},
                {'data': {'users': [], 'truncations': ['users']}}, {'data': {}}]):
            result = self.manager.post('wizard', self.request(['ou_member']))
        self.assertEqual(result['members']['status'], 'pending')
        self.assertEqual(result['members']['pending'], ['ou_member'])
        self.assertEqual(result['members']['missing'], [])

    def test_member_read_failure_does_not_hide_created_group(self):
        self.linked('ou_member')
        with patch.object(manage, 'run_cli', side_effect=[{'data': {'chat_id': 'oc_pending'}},
                RuntimeError('成员读取权限不足'), {'data': {'share_link': 'https://example.invalid/join'}}]):
            result = self.manager.post('wizard', self.request(['ou_member']))
        self.assertEqual(result['creation_status'], 'created')
        self.assertEqual(result['members']['status'], 'pending')
        self.assertIn('权限', result['members']['error'])
        self.assertTrue(result['share_link'])

    def test_uncertain_create_survives_restart_and_cannot_blindly_retry(self):
        data = self.request([])
        with patch.object(manage, 'run_cli', side_effect=manage.CliRequestError('timeout')) as run:
            result = self.manager.post('wizard', data)
        self.assertEqual(result['creation_status'], 'uncertain')
        self.assertIsNone(result['chat_id'])
        with patch.object(manage, 'run_cli', side_effect=AssertionError('unsafe retry')):
            self.assertEqual(manage.Manager(MirrorStore(self.store.path), self.manager.config_path).post('wizard', data), result)
        self.assertEqual(run.call_count, 1)

    def test_definite_failure_keeps_draft_and_allows_explicit_retry(self):
        data = self.request([])
        with patch.object(manage, 'run_cli', side_effect=manage.CliRequestError('权限未开通', uncertain=False)):
            result = self.manager.post('wizard', data)
        self.assertEqual(result['creation_status'], 'failed')
        self.assertEqual(result['name'], data['name'])
        with patch.object(manage, 'run_cli', side_effect=[{'data': {'chat_id': 'oc_retry'}}, {'data': {}}]):
            result = self.manager.post('wizard', data)
        self.assertEqual(result['chat_id'], 'oc_retry')

    def test_concurrent_request_is_claimed_before_calling_cli(self):
        data = self.request([])
        def nested(args, **kwargs):
            if '+chat-create' in args:
                result = manage.Manager(MirrorStore(self.store.path), self.manager.config_path).post('wizard', data)
                self.assertEqual(result['creation_status'], 'uncertain')
                return {'data': {'chat_id': 'oc_once'}}
            return {'data': {}}
        with patch.object(manage, 'run_cli', side_effect=nested) as run:
            result = self.manager.post('wizard', data)
        self.assertEqual(result['chat_id'], 'oc_once')
        self.assertEqual(sum('+chat-create' in c.args[0] for c in run.call_args_list), 1)

    def test_request_cannot_be_reused_for_different_input(self):
        data = self.request([])
        with patch.object(manage, 'run_cli', return_value={'data': {'chat_id': 'oc_once'}}):
            self.manager.post('wizard', data)
        with patch.object(manage, 'run_cli') as run, self.assertRaisesRegex(ValueError, '内容已改变'):
            self.manager.post('wizard', {**data, 'name': '另一个群'})
        run.assert_not_called()

if __name__ == "__main__":
    unittest.main()
