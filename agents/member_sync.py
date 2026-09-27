"""Persist and reconcile explicit membership events independently of messages."""
from __future__ import annotations
import json
import re
import time
import xml.etree.ElementTree as ET
from .manage import run_cli
from integrations.forward_records import stable_sender_id


class NeedsReview(ValueError):
    pass


def affected_members(row):
    # A pasted XML snippet in an ordinary chat is not a system event.
    try:local_type=int(row.get('local_type') or 0)
    except (ValueError,TypeError):return []
    if local_type!=10000:return []
    raw=str(row.get('content') or row.get('text') or '')
    start=raw.find('<sysmsg')
    if start<0:return []
    try:root=ET.fromstring(raw[start:].strip())
    except ET.ParseError:return []
    if root.tag!='sysmsg' or root.get('type')!='sysmsgtemplate':return []
    content=root.find('.//content_template')
    if content is None:return []
    template=re.sub(r'[\s\"“”]','',content.findtext('template') or '')
    invitation=re.fullmatch(r'(你|\$username\$)邀请\$names\$加入了群聊[。]?(?:\$revoke\$)?',template)
    removal=re.fullmatch(r'(你|\$username\$)将\$(names|kickoutname)\$移出了群聊[。]?',template)
    if invitation:operation,role='add','names'
    elif removal:operation,role='remove',removal.group(2)
    else:return []
    links=[link for link in content.findall('./link_list/link') if link.get('name')==role and link.get('hidden')!='1']
    if len(links)!=1:return []
    members=links[0].findall('./memberlist/member')
    ids=[stable_sender_id(m.findtext('username') or '') for m in members]
    if not ids or any(not value for value in ids):return []
    return [(value,operation) for value in dict.fromkeys(ids)]


def active_binding(store,bid):
    binding=store.get_binding(bid)
    if not binding or binding['archived'] or not binding['enabled'] or store.get_control(bid)['status']!='running':return None
    profile=next((p for p in store.list_profiles() if p['app_id']==binding['lark_app_id'] and p['enabled']),None)
    if not profile or not binding['member_sync_enabled']:return None
    return {**binding,'lark_profile':profile['profile_name'],'profile_id':profile['id']}


def identity(store,binding,wxid):
    with store.connect() as db:
        rows=db.execute("""SELECT w.id wechat_identity_id,p.id person_id,f.id feishu_identity_id,
            f.open_id,p.confirmed_name FROM wechat_identities w
            JOIN people p ON p.id=w.person_id AND p.status='active'
            JOIN feishu_identities f ON f.person_id=p.id AND f.profile_id=?
            WHERE w.source_account=? AND w.wechat_sender_id=? AND w.status='confirmed'""",
            (binding['profile_id'],binding['source_account'],wxid)).fetchall()
    if len(rows)!=1:raise NeedsReview('微信与当前应用的飞书身份缺失或存在歧义，请在人员页核实后重试')
    return dict(rows[0])


