#!/usr/bin/env python3
"""Local management CLI and web console for the mirror service.

The web server binds to loopback only. Credentials are passed to lark-cli via
stdin for profile onboarding and are never stored in this project's database.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from .storage import MirrorStore
    from .paths import CONFIG_DIR, DATA_DIR, DEFAULT_CONFIG as RUNTIME_CONFIG, EXAMPLE_CONFIG, config_path, runtime_paths, is_initialized
except ImportError:
    from storage import MirrorStore
    from paths import CONFIG_DIR, DATA_DIR, DEFAULT_CONFIG as RUNTIME_CONFIG, EXAMPLE_CONFIG, config_path, runtime_paths, is_initialized

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = RUNTIME_CONFIG
DEFAULT_DB = DATA_DIR / "wechat_feishu_mirror.sqlite3"
READER = Path(os.environ.get("WECHAT_READER", str(Path.home() / ".codex/skills/wechat-cli/scripts/reader.sh")))


def read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return runtime_paths(json.loads(path.read_text(encoding="utf-8")))


def write_runtime_config(value: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = dict(value)
    # The config is safe to publish only when it contains references, never
    # secrets.  lark-cli keeps App Secret and tokens in its own credential
    # store; setup rejects secret-like keys here.
    for key in ("app_secret", "app_secret_value", "access_token", "refresh_token"):
        clean.pop(key, None)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def setup_status(config_path: Path, db_path: Path) -> dict[str, Any]:
    cfg = read_config(config_path)
    return {
        "initialized": is_initialized(cfg, db_path),
        "config_path": str(config_path),
        "data_dir": str(Path(cfg.get("data_dir", DATA_DIR)).expanduser()),
        "database": str(db_path),
        "missing": [key for key in ("wechat_talker", "feishu_chat_id", "lark_profile") if not str(cfg.get(key) or "").strip()],
    }


def setup_init(config_path: Path, db_path: Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a resumable, non-secret runtime config.

    Interactive prompts are deliberately small.  A JSON payload is supported
    for Codex/UI automation and tests.  This command never starts a worker or
    changes a Feishu group.
    """
    current = read_config(config_path)
    values = dict(current)
    if payload:
        values.update({k: v for k, v in payload.items() if v is not None})
    else:
        prompts = (
            ("source_account", "微信账号标识", "default"),
            ("wechat_chat_name", "微信群名称", values.get("wechat_chat_name", "")),
            ("wechat_talker", "微信群 talker", values.get("wechat_talker", "")),
            ("feishu_chat_name", "飞书群名称", values.get("feishu_chat_name", "")),
            ("feishu_chat_id", "飞书群 chat_id", values.get("feishu_chat_id", "")),
            ("lark_profile", "lark-cli Profile 名称", values.get("lark_profile", "wechat-mirror")),
            ("lark_app_id", "飞书 App ID", values.get("lark_app_id", "")),
            ("admin_feishu_user_id", "管理员飞书 open_id（可留空）", values.get("admin_feishu_user_id", "")),
        )
        for key, label, default in prompts:
            suffix = f" [{default}]" if default else ""
            answer = input(f"{label}{suffix}: ").strip()
            values[key] = answer or default
    values = runtime_paths(values)
    if not values.get("wechat_talker") or not values.get("feishu_chat_id") or not values.get("lark_profile"):
        raise ValueError("尚未完成初始化：至少需要 wechat_talker、feishu_chat_id、lark_profile")
    if db_path.exists():
        backup = db_path.with_suffix(db_path.suffix + f".backup-{int(__import__('time').time())}")
        shutil.copy2(db_path, backup)
    write_runtime_config(values, config_path)
    store = MirrorStore(db_path)
    store.initialize(values)
    return setup_status(config_path, db_path)


def run_cli(argv: list[str], *, stdin: str | None = None) -> Any:
    env = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}
    proc = subprocess.run(argv, cwd=ROOT, input=stdin, text=True, capture_output=True, env=env)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"command failed: {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"output": proc.stdout.strip()}


