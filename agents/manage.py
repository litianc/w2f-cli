#!/usr/bin/env python3
"""One management service for the local web console and Codex JSON CLI."""
from __future__ import annotations
import argparse
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .storage import MirrorStore
from .chat_creation import CliRequestError, create_target
from .paths import DATA_DIR, DEFAULT_CONFIG, ROOT, runtime_paths

DEFAULT_DB=DATA_DIR/'wechat_feishu_mirror.sqlite3'
READER=Path(os.environ.get('WECHAT_READER',str(Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'skills/wechat-cli/scripts/reader.sh')))
SETUP_FIELDS={'source_account','wechat_chat_name','wechat_talker','feishu_chat_name','feishu_chat_id','lark_profile','lark_app_id','admin_feishu_user_id','admin_profile','feishu_app_link','feishu_assistant_link','poll_interval','reader_wakeup'}


def read_config(path):
    return runtime_paths(json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else {})


def write_runtime_config(value,path):
    # All values accepted from the browser are allowlisted separately. Runtime
    # paths are supplied only by local config/CLI, never arbitrary HTTP bodies.
    value={k:v for k,v in value.items() if k in SETUP_FIELDS|{'sqlite_file','data_dir','media_dir','log_dir','state_file','wechat_reader','realtime_wakeup','realtime_stream_url','sender_map','macos_image_key_derivation'}}
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');tmp.chmod(0o600);tmp.replace(path)


def run_cli(argv,*,stdin=None):
    from .wechat_feishu_mirror import safe_error
    try:
        if argv and argv[0] == 'lark-cli':
            argv = [shutil.which('lark-cli') or str(Path.home()/'.local/bin/lark-cli'), *argv[1:]]
        p=subprocess.run(argv,cwd=ROOT,input=stdin,text=True,encoding='utf-8',capture_output=True,timeout=60,
                         env={**os.environ,'LARKSUITE_CLI_NO_UPDATE_NOTIFIER':'1','LARKSUITE_CLI_NO_SKILLS_NOTIFIER':'1'})
    except subprocess.TimeoutExpired: raise CliRequestError('CLI 请求超时，写入结果需核对') from None
    except OSError: raise CliRequestError('CLI 未能启动，请检查安装路径', uncertain=False) from None
    try: value=json.loads(p.stdout)
    except ValueError: value={}
    if p.returncode and not (isinstance(value,dict) and value.get('error')):
        try: value=json.loads(p.stderr)
        except ValueError: value={}
    if p.returncode or (isinstance(value,dict) and value.get('ok') is False):
        error=value.get('error',{}) if isinstance(value,dict) else {}
        message=error.get('message') if isinstance(error,dict) else str(error)
        raise CliRequestError(safe_error(message or 'CLI 请求失败'),
            uncertain=not (isinstance(error,dict) and error.get('type') in {'validation','permission','auth','confirmation_required'}))
    return value


run_command=run_cli


def payload_items(payload,*keys):
    if isinstance(payload,list):return payload
    data=payload.get('data',payload)
    if isinstance(data,list):return data
    for key in keys:
        if isinstance(data.get(key),list):return data[key]
    return []


def profile_list(store):
    remote=payload_items(run_cli(['lark-cli','profile','list']),'profiles','items')
    remote=[{'name':str(x.get('name') or x.get('profile') or x.get('appId') or ''),'app_id':str(x.get('appId') or x.get('app_id') or '')} for x in remote]
    remote=[x for x in remote if x['name'] and x['app_id']]
    # Keep app identity immutable even when an external CLI operation reuses a
    # profile name. Move the missing reference aside, never overwrite App ID.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        local=[dict(r) for r in db.execute('SELECT * FROM profiles')]
        by_name={x['name']:x['app_id'] for x in remote}
        for p in local:
            if p['profile_name'] in by_name and by_name[p['profile_name']]!=p['app_id']:
                db.execute("UPDATE profiles SET profile_name=?,enabled=0,bot_status='replaced',last_error=? WHERE id=?",(f"missing-{p['id']}-{int(time.time())}",'同名 Profile 被替换为另一应用',p['id']))
                db.execute('UPDATE bindings SET enabled=0 WHERE lark_app_id=?',(p['app_id'],))
                db.execute("UPDATE runtime_control SET status='stopped',reason='Profile App ID changed',updated_at=? WHERE binding_id IN (SELECT id FROM bindings WHERE lark_app_id=?)",(time.time(),p['app_id']))
        for item in remote:
            p=db.execute('SELECT * FROM profiles WHERE app_id=?',(item['app_id'],)).fetchone()
            if p:
                db.execute('UPDATE profiles SET profile_name=?,updated_at=? WHERE id=?',(item['name'],time.time(),p['id']))
                db.execute('UPDATE bindings SET lark_profile=? WHERE lark_app_id=?',(item['name'],item['app_id']))
            else:
                db.execute('INSERT INTO profiles(profile_name,app_id,protected,created_at,updated_at) VALUES(?,?,?,?,?)',(item['name'],item['app_id'],int(item['name']=='feishu-mcp'),time.time(),time.time()))
        apps={p['app_id'] for p in remote}
        for p in local:
            if p['app_id'] not in apps:
                db.execute("UPDATE profiles SET bot_status='missing',last_error='Profile 在 CLI 中缺失' WHERE app_id=?",(p['app_id'],))
                db.execute("UPDATE runtime_control SET status='stopped',reason='Profile missing' WHERE binding_id IN (SELECT id FROM bindings WHERE lark_app_id=?)",(p['app_id'],))
    return store.list_profiles()


