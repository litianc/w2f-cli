"""Follow authoritative source group names without blocking message delivery."""
from __future__ import annotations
import time
from .manage import run_cli, payload_items


def _active(store, bid):
    binding=store.get_binding(bid)
    if not binding or not binding.get('name_sync_enabled') or binding['archived'] or not binding['enabled'] or store.get_control(bid)['status']!='running':
        return None
    profile=next((p for p in store.list_profiles() if p['app_id']==binding['lark_app_id'] and p['enabled']),None)
    if not profile:return None
    return {**binding,'lark_profile':profile['profile_name']}


def sync_group_name(config, store):
    bid=int(config['binding_id']);binding=_active(store,bid)
    if not binding:return
    key=f'name_sync:{bid}'
    scope=[binding['source_account'],binding['wechat_talker'],binding['feishu_chat_id'],binding['lark_app_id']]
    state=store.setting(key,{})
    if state.get('scope')!=scope:state={'scope':scope}
    state.update(checked_at=time.time(),next_check_at=time.time()+60)
    try:
        from .wechat_feishu_mirror import WECHAT_READER
        rows=payload_items(run_cli([str(config.get('wechat_reader') or WECHAT_READER),'resolve-chat',binding['wechat_talker'],'--type-filter','group','--limit','10']),'candidates')
        matches=[row for row in rows if str(row.get('username') or row.get('talker') or '')==binding['wechat_talker']]
        if len(matches)!=1:raise ValueError('未能按稳定 ID 唯一读取源群真实名称')
        # Rion display_name can be a private remark, so never prefer it to nick_name.
        source=matches[0]
        name=str(source.get('nick_name') if 'nick_name' in source else source.get('name') or source.get('display_name') or '').strip()
        if not name or name==binding['wechat_talker'] or len(name)>60 or any(ord(c)<32 for c in name):
            raise ValueError('源群真实名称缺失或不符合飞书群名限制，未修改目标')
        if 'observed' not in state:
            state.update(observed=name,applied=None,desired=None,status='baseline',error=None)
            store.save_setting(key,state)
            return
        if name!=state['observed']:
            state.update(observed=name,desired=name,status='pending',error=None)
            store.save_setting(key,state)
        if not state.get('desired') or state.get('desired')==state.get('applied'):
            state.update(error=None,status='synced' if state.get('applied') else 'baseline')
            store.save_setting(key,state)
            return
        with store.binding_maintenance(bid):
            current=_active(store,bid)
            if not current or [current[k] for k in ('source_account','wechat_talker','feishu_chat_id','lark_app_id')]!=scope:return
            prefix=['lark-cli','--profile',current['lark_profile'],'im']
            suffix=['--chat-id',current['feishu_chat_id'],'--as','bot','--format','json']
            def target_name():
                value=run_cli(prefix+['chats','get']+suffix)
                return str(value.get('data',value).get('name') or '')
            before=target_name()
            if before!=name:
                if not _active(store,bid):return
                run_cli(prefix+['+chat-update', '--name', name]+suffix)
                if target_name()!=name:raise ValueError('飞书回读群名不一致，保留待重试状态')
            state.update(applied=name,status='synced',error=None,synced_at=time.time())
            store.save_setting(key,state)
            store.rename_binding(bid,wechat_chat_name=name,feishu_chat_name=name,actor='worker-name-sync')
            store.audit('sync_group_name',binding_id=bid,before={'name':before},after={'name':name},actor='worker')

    except Exception as exc:
        from .wechat_feishu_mirror import safe_error
        state.update(status='failed',error=safe_error(exc))
        store.save_setting(key,state)


def group_name_loop(config, store, stopped):
    # A Worker-owned maintenance thread, not a new service or auto restart loop.
    # Network/permission failures cannot delay the message queue.
    while not stopped.is_set():
        try:
            from .manage import profile_list
            profile_list(store)
            sync_group_name(config,store)
            from .member_sync import process_member_tasks
            process_member_tasks(config,store)
        except Exception:pass  # A transient local database failure retries next cycle.
        stopped.wait(60)