def profile_list(store: MirrorStore) -> list[dict[str, Any]]:
    payload = run_cli(["lark-cli", "profile", "list"])
    remote = payload if isinstance(payload, list) else payload.get("data", payload.get("profiles", []))
    if not isinstance(remote, list):
        remote = []
    local_by_app = {str(x["app_id"]): x for x in store.list_profiles()}
    local_by_name = {str(x["profile_name"]): x for x in store.list_profiles()}
    result = []
    for item in remote:
        name = str(item.get("name") or item.get("profile") or item.get("appId") or "")
        app_id = str(item.get("appId") or item.get("app_id") or "")
        same_name = local_by_name.get(name)
        if same_name and str(same_name.get("app_id")) != app_id:
            # A profile name reused for another application must not silently
            # redirect an existing binding to the replacement app.
            with store.connect() as db:
                rows = db.execute("SELECT id FROM bindings WHERE lark_profile=? AND lark_app_id<>? AND archived=0", (name, app_id)).fetchall()
            for row in rows:
                store.set_control(int(row[0]), "stopped", actor="manager", reason="Profile App ID changed; review binding")
                with store.connect() as db:
                    db.execute("UPDATE bindings SET enabled=0,updated_at=? WHERE id=?", (__import__("time").time(), int(row[0])))
            same_name = store.upsert_profile(name, app_id, protected=bool(same_name.get("protected")))
            local_by_name[name] = same_name
        local = local_by_app.get(app_id)
        if local and local["profile_name"] != name:
            with store.connect() as db:
                db.execute("UPDATE profiles SET profile_name=?,updated_at=? WHERE id=?", (name, __import__("time").time(), local["id"]))
                db.execute("UPDATE bindings SET lark_profile=? WHERE lark_app_id=?", (name, app_id))
            local["profile_name"] = name
        elif not local and name and app_id:
            local = store.upsert_profile(name, app_id, protected=name in {"feishu-mcp", "cli_example_common_app"})
        if local:
            result.append({**local, "active": bool(item.get("active", False)), "cli_user": item.get("user"), "token_status": item.get("tokenStatus")})
    remote_apps = {str(item.get("appId") or item.get("app_id") or "") for item in remote}
    for local in store.list_profiles():
        if str(local.get("app_id")) not in remote_apps:
            store.update_profile_health(local["profile_name"], bot_status="missing", user_status="missing", error="Profile 不存在于 lark-cli")
    return result


def profile_health(store: MirrorStore, name: str) -> dict[str, Any]:
    try:
        auth = run_cli(["lark-cli", "--profile", name, "auth", "status", "--json", "--verify"])
        user_status = "ready" if auth.get("verified") else "needs_attention"
        auth_error = None
    except Exception as exc:
        auth = {}
        user_status = "error"
        auth_error = str(exc)[:500]
    try:
        run_cli(["lark-cli", "--profile", name, "im", "+chat-list", "--as", "bot", "--format", "json", "--page-size", "1"])
        bot_status = "ready"
    except Exception as exc:
        bot_status = "error"
        auth_error = auth_error or str(exc)[:500]
    store.update_profile_health(name, bot_status=bot_status, user_status=user_status, error=auth_error)
    return {"profile": name, "bot_status": bot_status, "user_status": user_status, "error": auth_error}


def binding_action(store: MirrorStore, config_path: Path, binding_id: int, action: str) -> Any:
    if action in {"start", "stop", "pause", "resume", "status"}:
        argv = [sys.executable, str(ROOT / "agents" / "wechat_feishu_mirror.py"), action, "--config", str(config_path), "--binding-id", str(binding_id)]
        if action in {"start", "stop", "pause", "resume"}:
            return run_local(argv)
        return json.loads(subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, check=True).stdout)
    if action == "archive":
        store.archive_binding(binding_id)
        return store.get_binding(binding_id)
    raise ValueError(f"unknown binding action: {action}")


def run_local(argv: list[str]) -> Any:
    proc = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"output": proc.stdout.strip()}


def search_wechat(query: str) -> Any:
    return run_command([str(READER), "resolve-chat", query, "--type-filter", "group", "--limit", "20"])


def search_feishu(profile: str, query: str) -> Any:
    return run_cli(["lark-cli", "--profile", profile, "im", "+chat-search", "--as", "bot", "--query", query, "--format", "json"])


