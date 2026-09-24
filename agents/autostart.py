#!/usr/bin/env python3
"""Boot autostart: one Windows scheduled task restores the守护集 after logon.

The task action runs ``python -m agents.manage autostart`` once per logon.
That boot routine is idempotent: the console is only spawned when no live
session answers on its recorded port, the watchdog holds an exclusive
singleton lock, and binding starts are already idempotent under the
worker launch/lifetime locks. Stopped, paused and archived bindings are
never pulled up — only control-state 'running' bindings are restored.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "w2f-autostart"


def supported() -> bool:
    return os.name == "nt"


def pythonw_executable() -> Path:
    """Windowless interpreter for the task action; fall back to python.exe."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.is_file() else Path(sys.executable)


def boot_command(config_path, db_path) -> dict:
    """Scheduled-task action parts reproducing this deployment at logon."""
    args = ["-m", "agents.manage", "autostart",
            "--config", str(Path(config_path).resolve()),
            "--db", str(Path(db_path).resolve())]
    return {"execute": str(pythonw_executable()),
            "argument": subprocess.list2cmdline(args),
            "working_directory": str(ROOT)}


def _quote(value) -> str:
    # PowerShell single-quoted string: double embedded quotes.
    return str(value).replace("'", "''")


def _powershell(script: str) -> str:
    # PowerShell emits the OEM code page (GBK here) unless told otherwise;
    # errors='replace' keeps a mojibake diagnostic from killing the reader.
    prefixed = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + script
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", prefixed],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "计划任务操作失败").strip()[:400])
    return proc.stdout


def _task_state(out: str) -> dict:
    try:
        value = json.loads(out) if out.strip() else {}
    except ValueError:
        value = {}
    value.setdefault("registered", False)
    value["supported"] = True
    value.setdefault("task", TASK_NAME)
    return value


def task_register(config_path, db_path) -> dict:
    if not supported():
        raise ValueError("开机自启目前仅支持 Windows 计划任务")
    cmd = boot_command(config_path, db_path)
    # Concatenation, not str.format: PowerShell hash literals need their
    # single braces, which format-style doubling would mangle.
    script = ("$action=New-ScheduledTaskAction -Execute '" + _quote(cmd["execute"])
              + "' -Argument '" + _quote(cmd["argument"])
              + "' -WorkingDirectory '" + _quote(cmd["working_directory"]) + "';"
              # whoami resolves the real logon principal on local, domain and
              # Azure AD joined machines alike; $env:USERDOMAIN is the device
              # name on Azure AD joins and would fail SID lookup.
              "$trigger=New-ScheduledTaskTrigger -AtLogOn -User (whoami);"
              "Register-ScheduledTask -TaskName '" + _quote(TASK_NAME) + "' -Action $action -Trigger $trigger"
              " -Description 'w2f mirror: restore console, watchdog and running bindings at logon' -Force|Out-Null;"
              "ConvertTo-Json @{registered=$true}")
    _powershell(script)
    return {"registered": True, "task": TASK_NAME, **cmd}


def task_unregister() -> dict:
    if not supported():
        raise ValueError("开机自启目前仅支持 Windows 计划任务")
    # Absent task stays a success; anything else must surface instead of
    # being reported as unregistered: a stopped scheduler service or an
    # unreadable task must not masquerade as a successful removal.
    script = ("$svc=Get-Service Schedule;if($svc.Status -ne 'Running'){throw ('Task Scheduler 服务未运行: {0}' -f $svc.Status)};"
              "$t=Get-ScheduledTask -TaskName '" + _quote(TASK_NAME) + "' -ErrorAction SilentlyContinue;"
              "if($t){Unregister-ScheduledTask -TaskName '" + _quote(TASK_NAME) + "' -Confirm:$false -ErrorAction Stop};"
              "try{$t=Get-ScheduledTask -TaskName '" + _quote(TASK_NAME) + "' -ErrorAction Stop}catch{"
              # Not-found is localized (找不到任何…MSFT_ScheduledTask 对象 / No
              # … objects found); the CIM class name appears in both.
              "if($_.Exception.Message -notmatch 'MSFT_ScheduledTask'){throw};"
              "$t=$null};"
              "ConvertTo-Json @{registered=$($null -ne $t)}")
    # Report the post-unregister reality, not the intended outcome.
    return _task_state(_powershell(script))


def task_status() -> dict:
    if not supported():
        return {"registered": False, "supported": False}
    # Service down would read as "not registered"; surface it instead.
    script = ("$svc=Get-Service Schedule;if($svc.Status -ne 'Running'){throw ('Task Scheduler 服务未运行: {0}' -f $svc.Status)};"
              "$t=Get-ScheduledTask -TaskName '" + _quote(TASK_NAME) + "' -ErrorAction SilentlyContinue;"
              "if($t){ConvertTo-Json @{registered=$true;state=$t.State.ToString()}}"
              "else{ConvertTo-Json @{registered=$false}}")
    try:
        out = _powershell(script)
    except Exception as exc:
        return {"registered": False, "supported": True, "error": str(exc)[:200]}
    return _task_state(out)


