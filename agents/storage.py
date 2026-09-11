"""SQLite control and delivery state for the WeChat -> Feishu mirror."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class MirrorStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def initialize(self, config: dict[str, Any] | None = None) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    wechat_chat_name TEXT NOT NULL,
                    wechat_talker TEXT NOT NULL UNIQUE,
                    feishu_chat_name TEXT NOT NULL,
                    feishu_chat_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_control (
                    binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),
                    status TEXT NOT NULL CHECK(status IN ('running','paused','stopped')),
                    reason TEXT,
                    actor TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_state (
                    binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),
                    cursor INTEGER NOT NULL DEFAULT 0,
                    last_success_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_leases (
                    binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),
                    pid INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    stopped_at REAL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    binding_id INTEGER NOT NULL REFERENCES bindings(id),
                    wechat_message_key TEXT NOT NULL,
                    local_id INTEGER,
                    server_id TEXT,
                    sender_wxid TEXT,
                    sender_name TEXT,
                    kind TEXT NOT NULL,
                    payload_json TEXT,
                    state TEXT NOT NULL CHECK(state IN ('received','processing','sent','placeholder','failed','pending')),
                    feishu_message_id TEXT,
                    idempotency_key TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(binding_id, wechat_message_key)
                );
                CREATE INDEX IF NOT EXISTS messages_state_idx ON messages(binding_id, state, updated_at);
                CREATE TABLE IF NOT EXISTS sender_mappings (
                    binding_id INTEGER NOT NULL REFERENCES bindings(id),
                    wechat_sender_id TEXT NOT NULL,
                    wechat_display_name TEXT,
                    confirmed_name TEXT,
                    feishu_open_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('confirmed','pending','rejected')),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(binding_id, wechat_sender_id)
                );
                CREATE TABLE IF NOT EXISTS media_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL REFERENCES messages(id),
                    media_type TEXT NOT NULL,
                    source_ref TEXT,
                    staging_path TEXT,
                    state TEXT NOT NULL CHECK(state IN ('pending','processing','uploaded','placeholder','manual','failed')),
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS media_jobs_state_idx ON media_jobs(state, updated_at);
                CREATE TABLE IF NOT EXISTS management_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    binding_id INTEGER REFERENCES bindings(id),
                    details_json TEXT,
                    result TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            db.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            if config:
                now = time.time()
                db.execute(
                    """INSERT INTO bindings(id,wechat_chat_name,wechat_talker,feishu_chat_name,feishu_chat_id,created_at,updated_at)
                       VALUES(1,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET wechat_chat_name=excluded.wechat_chat_name,
                       wechat_talker=excluded.wechat_talker,feishu_chat_name=excluded.feishu_chat_name,
                       feishu_chat_id=excluded.feishu_chat_id,updated_at=excluded.updated_at""",
                    (config.get("wechat_chat_name", ""), config["wechat_talker"], config.get("feishu_chat_name", ""), config["feishu_chat_id"], now, now),
                )
                db.execute(
                    "INSERT INTO runtime_control(binding_id,status,updated_at) VALUES(1,'stopped',?) ON CONFLICT(binding_id) DO NOTHING",
                    (now,),
                )
                db.execute(
                    "INSERT INTO runtime_state(binding_id,cursor,updated_at) VALUES(1,?,?) ON CONFLICT(binding_id) DO NOTHING",
                    (int(config.get("initial_cursor", 0)), now),
                )
                for sender_id, name in config.get("sender_map", {}).items():
                    db.execute(
                        """INSERT INTO sender_mappings(binding_id,wechat_sender_id,wechat_display_name,confirmed_name,status,updated_at)
                           VALUES(1,?,?,?,'confirmed',?)
                           ON CONFLICT(binding_id,wechat_sender_id) DO UPDATE SET confirmed_name=excluded.confirmed_name,status='confirmed',updated_at=excluded.updated_at""",
                        (sender_id, name, name, now),
                    )

    def binding_id(self) -> int:
        return 1

    def get_control(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runtime_control WHERE binding_id=1").fetchone()
        return dict(row) if row else {"binding_id": 1, "status": "stopped", "reason": None, "actor": None, "updated_at": 0}

    def get_cursor(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT cursor FROM runtime_state WHERE binding_id=1").fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, cursor: int) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO runtime_state(binding_id,cursor,last_success_at,updated_at) VALUES(1,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET cursor=MAX(runtime_state.cursor,excluded.cursor),last_success_at=excluded.last_success_at,updated_at=excluded.updated_at", (int(cursor), time.time(), time.time()))

    def set_control(self, status: str, actor: str = "codex", reason: str | None = None) -> None:
        now = time.time()
        with self.connect() as db:
            db.execute(
                """INSERT INTO runtime_control(binding_id,status,reason,actor,updated_at) VALUES(1,?,?,?,?)
                   ON CONFLICT(binding_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,actor=excluded.actor,updated_at=excluded.updated_at""",
                (status, reason, actor, now),
            )
            db.execute("INSERT INTO management_actions(action,actor,binding_id,details_json,result,created_at) VALUES(?,?,?,?,?,?)", (status, actor, 1, json.dumps({"reason": reason}, ensure_ascii=False), "accepted", now))

    def acquire_lease(self, pid: int | None = None) -> str:
        pid = pid or os.getpid()
        token = str(uuid.uuid4())
        now = time.time()
        with self.connect() as db:
            row = db.execute("SELECT pid,token,heartbeat_at,stopped_at FROM worker_leases WHERE binding_id=1").fetchone()
            if row and row[3] is None and now - float(row[2]) < 20:
                raise RuntimeError(f"已有同步 worker 运行中（pid={row[0]}）")
            db.execute("INSERT INTO worker_leases(binding_id,pid,token,started_at,heartbeat_at,stopped_at) VALUES(1,?,?,?,?,NULL) ON CONFLICT(binding_id) DO UPDATE SET pid=excluded.pid,token=excluded.token,started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,stopped_at=NULL", (pid, token, now, now))
        return token

    def heartbeat(self, token: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE worker_leases SET heartbeat_at=? WHERE binding_id=1 AND token=?", (time.time(), token))

    def release_lease(self, token: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE worker_leases SET stopped_at=?,heartbeat_at=? WHERE binding_id=1 AND token=?", (time.time(), time.time(), token))

    def get_lease(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM worker_leases WHERE binding_id=1").fetchone()
        return dict(row) if row else None

    def upsert_message(self, key: str, row: dict[str, Any], kind: str, state: str = "received", payload: dict[str, Any] | None = None) -> int:
        now = time.time()
        safe_payload = payload or {k: row.get(k) for k in ("talker", "local_id", "server_id", "sender_wxid", "sender", "kind_name", "text", "content", "time") if k in row}
        with self.connect() as db:
            db.execute(
                """INSERT INTO messages(binding_id,wechat_message_key,local_id,server_id,sender_wxid,sender_name,kind,payload_json,state,created_at,updated_at)
                   VALUES(1,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(binding_id,wechat_message_key) DO UPDATE SET updated_at=excluded.updated_at""",
                (key, row.get("local_id"), str(row.get("server_id") or ""), row.get("sender_wxid"), row.get("sender"), kind, json.dumps(safe_payload, ensure_ascii=False, default=str), state, now, now),
            )
            found = db.execute("SELECT id FROM messages WHERE binding_id=1 AND wechat_message_key=?", (key,)).fetchone()
        return int(found[0])

    def message_state(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT state FROM messages WHERE binding_id=1 AND wechat_message_key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def mark_message(self, key: str, state: str, *, feishu_message_id: str | None = None, idempotency_key: str | None = None, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE messages SET state=?,feishu_message_id=COALESCE(?,feishu_message_id),idempotency_key=COALESCE(?,idempotency_key),last_error=?,attempts=attempts+1,updated_at=? WHERE binding_id=1 AND wechat_message_key=?", (state, feishu_message_id, idempotency_key, error, time.time(), key))

    def add_media_job(self, message_id: int, media_type: str, source_ref: str | None, staging_path: str | None, state: str = "pending", error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO media_jobs(message_id,media_type,source_ref,staging_path,state,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (message_id, media_type, source_ref, staging_path, state, error, time.time(), time.time()))

    def record_sender(self, sender_id: str, display_name: str, confirmed_name: str | None, status: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO sender_mappings(binding_id,wechat_sender_id,wechat_display_name,confirmed_name,status,updated_at) VALUES(1,?,?,?,?,?) ON CONFLICT(binding_id,wechat_sender_id) DO UPDATE SET wechat_display_name=excluded.wechat_display_name,confirmed_name=excluded.confirmed_name,status=excluded.status,updated_at=excluded.updated_at", (sender_id, display_name, confirmed_name, status, time.time()))

    def status(self) -> dict[str, Any]:
        control = self.get_control()
        lease = self.get_lease()
        with self.connect() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM messages WHERE binding_id=1 GROUP BY state")}
            media_failed = db.execute("SELECT COUNT(*) FROM media_jobs WHERE state IN ('failed','manual','placeholder')").fetchone()[0]
            pending_senders = db.execute("SELECT COUNT(*) FROM sender_mappings WHERE binding_id=1 AND status='pending'").fetchone()[0]
            last = db.execute("SELECT local_id,sender_name,kind,state,updated_at FROM messages WHERE binding_id=1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        alive = False
        if lease and lease.get("stopped_at") is None:
            alive = time.time() - float(lease.get("heartbeat_at", 0)) < 20
        return {"control": control, "worker": {"alive": alive, "pid": lease.get("pid") if lease else None, "heartbeat_at": lease.get("heartbeat_at") if lease else None, "started_at": lease.get("started_at") if lease else None}, "cursor": self.get_cursor(), "messages": counts, "media_failures": int(media_failed), "pending_senders": int(pending_senders), "last_message": dict(last) if last else None}