def profile_health(store,name):
    errors=[]
    try:
        auth=run_cli(['lark-cli','--profile',name,'auth','status','--json','--verify'])
        auth=auth.get('data',auth)
        user=auth.get('identities',{}).get('user',{})
        user_status='ready' if user.get('status') in {'ready','authenticated'} else str(user.get('tokenStatus') or user.get('status') or 'needs_attention')
    except Exception as exc:user_status='needs_attention';errors.append(str(exc))
    try:
        run_cli(['lark-cli','--profile',name,'im','+chat-list','--as','bot','--page-size','1','--format','json']);bot_status='ready'
    except Exception as exc:bot_status='error';errors.append(str(exc))
    store.update_profile_health(name,bot_status=bot_status,user_status=user_status,error='; '.join(errors) or None)
    return {'profile':name,'bot_status':bot_status,'user_status':user_status,'error':'; '.join(errors) or None}


def discover_profile_user(store, profile_name):
    """Read the CLI's saved user identity for exactly one registered app."""
    profile=next((item for item in store.list_profiles() if item['profile_name']==profile_name and item['enabled']),None)
    if not profile:raise ValueError('请选择已启用的发送 Profile')
    status=run_cli(['lark-cli','--profile',profile_name,'auth','status','--json','--verify'])
    if str(status.get('appId') or '')!=str(profile['app_id']):
        raise ValueError('Profile App ID 与登记应用不一致，请先在「飞书应用」刷新 Profile')
    user=(status.get('identities') or {}).get('user') or {}
    open_id=str(user.get('openId') or '').strip()
    if open_id and not open_id.startswith('ou_'):raise ValueError('CLI 返回的用户 open_id 格式不正确')
    return {'profile_name':profile_name,'open_id':open_id or None,
            'display_name':str(user.get('userName') or ''),
            'available':bool(user.get('available')),
            'user_status':str(user.get('tokenStatus') or user.get('status') or 'unknown')}


def search_feishu_people(store, profile_name, name, query=''):
    """Find visible contacts for one app; the caller must choose a candidate."""
    profile=next((item for item in store.list_profiles()
                  if item['profile_name']==profile_name and item['enabled']),None)
    if not profile:raise ValueError('请选择已启用的发送 Profile')
    name=str(name or '').strip()
    query=str(query or '').strip()
    if not name or len(name)>50 or ',' in name or '\n' in name:
        raise ValueError('请先确认人员实名（最多 50 字，不能含逗号或换行）')
    if query and (len(query)>50 or ',' in query or '\n' in query):
        raise ValueError('搜索词最多 50 字，不能包含逗号或换行')
    auth=run_cli(['lark-cli','--profile',profile_name,'auth','status','--json','--verify'])
    if str(auth.get('appId') or '')!=str(profile['app_id']):
        raise ValueError('Profile App ID 与登记应用不一致，请先在「飞书应用」刷新 Profile')
    user=(auth.get('identities') or {}).get('user') or {}
    if not user.get('available'):
        raise ValueError('此应用的用户授权不可用；通讯录姓名搜索需要用户授权。请先检查该 Profile 的用户授权，或手动填写 open_id')
    if query:
        terms=[query]
    else:
        from pypinyin import lazy_pinyin, Style
        terms=[name]
        if len(name)>=3:
            terms.extend([name[:2],name[-2:]])
        syllables=lazy_pinyin(name)
        if any('\u4e00'<=ch<='\u9fff' for ch in name):
            terms.extend([''.join(syllables), ''.join(lazy_pinyin(name,style=Style.FIRST_LETTER))])
        terms=list(dict.fromkeys(term for term in terms if term and len(term)<=50))
    argv=['lark-cli','--profile',profile_name,'contact','+search-user','--as','user',
          '--page-size','30','--format','json']
    argv+=['--query',terms[0]] if len(terms)==1 else ['--queries',','.join(terms)]
    result=run_cli(argv)
    data=result.get('data',result)
    raw=data.get('users',[]) if isinstance(data,dict) else []
    candidates={}
    for item in raw:
        open_id=str(item.get('open_id') or '').strip()
        if not open_id.startswith('ou_'):continue
        display=str(item.get('localized_name') or item.get('name') or '').strip()
        source=str(item.get('matched_query') or terms[0])
        if open_id not in candidates:
            candidates[open_id]={'open_id':open_id,'display_name':display,
                                 'department':str(item.get('department') or ''),
                                 'enterprise_email':str(item.get('enterprise_email') or ''),
                                 'is_cross_tenant':bool(item.get('is_cross_tenant')),
                                 'matched_queries':[]}
        if source not in candidates[open_id]['matched_queries']:
            candidates[open_id]['matched_queries'].append(source)
    people=sorted(candidates.values(),key=lambda item:(item['display_name']!=name,
                   terms[0] not in item['matched_queries'],item['display_name'].casefold(),item['open_id']))
    queries=data.get('queries',[]) if isinstance(data,dict) else []
    partial=bool(data.get('has_more') or any(q.get('has_more') or q.get('error') for q in queries)) if isinstance(data,dict) else False
    return {'profile_name':profile_name,'app_id':profile['app_id'],'searched':terms,
            'candidates':people[:100],'has_more':partial or len(people)>100}