def singleton_lock(path):
    """Open exclusive lock handle, or None when another process holds it."""
    from .wechat_feishu_mirror import _lock_exclusive
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        _lock_exclusive(handle, blocking=False)
    except OSError:
        handle.close()
        return None
    return handle


def console_session(store) -> dict | None:
    path = Path(store.path).parent / "manager-session.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and state.get("port") else None


def console_alive(session: dict) -> bool:
    # A direct opener: http_proxy must never route a loopback probe, or a
    # live console would look dead and every logon would spawn a second one.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(session['port'])}/", timeout=2) as response:
            return response.status == 200
    except Exception:
        # Includes http.client.HTTPException (e.g. BadStatusLine from a
        # non-HTTP service that ended up on the recorded port).
        return False


def spawn_detached(args, log_path, cwd=ROOT):
    """Start a long-running helper that outlives this process, logging to file."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with log_path.open("a", encoding="utf-8", errors="replace") as out:
        return subprocess.Popen(args, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, **kwargs)


def restore_running_bindings(store, config_path, start=None):
    """Start workers for bindings that were running before shutdown.

    Returns (restored_ids, {binding_id: error}); one failed start never
    stops the rest — the watchdog keeps retrying running bindings anyway.
    """
    if start is None:
        from .manage import binding_action
        start = lambda bid: binding_action(store, config_path, bid, "start", start_reason="boot restore")
    from .manage import binding_restorable
    restored, failed = [], {}
    for binding in store.list_bindings(False):
        bid = int(binding["id"])
        # Same rule as the watchdog: enabled, control running, worker dead.
        if not binding_restorable(store, binding):
            continue
        try:
            start(bid)
        except Exception as exc:
            failed[bid] = str(exc)[:200]
            store.audit("boot.restore", after={"binding_id": bid, "error": failed[bid]}, actor="autostart", binding_id=bid, result="failed")
            continue
        store.audit("boot.restore", after={"binding_id": bid}, actor="autostart", binding_id=bid)
        restored.append(bid)
    return restored, failed


def _log_dir(config_path, store) -> Path:
    try:
        from .paths import runtime_paths
        path = Path(config_path)
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        # Only an explicit config log_dir moves boot logs away from the data
        # directory; the default fallback must never touch the home dir.
        if config.get("log_dir"):
            return Path(runtime_paths(config)["log_dir"])
    except Exception:
        pass
    return Path(store.path).parent / "logs"


def run_boot(store, config_path, db_path, start=None, spawn=None, console_check=None) -> dict:
    """One idempotent boot pass: restore bindings, then console and watchdog.

    A restore crash must never keep the console and watchdog offline; only
    per-binding start failures are expected, everything else is recorded.
    """
    config_path = Path(config_path)
    restored, failed, boot_error = [], {}, None
    try:
        restored, failed = restore_running_bindings(store, config_path, start=start)
    except Exception as exc:
        boot_error = str(exc)[:200]
    log_dir = _log_dir(config_path, store)
    spawn = spawn or spawn_detached
    console_cmd = [sys.executable, "-m", "agents.manage", "serve",
                   "--config", str(config_path.resolve()), "--db", str(Path(db_path).resolve())]
    watchdog_cmd = [sys.executable, "-m", "agents.manage", "watchdog",
                    "--config", str(config_path.resolve()), "--db", str(Path(db_path).resolve())]
    session = console_session(store)
    try:
        alive = bool(session) and (console_check or console_alive)(session)
    except Exception:
        # A probe must never kill the boot pass: an unexpected exception here
        # would leave both console and watchdog offline (worse than a respawn).
        alive = False
    if not alive:
        # A second serve spawned in a race loses the console lock on startup
        # and exits; the lock is the final authority, the probe is a shortcut.
        spawn(console_cmd, log_dir / "manager.log")
    # The watchdog's singleton lock makes an overlapping second copy exit.
    spawn(watchdog_cmd, log_dir / "watchdog.log")
    summary = {"boot": "completed", "restored": restored, "failed": failed,
               "console": "already-running" if alive else "spawned", "watchdog": "spawned",
               "at": time.time()}
    if boot_error:
        summary["error"] = boot_error
    line = json.dumps(summary, ensure_ascii=False) + "\n"
    # No stdout print: under the task's pythonw it is a no-op, and the CLI
    # caller serializes the same summary itself.
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "autostart.log").open("a", encoding="utf-8", errors="replace") as out:
            out.write(line)
    except OSError:
        pass
    return summary
