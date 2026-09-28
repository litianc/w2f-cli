"""Create one target group, retaining its receipt independently of member checks."""
import json
import re
import uuid


class CliRequestError(RuntimeError):
    def __init__(self, message, *, uncertain=True):
        super().__init__(message)
        self.uncertain = uncertain


def create_target(store, data, run_cli, share_link):
    profile_name = str(data.get('profile') or '')
    name = str(data.get('name') or '').strip()
    profile = next((p for p in store.list_profiles() if p['profile_name'] == profile_name and p['enabled']), None)
    if not profile:
        raise ValueError('请先选择已接入且启用的发送 Profile')
    if not name or len(name) > 60:
        raise ValueError('请填写 1 至 60 字的新群名称')
    if 'member_open_ids' in data and data.get('add_owner'):
        raise ValueError('请使用初始成员列表，不要同时提交旧版本人选项')
    members = data.get('member_open_ids', [data.get('owner_open_id')] if data.get('add_owner') else [])
    if not isinstance(members, list) or any(not isinstance(uid, str) or not re.fullmatch(r'ou_[A-Za-z0-9_-]+', uid) for uid in members):
        raise ValueError('初始成员必须是有效 open_id 列表；空 ID 不可提交，请核对当前 Profile 下的关联')
    members = list(dict.fromkeys(members))
    if len(members) > 50:
        raise ValueError('一次最多选择 50 名初始成员，请减少选择后重试')
    request_id = data.get('request_id') or uuid.uuid4().hex
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
        raise ValueError('建群请求编号无效')
    key = 'create_target:' + request_id
    inputs = {'app_id': profile['app_id'], 'profile': profile_name, 'name': name, 'members': members}
    result = {'request_id': request_id, 'profile': profile_name, 'name': name, 'chat_id': None,
              'creation_status': 'uncertain', 'creation_error': '建群结果待核实，请刷新已有群列表核对，勿重复建群。',
              'members': {'status': 'pending' if members else 'not_requested', 'requested': members,
                          'joined': [], 'missing': [], 'pending': members}}
    # Claim before calling the CLI, across HTTP handlers and manager restarts.
    # A crash/timeout leaves an uncertain receipt; it is never blindly replayed.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        if existing:
            saved = json.loads(existing[0])
            if saved['inputs'] != inputs:
                raise ValueError('同一建群请求的内容已改变，请先核对之前的创建结果')
            if saved['result']['creation_status'] != 'failed':
                return saved['result']
        linked = {i['open_id'] for i in store.list_feishu_identities() if i['profile_id'] == profile['id']}
        if any(uid not in linked for uid in members):
            raise ValueError('初始成员未关联到当前 Profile，或关联已失效；请到「人员与实名」核对后重新选择')
        db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, json.dumps({'inputs': inputs, 'result': result}, ensure_ascii=False)))

    def save():
        store.save_setting(key, {'inputs': inputs, 'result': result})

    args = ['lark-cli', '--profile', profile_name, 'im', '+chat-create', '--name', name, '--as', 'bot', '--format', 'json']
    if members:
        args += ['--users', ','.join(members)]
    try:
        payload = run_cli(args)
    except RuntimeError as exc:
        from .wechat_feishu_mirror import safe_error
        result['creation_status'] = 'uncertain' if getattr(exc, 'uncertain', True) else 'failed'
        result['creation_error'] = safe_error(exc)
        save()
        return result
    remote = (payload.get('data') or payload) if isinstance(payload, dict) else {}
    if not isinstance(remote, dict):
        return result
    chat_id = str(remote.get('chat_id') or '')
    if not chat_id:
        save()
        return result
    result.update(chat_id=chat_id, creation_status='created', creation_error=None)
    save()  # Member or invite-link failure cannot lose a successful creation.
    if members:
        try:
            checked = run_cli(['lark-cli', '--profile', profile_name, 'im', '+chat-members-list',
                               '--chat-id', chat_id, '--member-types', 'user', '--member-id-type', 'open_id',
                               '--page-all', '--page-limit', '0', '--as', 'bot', '--format', 'json'])
            values = checked.get('data', checked)
            users = values.get('users')
            if not isinstance(users, list):
                raise ValueError('成员列表未返回可核验的用户列表')
            present = {str(u.get('member_id') or u.get('open_id') or u.get('id') or '') for u in users}
            joined = [uid for uid in members if uid in present]
            remaining = [uid for uid in members if uid not in present]
            complete = not (values.get('has_more') or values.get('truncations'))
            result['members'].update(status='verified' if not remaining else 'partial' if complete else 'pending',
                                     joined=joined, missing=remaining if complete else [], pending=[] if complete else remaining)
        except Exception as exc:
            from .wechat_feishu_mirror import safe_error
            result['members']['error'] = safe_error(exc)
        save()
    try:
        result.update(share_link(profile_name, chat_id))
    except Exception as exc:
        from .wechat_feishu_mirror import safe_error
        result['share_error'] = safe_error(exc)
    save()
    return result