def chat_share_link(profile_name, chat_id):
    payload = run_cli(['lark-cli', '--profile', profile_name, 'im', 'chats', 'link',
                       '--chat-id', chat_id, '--data', json.dumps({'validity_period': 'week'}),
                       '--as', 'bot', '--format', 'json'])
    data = payload.get('data', payload)
    link = str(data.get('share_link') or '')
    if not link.startswith('https://'):
        raise RuntimeError('飞书未返回有效的群分享链接')
    return {'share_link': link, 'expire_time': data.get('expire_time'),
            'is_permanent': bool(data.get('is_permanent'))}


def search_wechat(query,limit=30):
    query=str(query or '').strip()
    if query:
        return payload_items(run_cli([str(READER),'resolve-chat',query,'--type-filter','group','--limit',str(limit)]),'candidates')
    # resolve-chat requires a nonempty query. The contacts command filters
    # groups after its limit, so ask for the full index before limiting here.
    groups=payload_items(run_cli([str(READER),'contacts','--groups-only','--limit','100000']),'contacts')
    return sorted(groups,key=lambda item:str(item.get('display_name') or item.get('username') or '').casefold())[:limit]


def search_wechat_people(store, config_path, binding_id=None, query='', source_account=''):
    """Return local WeChat identity candidates; never infer a real person."""
    query=str(query or '').strip()
    if len(query)>50 or any(ch in query for ch in '\r\n'):
        raise ValueError('搜索词最多 50 字，不能包含换行')
    bindings=[b for b in store.list_bindings() if not b['archived']]
    binding=None
    if binding_id not in (None,'',0,'0'):
        try: selected=int(binding_id)
        except (TypeError,ValueError): raise ValueError('群映射编号不正确') from None
        binding=next((b for b in bindings if b['id']==selected),None)
        if not binding:raise ValueError('请选择有效的微信群映射')
        account=binding['source_account']
        result=run_cli([str(READER),'members','--chatroom-id',binding['wechat_talker'],'--limit','100000'])
        rows=payload_items(result,'members')
        group_name=binding['wechat_alias'] or binding['wechat_chat_name']
    else:
        if not query:raise ValueError('搜索联系人时请填写昵称、备注或微信 ID')
        config=read_config(config_path)
        accounts={str(b['source_account']) for b in bindings}
        if not accounts:accounts.add(str(config.get('source_account') or 'default'))
        account=str(source_account or (bindings[0]['source_account'] if bindings else config.get('source_account') or 'default')).strip()
        if account not in accounts:raise ValueError('微信账号标识不属于当前配置')
        result=run_cli([str(READER),'contacts','--friends-only','--limit','100000'])
        rows=payload_items(result,'contacts')
        group_name=''
    from pypinyin import lazy_pinyin, Style
    term=query.casefold()
    known={(r['source_account'],r['wechat_sender_id']):r for r in store.list_wechat_identities()}
    candidates={}
    for row in rows:
        sender_id=str(row.get('username') or '').strip()
        if not sender_id or sender_id.endswith('@chatroom') or sender_id.startswith('unidentified:'):
            continue
        names=[str(row.get(key) or '').strip() for key in ('display_name','remark','nick_name','alias')]
        display=next((x for x in names if x),sender_id)
        terms=[*names,sender_id]
        if term:
            rank=3
            for value in terms:
                folded=value.casefold()
                if folded==term:rank=min(rank,0)
                elif folded.startswith(term):rank=min(rank,1)
                elif term in folded:rank=min(rank,2)
                if value and any('\u4e00'<=ch<='\u9fff' for ch in value):
                    roman=''.join(lazy_pinyin(value)).casefold()
                    initials=''.join(lazy_pinyin(value,style=Style.FIRST_LETTER)).casefold()
                    if roman.startswith(term) or initials.startswith(term):rank=min(rank,2)
            if rank==3:continue
        else:rank=2
        linked=known.get((account,sender_id),{})
        candidates[sender_id]={'source_account':account,'wechat_sender_id':sender_id,
            'display_name':display,'nick_name':names[2],'remark':names[1],'alias':names[3],
            'group_name':group_name,'person_id':linked.get('person_id'),
            'confirmed_name':linked.get('confirmed_name'), 'rank':rank}
    ordered=sorted(candidates.values(),key=lambda item:(item['rank'],item['display_name'].casefold(),item['wechat_sender_id']))
    for item in ordered:item.pop('rank')
    return {'source_account':account,'group_name':group_name,'candidates':ordered[:100],
            'total':len(ordered),'has_more':len(ordered)>100}