def run_command(argv: list[str]) -> Any:
    proc = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return json.loads(proc.stdout)


def result_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>微信群同步管理台</title><style>
body{font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f5f6f8;color:#1f2329}header{background:#fff;padding:18px 28px;border-bottom:1px solid #ddd}main{max-width:1180px;margin:20px auto;padding:0 18px}.tabs{display:flex;gap:8px;margin-bottom:14px}.tabs button{border:0;background:#e5e6eb;padding:9px 16px;border-radius:6px;cursor:pointer}.tabs button.active{background:#3370ff;color:#fff}.panel{display:none;background:#fff;padding:18px;border-radius:8px;box-shadow:0 1px 3px #0001}.panel.active{display:block}.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}input,select{padding:8px;border:1px solid #bbb;border-radius:5px}button{padding:8px 12px;border:1px solid #bbb;border-radius:5px;background:#fff;cursor:pointer}button.primary{background:#3370ff;color:#fff;border-color:#3370ff}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{text-align:left;border-bottom:1px solid #eee;padding:9px 6px;vertical-align:top}.muted{color:#777}.danger{color:#d93025}
</style></head><body><header><b>微信群同步管理台</b><span class="muted">　Profile / 群映射 / 人员</span></header><main><div class="tabs"><button data-tab="profiles">飞书应用</button><button data-tab="bindings">群映射</button><button data-tab="people">人员</button></div><section id="profiles" class="panel"><div class="row"><button class="primary" onclick="syncProfiles()">同步 Profile</button><button onclick="addProfile()">接入已有应用</button></div><table><thead><tr><th>Profile</th><th>App ID</th><th>状态</th><th>操作</th></tr></thead><tbody id="profilesRows"></tbody></table></section><section id="bindings" class="panel"><div class="row"><button class="primary" onclick="newBinding()">新建群映射</button><button onclick="loadBindings()">刷新</button></div><table><thead><tr><th>微信群</th><th>飞书群</th><th>Profile</th><th>状态</th><th>操作</th></tr></thead><tbody id="bindingsRows"></tbody></table></section><section id="people" class="panel"><div class="row"><button class="primary" onclick="newPerson()">新建人员</button><button onclick="loadPeople()">刷新</button></div><table><thead><tr><th>姓名</th><th>微信身份数</th><th>飞书身份数</th><th>操作</th></tr></thead><tbody id="peopleRows"></tbody></table><h3>待确认身份</h3><table><thead><tr><th>编号</th><th>昵称</th><th>微信 ID</th><th>操作</th></tr></thead><tbody id="confirmRows"></tbody></table></section></main><script>
const token=new URLSearchParams(location.search).get('token')||'';const api=(p,o={})=>fetch(p,{...o,headers:{'Content-Type':'application/json','X-Mirror-Admin':token,...(o.headers||{})}}).then(async r=>{let x=await r.json();if(!r.ok)throw Error(x.error||'请求失败');return x});
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));document.getElementById(b.dataset.tab).classList.add('active');if(b.dataset.tab==='profiles')loadProfiles();if(b.dataset.tab==='bindings')loadBindings();if(b.dataset.tab==='people')loadPeople()});document.querySelector('[data-tab="profiles"]').click();
async function loadProfiles(){let x=await api('/api/profiles');profilesRows.innerHTML=x.map(p=>`<tr><td>${p.profile_name}</td><td>${p.app_id}</td><td>${p.bot_status||'unknown'} / ${p.user_status||'unknown'}${p.protected?'（受保护）':''}</td><td><button onclick="health('${p.profile_name}')">检查</button></td></tr>`).join('')||'<tr><td colspan="4">暂无 Profile</td></tr>'}async function syncProfiles(){await api('/api/profiles',{method:'POST',body:JSON.stringify({op:'sync'})});loadProfiles()}async function health(p){await api('/api/profiles',{method:'POST',body:JSON.stringify({op:'health',profile_name:p})});loadProfiles()}async function addProfile(){let n=prompt('Profile 名称');if(!n)return;let a=prompt('App ID');if(!a)return;let s=prompt('App Secret（仅传给 lark-cli，不保存）');if(s===null)return;await api('/api/profiles',{method:'POST',body:JSON.stringify({op:'add',profile_name:n,app_id:a,app_secret:s})});loadProfiles()}
async function loadBindings(){let x=await api('/api/bindings');bindingsRows.innerHTML=x.map(b=>`<tr><td>${b.wechat_chat_name}<br><small>${b.wechat_talker}</small></td><td>${b.feishu_chat_name}<br><small>${b.feishu_chat_id}</small></td><td>${b.lark_profile}</td><td>${b.control_status||'stopped'} / ${b.worker_alive?'运行中':'已停止'}${b.archived?'（已归档）':''}</td><td><button onclick="baction(${b.id},'start')">启动</button><button onclick="baction(${b.id},'pause')">暂停</button><button onclick="baction(${b.id},'resume')">恢复</button><button onclick="baction(${b.id},'stop')">停止</button><button onclick="baction(${b.id},'archive')">归档</button></td></tr>`).join('')||'<tr><td colspan="5">暂无映射</td></tr>'}async function baction(id,op){await api('/api/bindings',{method:'POST',body:JSON.stringify({op,id})});loadBindings()}async function newBinding(){let w=prompt('微信群名');if(!w)return;let t=prompt('微信 talker');if(!t)return;let f=prompt('飞书群名');if(!f)return;let c=prompt('飞书 chat_id');if(!c)return;let p=prompt('Profile 名称','wechat-mirror');if(!p)return;await api('/api/bindings',{method:'POST',body:JSON.stringify({op:'create',wechat_chat_name:w,wechat_talker:t,feishu_chat_name:f,feishu_chat_id:c,lark_profile:p})});loadBindings()}
async function loadPeople(){let x=await api('/api/people');peopleRows.innerHTML=x.map(p=>`<tr><td>${p.confirmed_name}</td><td>${p.wechat_identity_count}</td><td>${p.feishu_identity_count}</td><td><button onclick="renamePerson(${p.id},'${p.confirmed_name.replaceAll("'","\\'")}')">改名</button></td></tr>`).join('')||'<tr><td colspan="4">暂无人员</td></tr>';let c=await api('/api/confirmations');confirmRows.innerHTML=c.map(x=>`<tr><td>${x.id}</td><td>${x.display_name}</td><td>${x.wechat_sender_id}</td><td><button onclick="confirmPerson(${x.id})">确认姓名</button></td></tr>`).join('')||'<tr><td colspan="4">暂无待确认身份</td></tr>'}async function newPerson(){let n=prompt('实名');if(n)await api('/api/people',{method:'POST',body:JSON.stringify({op:'create',name:n})});loadPeople()}async function renamePerson(id,old){let n=prompt('新姓名',old);if(n)await api('/api/people',{method:'POST',body:JSON.stringify({op:'rename',id,name:n})});loadPeople()}async function confirmPerson(id){let n=prompt('确认实名');if(n)await api('/api/confirmations',{method:'POST',body:JSON.stringify({op:'confirm',id,name:n})});loadPeople()}
</script></body></html>'''

# Keep the prototype markup compact while layering the v0.3 safety/status
# affordances without making the server depend on a frontend build step.
HTML = HTML.replace('</header><main>', '</header><main><div id="setupNotice" class="muted"></div>')
HTML = HTML.replace("<button onclick=\"health('${p.profile_name}')\">检查</button>", "<button onclick=\"health('${p.profile_name}')\">检查</button><button onclick=\"toggleProfile('${p.profile_name}',${p.enabled?'false':'true'})\">${p.enabled?'停用':'启用'}</button>")
HTML = HTML.replace("<button onclick=\"baction(${b.id},'archive')\">归档</button>", "<button onclick=\"renameBinding(${b.id})\">改显示名</button><button onclick=\"renameFeishu(${b.id})\">改飞书群名</button><button onclick=\"baction(${b.id},'archive')\">归档</button>")
HTML = HTML.replace('</script></body></html>', """async function loadSetup(){try{let x=await api('/api/setup');setupNotice.textContent=x.initialized?'运行数据已初始化：'+x.data_dir:'尚未完成初始化，请先运行 setup init；不会自动启动 Worker'}catch(e){setupNotice.textContent='初始化状态读取失败：'+e.message}}async function toggleProfile(name,enabled){if(!enabled&&!confirm('停用会停止该 Profile 的有效绑定，继续？'))return;await api('/api/profiles',{method:'POST',body:JSON.stringify({op:enabled?'enable':'disable',profile_name:name})});loadProfiles();loadBindings()}async function renameBinding(id){let w=prompt('新的微信群显示名');let f=prompt('新的飞书群显示名');if(w===null&&f===null)return;await api('/api/bindings',{method:'POST',body:JSON.stringify({op:'rename',id,wechat_chat_name:w,feishu_chat_name:f})});loadBindings()}async function renameFeishu(id){let n=prompt('新的飞书真实群名');if(!n||!confirm('确认修改飞书群真实名称？'))return;await api('/api/bindings',{method:'POST',body:JSON.stringify({op:'rename-feishu',id,name:n,confirm:true})});loadBindings()}loadSetup();</script></body></html>""")


class Handler(BaseHTTPRequestHandler):
    store: MirrorStore
    config_path: Path
    token: str

    def log_message(self, *_args: Any) -> None:
        return

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Mirror-Admin", ""), self.token)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if not self.authorized(): self.send_json({"error": "unauthorized"}, 401); return
        try:
            if parsed.path == "/api/profiles": self.send_json(profile_list(self.store))
            elif parsed.path == "/api/bindings": self.send_json(self.store.list_bindings())
            elif parsed.path == "/api/people": self.send_json(self.store.list_people())
            elif parsed.path == "/api/confirmations": self.send_json(self.store.list_confirmations())
            elif parsed.path == "/api/setup": self.send_json(setup_status(self.config_path, Path(self.store.path)))
            elif parsed.path == "/api/status": self.send_json({"bindings": self.store.list_bindings(), "confirmations": self.store.list_confirmations()})
            elif parsed.path == "/api/media-failures": self.send_json(self.store.list_media_failures())
            elif parsed.path == "/api/search/wechat": self.send_json(search_wechat(urllib.parse.parse_qs(parsed.query).get("q", [""])[0]))
            elif parsed.path == "/api/search/feishu": self.send_json(search_feishu(urllib.parse.parse_qs(parsed.query).get("profile", [""])[0], urllib.parse.parse_qs(parsed.query).get("q", [""])[0]))
            else: self.send_json({"error": "not found"}, 404)
        except Exception as exc: self.send_json({"error": str(exc)}, 500)

    def body(self) -> dict[str, Any]:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

    def do_POST(self) -> None:
        if not self.authorized(): self.send_json({"error": "unauthorized"}, 401); return
        try:
            data = self.body(); path = urllib.parse.urlparse(self.path).path
            if path == "/api/profiles":
                op = data.get("op")
                if op == "sync": result = profile_list(self.store)
                elif op == "health": result = profile_health(self.store, data["profile_name"])
                elif op == "add":
                    secret = data.pop("app_secret", "")
                    run_cli(["lark-cli", "profile", "add", "--name", data["profile_name"], "--app-id", data["app_id"], "--app-secret-stdin"], stdin=secret + "\n")
                    result = profile_list(self.store)
                elif op == "rename":
                    result = run_cli(["lark-cli", "profile", "rename", data["old_name"], data["new_name"]]); result = profile_list(self.store)
                elif op in {"disable", "enable"}:
                    result = self.store.set_profile_enabled(data["profile_name"], op == "enable")
                elif op == "remove":
                    p = next((x for x in self.store.list_profiles() if x["profile_name"] == data["profile_name"]), None)
                    if p and p["protected"]: raise ValueError("受保护 Profile 不能删除")
                    if self.store.profile_has_bindings(data["profile_name"]): raise ValueError("存在有效群绑定，不能删除")
                    result = run_cli(["lark-cli", "profile", "remove", data["profile_name"]]); result = profile_list(self.store)
                else: raise ValueError("unsupported profile operation")
            elif path == "/api/setup":
                if data.get("op") != "init":
                    raise ValueError("unsupported setup operation")
                result = setup_init(self.config_path, Path(self.store.path), data.get("config"))
            elif path == "/api/bindings":
                op = data.get("op")
                if op == "create":
                    if len([x for x in self.store.list_bindings(False) if not x["archived"]]) >= 10: raise ValueError("最多支持 10 组有效绑定")
                    result = self.store.create_binding(data)
                elif op == "rename":
                    result = self.store.rename_binding(int(data["id"]), wechat_chat_name=data.get("wechat_chat_name"), feishu_chat_name=data.get("feishu_chat_name"))
                elif op == "rename-feishu":
                    if not data.get("confirm"): raise ValueError("修改飞书群名称需要 confirm=true")
                    binding = self.store.get_binding(int(data["id"]))
                    if not binding: raise ValueError("群绑定不存在")
                    run_cli(["lark-cli", "--profile", binding["lark_profile"], "im", "+chat-update", "--as", "bot", "--chat-id", binding["feishu_chat_id"], "--name", data["name"], "--format", "json"])
                    result = self.store.rename_binding(int(data["id"]), feishu_chat_name=data["name"])
                elif op in {"start", "stop", "pause", "resume", "status", "archive"}: result = binding_action(self.store, self.config_path, int(data["id"]), op)
                else: raise ValueError("unsupported binding operation")
            elif path == "/api/people":
                op = data.get("op")
                if op == "create": result = self.store.create_person(data["name"], data.get("notes", ""))
                elif op == "rename": result = self.store.rename_person(int(data["id"]), data["name"])
                elif op == "link_wechat": result = self.store.link_wechat_identity(int(data["person_id"]), data.get("source_account", "default"), data["wechat_sender_id"], data.get("display_name", ""))
                elif op == "link_feishu": result = self.store.link_feishu_identity(int(data["person_id"]), int(data["profile_id"]), data["open_id"], data.get("user_id", ""), data.get("display_name", ""))
                else: raise ValueError("unsupported people operation")
            elif path == "/api/confirmations" and data.get("op") == "confirm": result = self.store.confirm_sender(int(data["id"]), data["name"])
            else: raise ValueError("not found")
            self.send_json(result)
        except Exception as exc: self.send_json({"error": str(exc)}, 400)


def serve(store: MirrorStore, config_path: Path, host: str, port: int) -> None:
    token = secrets.token_urlsafe(24)
    handler = type("MirrorHandler", (Handler,), {"store": store, "config_path": config_path, "token": token})
    server = ThreadingHTTPServer((host, port), handler)
    print(json.dumps({"url": f"http://{host}:{server.server_port}/?token={token}", "host": host, "port": server.server_port}, ensure_ascii=False), flush=True)
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="WeChat Feishu mirror management")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("profiles"); pr.add_argument("--health", action="store_true"); pr.add_argument("--op", choices=("sync", "add", "health", "rename", "remove", "disable", "enable"), default="sync"); pr.add_argument("--profile-name"); pr.add_argument("--app-id"); pr.add_argument("--app-secret"); pr.add_argument("--old-name"); pr.add_argument("--new-name")
    b = sub.add_parser("bindings"); b.add_argument("action", choices=("list", "create", "start", "stop", "pause", "resume", "status", "archive")); b.add_argument("--id", type=int); b.add_argument("--data", help="JSON binding payload for create")
    p = sub.add_parser("people"); p.add_argument("action", choices=("list", "create", "rename", "confirm", "link-wechat", "link-feishu")); p.add_argument("--id", type=int); p.add_argument("--name"); p.add_argument("--data", help="JSON people payload")
    sub.add_parser("status")
    setup = sub.add_parser("setup", help="检查或初始化本机运行数据")
    setup.add_argument("action", choices=("status", "init", "check", "reset"), nargs="?", default="status")
    setup.add_argument("--data", help="非交互 JSON 配置，不含 Secret")
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=0)
    # Accept global paths after the subcommand too, which is the natural form
    # when Codex invokes commands such as ``manage status --config ...``.
    for child in sub.choices.values():
        child.add_argument("--db", dest="db", default=argparse.SUPPRESS)
        child.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    args = parser.parse_args()
    db = Path(args.db)
    db = db if db.is_absolute() else ROOT / db
    config_path = Path(args.config)
    config_path = config_path if config_path.is_absolute() else ROOT / config_path
    config = read_config(config_path)
    config.setdefault("sqlite_file", str(db))
    config = runtime_paths(config)
    db = Path(config.get("sqlite_file", db)).expanduser()
    seed_config = config if all(str(config.get(key) or "").strip() for key in ("wechat_talker", "feishu_chat_id", "lark_profile")) else {}
    store = MirrorStore(db); store.initialize(seed_config)
    if args.command == "setup":
        if args.action == "status":
            result = setup_status(config_path, db)
        elif args.action == "check":
            result = setup_status(config_path, db)
            if not result["initialized"]:
                raise ValueError("初始化未完成：" + ", ".join(result["missing"]))
        elif args.action == "init":
            payload = json.loads(args.data) if args.data else None
            result = setup_init(config_path, db, payload)
        else:
            if db.exists():
                backup = db.with_suffix(db.suffix + f".backup-{int(__import__('time').time())}")
                db.replace(backup)
            result = {"reset": True, **setup_status(config_path, db)}
    elif args.command == "profiles":
        if args.op == "sync": result = profile_list(store)
        elif args.op == "health": result = profile_health(store, args.profile_name)
        elif args.op == "add":
            if not all((args.profile_name, args.app_id, args.app_secret is not None)): raise ValueError("add 需要 --profile-name、--app-id、--app-secret")
            run_cli(["lark-cli", "profile", "add", "--name", args.profile_name, "--app-id", args.app_id, "--app-secret-stdin"], stdin=args.app_secret + "\n")
            result = profile_list(store)
        elif args.op == "rename":
            if not args.old_name or not args.new_name: raise ValueError("rename 需要 --old-name 和 --new-name")
            run_cli(["lark-cli", "profile", "rename", args.old_name, args.new_name]); result = profile_list(store)
        elif args.op in {"disable", "enable"}:
            if not args.profile_name: raise ValueError(f"{args.op} 需要 --profile-name")
            result = store.set_profile_enabled(args.profile_name, args.op == "enable")
        else:
            if not args.profile_name: raise ValueError("remove 需要 --profile-name")
            p0 = next((x for x in store.list_profiles() if x["profile_name"] == args.profile_name), None)
            if p0 and p0["protected"]: raise ValueError("受保护 Profile 不能删除")
            if store.profile_has_bindings(args.profile_name): raise ValueError("存在有效群绑定，不能删除")
            run_cli(["lark-cli", "profile", "remove", args.profile_name]); result = profile_list(store)
        if args.health and args.op == "sync": result = [profile_health(store, x["profile_name"]) for x in result]
    elif args.command == "bindings":
        if args.action == "list": result = store.list_bindings()
        elif args.action == "create":
            if not args.data: raise ValueError("create 需要 --data JSON")
            result = store.create_binding(json.loads(args.data))
        else: result = binding_action(store, config_path, args.id or 1, args.action)
    elif args.command == "people":
        if args.action == "list": result = {"people": store.list_people(), "wechat": store.list_wechat_identities(), "feishu": store.list_feishu_identities(), "confirmations": store.list_confirmations()}
        elif args.action == "create":
            data = json.loads(args.data) if args.data else {"name": args.name or ""}; result = store.create_person(data["name"], data.get("notes", ""))
        elif args.action == "rename":
            data = json.loads(args.data) if args.data else {"id": args.id, "name": args.name}; result = store.rename_person(int(data["id"]), data["name"])
        elif args.action == "confirm":
            data = json.loads(args.data) if args.data else {"id": args.id, "name": args.name}; result = store.confirm_sender(int(data["id"]), data["name"])
        elif args.action == "link-wechat":
            data = json.loads(args.data); result = store.link_wechat_identity(int(data["person_id"]), data.get("source_account", "default"), data["wechat_sender_id"], data.get("display_name", ""))
        else:
            data = json.loads(args.data); result = store.link_feishu_identity(int(data["person_id"]), int(data["profile_id"]), data["open_id"], data.get("user_id", ""), data.get("display_name", ""))
    elif args.command == "status": result = {"bindings": store.list_bindings(), "profiles": store.list_profiles(), "confirmations": store.list_confirmations()}
    else: serve(store, config_path, args.host, args.port); return 0
    result_json(result); return 0


if __name__ == "__main__": raise SystemExit(main())