def enqueue(store,binding):
    bid=binding['id'];setting=store.setting(f'member_sync:{bid}',{})
    boundary=int(setting.get('boundary',0));cursor=max(boundary,int(setting.get('cursor',boundary)))
    with store.connect() as db:
        rows=db.execute('SELECT local_id,payload_json FROM messages WHERE binding_id=? AND local_id>? ORDER BY local_id LIMIT 1000',(bid,cursor)).fetchall()
    for item in rows:
        row=json.loads(item['payload_json'])
        if row.get('talker')!=binding['wechat_talker']:continue
        for wxid,operation in affected_members(row):
            try:person=identity(store,binding,wxid);state='pending';error=None
            except NeedsReview as exc:person=None;state='needs_review';error=str(exc)
            ts=time.time()
            with store.connect() as db:
                db.execute("""INSERT OR IGNORE INTO member_tasks(binding_id,event_local_id,source_account,talker,chat_id,app_id,wechat_id,operation,identity_json,state,error,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(bid,item['local_id'],binding['source_account'],binding['wechat_talker'],binding['feishu_chat_id'],binding['lark_app_id'],wxid,operation,json.dumps(person) if person else None,state,error,ts,ts))
        cursor=max(cursor,item['local_id'])
    # Do not overwrite the enabled flag if the user disabled it during reads.
    with store.connect() as db:
        raw=db.execute('SELECT value FROM settings WHERE key=?',(f'member_sync:{bid}',)).fetchone()
        current=json.loads(raw[0]) if raw else {}
        if current.get('boundary')==setting.get('boundary'):
            current['cursor']=max(cursor,int(current.get('cursor',0)))
            db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(current),f'member_sync:{bid}'))


def source_members(binding):
    from .wechat_feishu_mirror import WECHAT_READER
    value=run_cli([str(binding.get('wechat_reader') or WECHAT_READER),'members',binding['wechat_talker'],'--limit','10000','--offset','0'])
    data=value.get('data',value);rows=data.get('members',[])
    ids=[str(row.get('username') or '') for row in rows]
    if data.get('chatroom_id')!=binding['wechat_talker'] or not ids or len(set(ids))!=len(ids) or len(ids)!=data.get('total') or any(not stable_sender_id(x) for x in ids):
        raise NeedsReview('源群成员快照不完整，不能据此执行成员增删；请刷新本机微信群后重试')
    return set(ids)


def target_members(binding):
    prefix=['lark-cli','--profile',binding['lark_profile'],'im']
    value=run_cli(prefix+['+chat-members-list','--chat-id',binding['feishu_chat_id'],'--member-id-type','open_id','--page-all','--page-limit','0','--as','bot','--format','json'])
    data=value.get('data',value)
    if data.get('truncations') or data.get('has_more') or not isinstance(data.get('users'),list) or not isinstance(data.get('bots'),list):
        raise NeedsReview('飞书成员列表不完整，请检查成员读取权限与群安全限制')
    def ids(rows):
        return {str(row.get('member_id') or row.get('open_id') or row.get('bot_id') or row.get('app_id') or '') for row in rows}
    users,bots=ids(data['users']),ids(data['bots'])
    if '' in users or '' in bots:raise NeedsReview('飞书成员标识缺失，需人工核验')
    return users,bots


def complete_task(store,task,state,error=None,attempts=None):
    with store.connect() as db:
        db.execute('UPDATE member_tasks SET state=?,error=?,attempts=COALESCE(?,attempts),retry_at=?,updated_at=? WHERE id=?',
            (state,error,attempts,time.time()+min(120,30*max(1,attempts or task['attempts'])),time.time(),task['id']))
    store.audit('member_sync',binding_id=task['binding_id'],before={'task_id':task['id'],'state':task['state']},
                after={'state':state,'error':error},actor='worker',result=state)


def process_member_tasks(config,store):
    bid=int(config['binding_id']);binding=active_binding(store,bid)
    if not binding:return
    if store.recovery(bid).get('phase') not in {'idle','catching_up'}:return
    enqueue(store,binding)
    with store.connect() as db:
        tasks=[dict(r) for r in db.execute("SELECT * FROM member_tasks WHERE binding_id=? AND state IN ('pending','failed','uncertain') AND retry_at<=? ORDER BY event_local_id,id LIMIT 20",(bid,time.time()))]
    for task in tasks:
        with store.binding_maintenance(bid):
            current=active_binding(store,bid)
            if not current:return
            current={**config,**current}
            if any(task[a]!=current[b] for a,b in [('source_account','source_account'),('talker','wechat_talker'),('chat_id','feishu_chat_id'),('app_id','lark_app_id')]):
                complete_task(store,task,'obsolete','原绑定或应用已变更，旧任务不重新指向新目标');continue
            with store.connect() as db:
                newer=db.execute('SELECT 1 FROM member_tasks WHERE binding_id=? AND wechat_id=? AND event_local_id>? LIMIT 1',(bid,task['wechat_id'],task['event_local_id'])).fetchone()
            if newer:complete_task(store,task,'obsolete','已存在更新成员事件');continue
            snapshot=json.loads(task['identity_json']) if task['identity_json'] else {}
            with store.people_maintenance(snapshot.get('person_id',0)):
                attempted=False
                try:
                    person=identity(store,current,task['wechat_id'])
                    original=json.loads(task['identity_json']) if task['identity_json'] else None
                    keys=('wechat_identity_id','person_id','feishu_identity_id','open_id')
                    if not original or any(original[k]!=person[k] for k in keys):
                        raise NeedsReview('人员关联已变更，旧任务不改指向新身份；请核实后处理')
                    present=task['wechat_id'] in source_members(current)
                    desired=task['operation']=='add'
                    if present!=desired:
                        complete_task(store,task,'obsolete','当前源成员状态与此旧事件不符');continue
                    users,bots=target_members(current);member=person['open_id']
                    if member in bots:raise NeedsReview('目标是机器人，不能自动增删镜像机器人')
                    if (member in users)==desired:
                        complete_task(store,task,'applied');continue
                    prefix=['lark-cli','--profile',current['lark_profile'],'im']
                    if not desired:
                        info=run_cli(prefix+['chats','get','--chat-id',current['feishu_chat_id'],'--user-id-type','open_id','--as','bot','--format','json'])
                        owner=info.get('data',info).get('owner_id')
                        if not owner or owner==member:raise NeedsReview('群主身份未知或目标为群主，不自动移出；请人工核验')
                    # Re-read after network calls for both directions. A
                    # leave or rejoin can supersede the event while querying.
                    if (task['wechat_id'] in source_members(current))!=desired:
                        complete_task(store,task,'obsolete','源群成员状态已再次变化');continue
                    with store.connect() as db:
                        db.execute("UPDATE member_tasks SET state='uncertain',attempts=attempts+1,updated_at=? WHERE id=?",(time.time(),task['id']))
                    attempted=True
                    args=prefix+['chat.members','create' if desired else 'delete','--chat-id',current['feishu_chat_id'],
                        '--member-id-type','open_id','--data',json.dumps({'id_list':[member]}),'--as','bot','--format','json']
                    args+=['--succeed-type','2'] if desired else ['--yes']
                    result=run_cli(args).get('data',{})
                    if any(result.get(k) for k in ('invalid_id_list','not_existed_id_list','pending_approval_id_list')):
                        raise NeedsReview('目标人员不可见、无效或等待群主审批，请检查应用可用范围及审批状态')
                    after,_=target_members(current)
                    if (member in after)!=desired:raise RuntimeError('成员状态回读不符，未标记成功')
                    complete_task(store,task,'applied',attempts=task['attempts']+1)
                except NeedsReview as exc:
                    complete_task(store,task,'needs_review',str(exc),task['attempts']+int(attempted))
                except Exception as exc:
                    from .wechat_feishu_mirror import safe_error
                    attempts=task['attempts']+1
                    message=safe_error(exc)+'；请核对 bot 成员读写权限、群角色或网络后重试'
                    complete_task(store,task,'needs_review' if attempts>=3 else ('uncertain' if attempted else 'failed'),message,attempts)


def retry_member_task(store,task_id):
    with store.connect() as db:row=db.execute('SELECT * FROM member_tasks WHERE id=?',(task_id,)).fetchone()
    if not row or row['state'] not in {'needs_review','failed','uncertain'}:raise ValueError('成员待办不存在或无需重试')
    task=dict(row);binding=active_binding(store,task['binding_id'])
    if not binding:raise ValueError('请先启用成员同步并启动此绑定')
    person=identity(store,binding,task['wechat_id'])
    original=json.loads(task['identity_json']) if task['identity_json'] else None
    if original and any(original[k]!=person[k] for k in ('wechat_identity_id','person_id','feishu_identity_id','open_id')):
        raise ValueError('旧任务身份已变更，不能改指向新身份；请手动核验群成员')
    with store.connect() as db:
        db.execute("UPDATE member_tasks SET identity_json=?,state='pending',retry_at=0,attempts=0,error=NULL,updated_at=? WHERE id=?",(json.dumps(person),time.time(),task_id))
    store.audit('retry_member_task',before={'task_id':task_id,'attempts':task['attempts']},after={'state':'pending'},binding_id=task['binding_id'])
    return {'queued':True}