def search_feishu(profile,query):
    args=['lark-cli','--profile',profile,'im','+chat-search','--as','bot','--query',query,'--format','json'] if query else ['lark-cli','--profile',profile,'im','+chat-list','--as','bot','--page-size','100','--format','json']
    return payload_items(run_cli(args),'items','chats')


def check_target(profile_name,chat_id,store=None):
    """Feishu-side half of a preflight: app enabled, group visible, bot inside."""
    checks={};errors=[]
    profile=next((p for p in (store.list_profiles() if store else []) if p['profile_name']==profile_name),None)
    checks['profile_ready']=bool(profile and profile['enabled'])
    if not checks['profile_ready']:errors.append('请先接入并启用发送应用');return {'checks':checks,'errors':errors}
    try:
        result=run_cli(['lark-cli','--profile',profile_name,'im','chats','get','--chat-id',chat_id,'--as','bot','--format','json'])
        checks['target_visible']=True;checks['target_name']=result.get('data',{}).get('name','')
        members=run_cli(['lark-cli','--profile',profile_name,'im','+chat-members-list','--chat-id',chat_id,'--member-types','bot','--page-all','--as','bot','--format','json'])
        bots=payload_items(members,'bots','items')
        # API schemas vary: list bot IDs explicitly; no substring matching.
        checks['bot_in_chat']=any(str(p.get('app_id') or p.get('id') or p.get('member_id') or '')==str(profile['app_id'] if profile else '') for p in bots)
        if not checks['bot_in_chat']:errors.append('未在目标群机器人列表中确认该 App ID')
    except Exception as exc:checks['target_visible']=False;errors.append(str(exc))
    return {'checks':checks,'errors':errors}


def preflight_binding(data,store=None):
    checks={};errors=[]
    profile=next((p for p in (store.list_profiles() if store else []) if p['profile_name']==data.get('lark_profile')),None)
    checks['profile_ready']=bool(profile and profile['enabled'])
    if not checks['profile_ready']:errors.append('请先接入并启用发送应用')
    try:
        from .wechat_feishu_mirror import source_page
        result=source_page({**data,'page_size':1},0)
        checks['source_readable']=result.get('contract')=='ordered_sync_v1'
    except Exception as exc:checks['source_readable']=False;errors.append(str(exc))
    target=check_target(data.get('lark_profile',''),data.get('feishu_chat_id',''),store)
    checks.update(target['checks']);errors.extend(target['errors'])
    if store:
        for b in store.list_bindings(False):
            if b['id']==data.get('id'):continue
            if (b['source_account']==data.get('source_account','default') and b['wechat_talker']==data.get('wechat_talker')) or b['feishu_chat_id']==data.get('feishu_chat_id'):
                errors.append('源群或目标群已有有效绑定');break
    return {'ok':not errors,'checks':checks,'errors':errors,'permission_note':'读取预检不发送测试消息；发送/资源权限在实际调用时核验'}


def setup_status(config_path,db_path):
    config=read_config(config_path);store=MirrorStore(db_path)
    bindings=store.list_bindings() if Path(db_path).exists() else []
    saved=store.setting('setup_draft',{}) if Path(db_path).exists() else {}
    values={k:v for k,v in config.items() if k in SETUP_FIELDS};values.update(saved)
    missing=[k for k in ('wechat_talker','feishu_chat_id','lark_profile') if not values.get(k)]
    return {'initialized':bool(bindings),'config_path':str(config_path),'database':str(db_path),'data_dir':config.get('data_dir',str(DATA_DIR)),'missing':missing,'draft':values}


def setup_init(config_path,db_path,payload=None):
    values=read_config(config_path)
    if payload is None:
        payload={k:input(k+': ').strip() for k in ('wechat_chat_name','wechat_talker','feishu_chat_name','feishu_chat_id','lark_profile','lark_app_id')}
    values.update({k:v for k,v in payload.items() if k in SETUP_FIELDS})
    values['sqlite_file']=str(Path(db_path).resolve())
    if any(not values.get(k) for k in ('wechat_talker','feishu_chat_id','lark_profile')):raise ValueError('请完成源群、目标群和发送应用配置')
    store=MirrorStore(db_path)
    if Path(db_path).exists():store.backup()
    store.initialize(values)
    write_runtime_config(values,config_path)
    return setup_status(config_path,db_path)


def binding_action(store,config_path,binding_id,action):
    if action=='archive':
        binding_action(store,config_path,binding_id,'stop');store.archive_binding(binding_id);return store.get_binding(binding_id)
    args=[sys.executable,'-m','agents.wechat_feishu_mirror',action,'--config',str(config_path),'--db',str(store.path),'--binding-id',str(binding_id)]
    return run_cli(args)


class Manager:
    def __init__(self,store,config_path):self.store=store;self.config_path=Path(config_path);self.started=time.time()
    def get(self,resource,query=None):
        query=query or {};s=self.store
        if resource=='profiles':return s.list_profiles()
        if resource=='profiles/self':return discover_profile_user(s,str(query.get('profile') or ''))
        if resource=='people/search-feishu':return search_feishu_people(s,str(query.get('profile') or ''),
                                                                       query.get('name',''),query.get('q',''))
        if resource=='people/search-wechat':return search_wechat_people(s,self.config_path,
                query.get('binding_id'),query.get('q',''),query.get('source_account',''))
        if resource=='bindings':return s.list_bindings()
        if resource=='people':return {'people':s.list_people(query.get('status','active')),'archived':s.list_people('archived'),'wechat':s.list_wechat_identities(),'feishu':s.list_feishu_identities(),'actions':s.actions(),'quarantined':s.list_quarantined_identities()}
        if resource=='confirmations':return s.list_confirmations()
        if resource=='media-failures':return [{k:v for k,v in r.items() if k not in {'source_ref','staging_path'}} for r in s.list_media_failures()]
        if resource=='uncertain':return s.uncertain_deliveries()
        if resource=='setup':return setup_status(self.config_path,s.path)
        if resource=='search/wechat':return search_wechat(query.get('q',''))
        if resource=='search/feishu':return search_feishu(query.get('profile',''),query.get('q',''))
        if resource=='wizard/wechat':return search_wechat(query.get('q',''),200)
        if resource=='wizard/feishu':return search_feishu(query.get('profile',''),'')
        if resource=='wizard/check-source':
            from .wechat_feishu_mirror import source_page
            result=source_page({'wechat_talker':query.get('talker',''),'page_size':1},0)
            return {'ok':result.get('contract')=='ordered_sync_v1'}
        if resource=='wizard/check-target':return check_target(query.get('profile',''),query.get('chat_id',''),s)
        if resource=='status':
            return {'version':'0.3','started_at':self.started,'bindings':[{**b,**s.status(b['id'])} for b in s.list_bindings()], 'confirmations':len(s.list_confirmations()),'media_failures':len(s.list_media_failures()),'actions':s.actions()[:20]}
        raise ValueError('接口不存在')
    def post(self,resource,data,actor='codex'):
        s=self.store;op=data.get('op');result=None
        if resource=='profiles':
            name=data.get('profile_name','');profile=next((p for p in s.list_profiles() if p['profile_name']==name),None)
            if op=='sync':return profile_list(s)
            if op=='health':return profile_health(s,name)
            if op=='add':
                secret=data.pop('app_secret','')
                if not secret or not data.get('app_id') or not name:raise ValueError('需要 Profile、App ID 和 Secret')
                run_cli(['lark-cli','profile','add','--name',name,'--app-id',data['app_id'],'--app-secret-stdin'],stdin=secret+'\n');result=profile_list(s)
            elif op=='rename':
                if not profile or profile['protected']:raise ValueError('受保护或不存在的 Profile 不能重命名')
                run_cli(['lark-cli','profile','rename',name,data['new_name']]);result=profile_list(s)
            elif op in {'enable','disable'}:result=s.set_profile_enabled(name,op=='enable',actor)
            elif op=='display':
                if not profile:raise ValueError('应用不存在')
                with s.connect() as db:db.execute('UPDATE profiles SET display_name=? WHERE id=?',(data['name'],profile['id']))
                result=s.list_profiles()
            elif op=='remove':
                if not data.get('confirm'):raise ValueError('请确认删除应用引用')
                if not profile or profile['protected'] or s.profile_has_bindings(name):raise ValueError('受保护或仍有有效绑定的应用不能删除')
                run_cli(['lark-cli','profile','remove',name])
                with s.connect() as db:db.execute('UPDATE profiles SET enabled=0,bot_status=\'removed\' WHERE id=?',(profile['id'],))
                result=s.list_profiles()
            else:raise ValueError('不支持的应用操作')
        elif resource=='bindings':
            if op=='preflight':return preflight_binding(data,s)
            if op=='create':
                check=preflight_binding(data,s)
                if not check['ok']:raise ValueError('; '.join(check['errors']))
                result=s.create_binding(data)
            elif op in {'start','stop','pause','resume','archive'}:
                if op=='start':
                    profile_list(s)
                    binding=s.get_binding(int(data['id']));check=preflight_binding(binding,s)
                    if not check['ok']:raise ValueError('; '.join(check['errors']))
                result=binding_action(s,self.config_path,int(data['id']),op)
            elif op=='member-sync':
                bid=int(data['id']);binding=s.get_binding(bid)
                if not binding or binding['archived']:raise ValueError('有效群映射不存在')
                if data.get('confirm') is not True:raise ValueError('请确认成员同步将实际邀请或移出飞书成员')
                if not isinstance(data.get('enabled'),bool):raise ValueError('enabled 必须为布尔值')
                setting=s.setting(f'member_sync:{bid}',{})
                if data['enabled'] and 'boundary' not in setting:
                    from .wechat_feishu_mirror import source_page
                    runtime={**read_config(self.config_path),**binding,'binding_id':bid}
                    setting['boundary']=int(source_page(runtime,0)['through_local_id'])
                with s.binding_maintenance(bid):
                    setting['enabled']=data['enabled'];s.save_setting(f'member_sync:{bid}',setting)
                result=s.get_binding(bid)
            elif op=='member-retry':
                if data.get('confirm') is not True:raise ValueError('请确认重试此成员操作')
                from .member_sync import retry_member_task
                result=retry_member_task(s,int(data['id']))
            elif op=='name-sync':
                bid=int(data['id']);binding=s.get_binding(bid)
                if not binding or binding['archived']:raise ValueError('有效群映射不存在')
                if data.get('confirm') is not True:raise ValueError('请确认自动跟随会修改飞书真实群名')
                if not isinstance(data.get('enabled'),bool):raise ValueError('enabled 必须为布尔值')
                with s.binding_maintenance(bid):
                    s.save_setting(f'name_sync_enabled:{bid}',data['enabled'])
                result=s.get_binding(bid)
            elif op=='share-link':
                binding=s.get_binding(int(data['id']))
                if not binding or binding['archived']:raise ValueError('有效群映射不存在')
                result=chat_share_link(binding['lark_profile'],binding['feishu_chat_id'])
            elif op=='rename':
                bid=int(data['id']);before=s.get_binding(bid)
                if not before:raise ValueError('绑定不存在')
                with s.connect() as db:db.execute('UPDATE bindings SET wechat_alias=?,feishu_alias=?,updated_at=? WHERE id=?',(data.get('wechat_alias',''),data.get('feishu_alias',''),time.time(),bid))
                result=s.get_binding(bid)
            elif op in {'rename-feishu','refresh-names'}:
                binding=s.get_binding(int(data['id']))
                if not binding:raise ValueError('绑定不存在')
                if op=='rename-feishu':
                    if not data.get('confirm'):raise ValueError('请确认修改飞书真实群名称')
                    run_cli(['lark-cli','--profile',binding['lark_profile'],'im','+chat-update','--chat-id',binding['feishu_chat_id'],'--name',data['name'],'--as','bot','--format','json'])
                remote=run_cli(['lark-cli','--profile',binding['lark_profile'],'im','chats','get','--chat-id',binding['feishu_chat_id'],'--as','bot','--format','json']).get('data',{})
                actual=remote.get('name')
                if op=='rename-feishu' and actual!=data['name']:raise ValueError('飞书实际群名称核验不一致')
                sources=search_wechat(binding['wechat_talker'])
                source=next((r for r in sources if (r.get('username') or r.get('talker'))==binding['wechat_talker']),{})
                result=s.rename_binding(binding['id'],wechat_chat_name=source.get('display_name') or source.get('name'),feishu_chat_name=actual,actor=actor)
            else:raise ValueError('不支持的绑定操作')
        elif resource=='people':
            if op=='create':result=s.create_person(data['name'],data.get('notes',''))
            elif op=='rename':result=s.rename_person(int(data['id']),data['name'],actor)
            elif op=='link_wechat':result=s.link_wechat_identity(int(data['person_id']),data.get('source_account','default'),data['wechat_sender_id'],data.get('display_name',''),actor=actor)
            elif op=='reassign_wechat':
                if data.get('confirm') is not True:raise ValueError('请明确确认更正微信身份归属')
                result=s.reassign_wechat_identity(int(data['identity_id']),int(data['from_person_id']),int(data['to_person_id']),data.get('reason',''),actor=actor)
            elif op=='link_feishu':result=s.link_feishu_identity(int(data['person_id']),int(data['profile_id']),data['open_id'],data.get('user_id',''),data.get('display_name',''))
            elif op in {'archive','restore','delete','unlink_wechat','unlink_feishu'}:
                if data.get('confirm') is not True:raise ValueError('请明确确认人员维护操作')
                if op.startswith('unlink_'):
                    runtime=read_config(self.config_path)
                    result=s.unlink_identity(op.removeprefix('unlink_'),int(data.get('identity_id',data.get('id'))),int(data['person_id']),actor=actor,
                        protected_feishu=(str(runtime.get('admin_profile') or 'wechat-mirror'),str(runtime.get('admin_feishu_user_id') or '')))
                else:result=s.change_person_state(int(data['id']),op,actor=actor)
            else:raise ValueError('不支持的人员操作')
        elif resource=='confirmations' and op=='confirm':
            if data.get('person_id'):
                item=next(r for r in s.list_confirmations() if r['id']==int(data['id']))
                result=s.link_wechat_identity(int(data['person_id']),item['source_account'],item['wechat_sender_id'],item['display_name'],actor=actor)
            else:result=s.confirm_sender(int(data['id']),data['name'],actor)
        elif resource=='setup':
            if op=='save':
                draft=s.setting('setup_draft',{});draft.update({k:v for k,v in data.items() if k in SETUP_FIELDS})
                # After setup, operational settings must reach the runtime
                # config; otherwise the UI claims success but workers never see them.
                if s.list_bindings():
                    mutable={'admin_feishu_user_id','admin_profile','poll_interval','reader_wakeup'}
                    updates={k:v for k,v in data.items() if k in mutable}
                    if updates:
                        runtime=read_config(self.config_path)
                        runtime.update(updates)
                        admin_id=str(runtime.get('admin_feishu_user_id') or '').strip()
                        admin_profile=str(runtime.get('admin_profile') or 'wechat-mirror').strip()
                        if admin_id and (('admin_feishu_user_id' in updates) or ('admin_profile' in updates)):
                            profile=next((p for p in s.list_profiles() if p['profile_name']==admin_profile),None)
                            if not profile or not any(f['profile_id']==profile['id'] and f['open_id']==admin_id
                                                      for f in s.list_feishu_identities()):
                                raise ValueError('管理员身份尚未关联到提醒应用；请先在「人员」页关联并核对')
                        write_runtime_config(runtime,self.config_path)
                s.save_setting('setup_draft',draft);result=self.get('setup')
            elif op=='check':result=preflight_binding(s.setting('setup_draft',{}),s)
            elif op=='init':
                draft=s.setting('setup_draft',{}) or data.get('config') or {k:v for k,v in data.items() if k in SETUP_FIELDS}
                if s.list_bindings():raise ValueError('已初始化，请在群映射页面新建绑定')
                check=preflight_binding(draft,s)
                if not check['ok']:raise ValueError('; '.join(check['errors']))
                result=setup_init(self.config_path,s.path,draft)
            elif op=='reset':
                if not data.get('confirm'):raise ValueError('请确认重置引导草稿；已启动绑定和凭证不会删除')
                s.save_setting('setup_draft',{});result=self.get('setup')
            else:raise ValueError('不支持的初始化操作')
        elif resource=='wizard' and op=='share-link':
            profile=str(data.get('profile') or '').strip()
            chat_id=str(data.get('chat_id') or '').strip()
            if not profile or not chat_id:raise ValueError('请选择目标飞书群和发送应用')
            if not any(p['profile_name']==profile and p['enabled'] for p in s.list_profiles()):
                raise ValueError('发送应用未启用')
            result=chat_share_link(profile,chat_id)
        elif resource=='wizard' and op=='create-target':
            result=create_target(s,data,run_cli,chat_share_link)
        elif resource=='media-failures' and op=='retry':
            with s.connect() as db:
                row=db.execute('SELECT * FROM media_jobs WHERE id=?',(int(data['id']),)).fetchone()
                if not row or row['state']=='uploaded':raise ValueError('失败项不存在或已成功')
                if row['state'] not in {'pending','processing'}:
                    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(f"media_retry_base:{row['id']}",json.dumps(row['attempts'])))
                    db.execute("UPDATE media_jobs SET state='pending',updated_at=0 WHERE id=?",(row['id'],))
            result={'queued':True,'message':'已排队；绑定运行时处理，停止时保留待办'}
        elif resource=='uncertain' and op=='receipt':
            if not data.get('confirm'):raise ValueError('需要明确确认回执')
            if not str(data.get('remote_id','')).startswith('om_'):raise ValueError('请填写已核实的飞书消息 ID')
            with s.connect() as db:
                row=db.execute('SELECT d.*,m.binding_id FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE d.message_id=? AND d.part=?',(int(data['message_id']),data['part'])).fetchone()
            if not row or row['state'] not in {'uncertain','sending'}:raise ValueError('该部分没有待核对回执')
            binding=s.get_binding(row['binding_id'])
            remote=payload_items(run_cli(['lark-cli','--profile',binding['lark_profile'],'im','+messages-mget','--message-ids',data['remote_id'],'--no-reactions','--as','bot']),'messages')
            found=next((r for r in remote if r.get('message_id')==data['remote_id']),None)
            if not found or found.get('chat_id')!=binding['feishu_chat_id'] or found.get('deleted'):raise ValueError('回执消息不可用或不属于该目标群')
            sender=found.get('sender') or {}
            if sender.get('id')!=binding['lark_app_id']:raise ValueError('回执消息不是该发送应用发出的')
            args=json.loads(row['payload_json'])
            if '--text' in args and found.get('content')!=args[args.index('--text')+1]:raise ValueError('回执正文不一致，请核对原消息和表情转换')
            s.set_delivery(int(data['message_id']),data['part'],'sent',data['remote_id']);result={'confirmed':True}
        else:raise ValueError('不支持的管理操作')
        # Audit never captures Secret or arbitrary full request bodies.
        safe={k:v for k,v in data.items() if k in {'op','id','name','profile_name','person_id','message_id','part','new_name','remote_id'}}
        s.audit(resource+'.'+str(op),after=safe,actor=actor)
        return result


class Handler(BaseHTTPRequestHandler):
    manager:Manager
    token:str
    def log_message(self,*args):pass
    def send_body(self,body,status=200,mime='application/json; charset=utf-8'):
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(body)
    def send_json(self,value,status=200):self.send_body(json.dumps(value,ensure_ascii=False,default=str).encode(),status)
    def authorized(self):
        host=self.headers.get('Host','').split(':')[0]
        origin=self.headers.get('Origin')
        return host in {'localhost','127.0.0.1'} and (not origin or origin==f'http://{self.headers.get("Host")}') and secrets.compare_digest(self.headers.get('X-Mirror-Admin',''),self.token)
    def do_GET(self):
        url=urllib.parse.urlparse(self.path)
        static={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','application/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
        if url.path in static:
            name,mime=static[url.path];self.send_body((Path(__file__).parent/'web'/name).read_bytes(),mime=mime);return
        if not self.authorized():self.send_json({'error':'会话未授权，请从 Codex 提供的本机链接打开'},401);return
        try:self.send_json(self.manager.get(url.path.removeprefix('/api/'),{k:v[0] for k,v in urllib.parse.parse_qs(url.query).items()}))
        except Exception as exc:
            from .wechat_feishu_mirror import safe_error
            self.send_json({'error':safe_error(exc)},400)
    def do_POST(self):
        if not self.authorized():self.send_json({'error':'会话未授权'},401);return
        try:
            length=int(self.headers.get('Content-Length',0))
            if length>65536:raise ValueError('请求过大')
            data=json.loads(self.rfile.read(length) or b'{}')
            self.send_json(self.manager.post(urllib.parse.urlparse(self.path).path.removeprefix('/api/'),data,'web'))
        except Exception as exc:
            from .wechat_feishu_mirror import safe_error
            self.send_json({'error':safe_error(exc)},400)


def serve(store,config_path,host,port,token=None):
    if host not in {'127.0.0.1','localhost'}:raise ValueError('管理台只允许本机访问')
    token=token or secrets.token_urlsafe(24);manager=Manager(store,config_path)
    handler=type('LocalHandler',(Handler,),{'manager':manager,'token':token})
    server=ThreadingHTTPServer((host,port),handler)
    state={'url':f'http://{host}:{server.server_port}/#token={token}','pid':os.getpid(),'port':server.server_port,'token':token}
    state_file=store.path.parent/'manager-session.json';state_file.write_text(json.dumps(state),encoding='utf-8');state_file.chmod(0o600)
    print(json.dumps({'url':state['url'],'port':server.server_port}),flush=True)
    stopped=threading.Event()
    def reminders():
        from .wechat_feishu_mirror import notify_confirmation
        while not stopped.wait(30):
            try:
                config=read_config(config_path)
                for item in store.list_confirmations():
                    if stopped.is_set():return
                    notify_confirmation(config,store,item['source_account'],item['wechat_sender_id'])
            except Exception:
                pass  # The pending item retains its error for the console.
    threading.Thread(target=reminders,daemon=True).start()
    try:server.serve_forever()
    finally:stopped.set();server.server_close()


def main():
    for pipe in (sys.stdout, sys.stderr):
        if hasattr(pipe, 'reconfigure'):
            pipe.reconfigure(encoding='utf-8', errors='replace')
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,default=DEFAULT_CONFIG);p.add_argument('--db',type=Path)
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('profiles','bindings','people','confirmations','media-failures','uncertain','setup'):
        child=sub.add_parser(name);child.add_argument('action',nargs='?',default='list');child.add_argument('--op');child.add_argument('--id',type=int);child.add_argument('--data');child.add_argument('--name');child.add_argument('--profile-name');child.add_argument('--app-id');child.add_argument('--new-name');child.add_argument('--health',action='store_true');child.add_argument('--app-secret-stdin',action='store_true');child.add_argument('--query');child.add_argument('--binding-id',type=int);child.add_argument('--source-account')
    sub.add_parser('status');sv=sub.add_parser('serve');sv.add_argument('--port',type=int,default=0);sv.add_argument('--host',default='127.0.0.1')
    for child in sub.choices.values():
        child.add_argument('--config',default=argparse.SUPPRESS,type=Path);child.add_argument('--db',default=argparse.SUPPRESS,type=Path)
    args=p.parse_args();config=read_config(args.config)
    # Reader overrides from the runtime config apply to manager-side reader
    # calls (group search, preflight) the same way they do for workers.
    reader_override = str(config.get('wechat_reader') or '').strip()
    if reader_override:
        global READER
        os.environ['WECHAT_READER'] = reader_override
        READER = Path(reader_override)
    db=args.db or Path(config['sqlite_file']);store=MirrorStore(db);store.initialize(config);manager=Manager(store,args.config)
    if args.command=='serve':serve(store,args.config,args.host,args.port);return 0
    if args.command=='status':result=manager.get('status')
    else:
        op=args.op or args.action
        if args.health:op='health'
        data=json.loads(args.data) if args.data else {}
        for field in ('id','name','profile_name','app_id','new_name'):
            if getattr(args,field,None) is not None:data[field]=getattr(args,field)
        if args.app_secret_stdin:data['app_secret']=sys.stdin.readline().rstrip('\n')
        if op in {'list','status'}:result=manager.get(args.command)
        elif args.command=='people' and op=='search-feishu':
            result=manager.get('people/search-feishu',{'profile':data.get('profile_name',''),
                                                       'name':data.get('name',''),'q':args.query or data.get('query','')})
        elif args.command=='people' and op=='search-wechat':
            result=manager.get('people/search-wechat',{'binding_id':args.binding_id or data.get('binding_id'),
                'source_account':args.source_account or data.get('source_account',''),
                'q':args.query or data.get('query','')})
        else:
            data['op']={'check':'preflight' if args.command=='bindings' else 'check','link-wechat':'link_wechat','link-feishu':'link_feishu'}.get(op,op)
            if args.command=='setup' and op=='init' and not args.data:result=setup_init(args.config,store.path)
            else:result=manager.post(args.command,data)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
