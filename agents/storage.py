"""SQLite persistence for profiles, bindings, identities and deliveries."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
STATUSES = {"running", "paused", "stopped"}


def now() -> float:
    return time.time()


class _Connection(sqlite3.Connection):
    """Connection that closes on context exit.

    sqlite3's own context manager only commits or rolls back; on Windows an
    unclosed handle keeps the file locked, so temporary state directories
    cannot be cleaned up. All callers use ``with store.connect() as db:``.
    """

    def __enter__(self) -> sqlite3.Connection:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


class MirrorStore:
    """Shared state store used by the manager API and each worker process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        db = _Connection(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def initialize(self, config: dict[str, Any] | None = None) -> None:
        with self.connect() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            existing = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bindings'").fetchone())
            version = 0
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_meta'").fetchone():
                row = db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                version = int(row[0]) if row else 0
            if version > SCHEMA_VERSION:
                raise RuntimeError('数据库版本高于当前程序，请更新代码后重试')
            if existing and version < SCHEMA_VERSION:
                self.backup()
            if self._needs_migration(db):
                self._migrate_v1(db)
            self._create_schema(db)
            self._upgrade_v3(db)
            if config and config.get("wechat_talker") and config.get("feishu_chat_id"):
                self._seed_config(db, config)
            db.execute("INSERT INTO schema_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("schema_version", str(SCHEMA_VERSION)))
            db.execute("PRAGMA foreign_keys=ON")

    def backup(self) -> Path:
        destination = self.path.parent / "backups" / f"{self.path.name}.{time.time_ns()}.bak"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            target.commit()
        finally:
            source.close()
            target.close()
        return destination

    def _upgrade_v3(self, db):
        columns = {r[1] for r in db.execute("PRAGMA table_info(runtime_state)")}
        if 'initialized' not in columns:
            db.execute("ALTER TABLE runtime_state ADD COLUMN initialized INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE runtime_state ADD COLUMN recovery_json TEXT NOT NULL DEFAULT '{}'")
            db.execute("UPDATE runtime_state SET initialized=1 WHERE cursor>0 OR binding_id IN (SELECT binding_id FROM messages)")
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='bindings'").fetchone()[0]
        if 'UNIQUE(source_account,wechat_talker)' in schema:
            invalid = db.execute("SELECT id FROM bindings WHERE trim(wechat_talker)='' OR trim(feishu_chat_id)='' OR trim(lark_profile)=''").fetchall()
            for row in invalid:
                db.execute('UPDATE bindings SET archived=1,enabled=0 WHERE id=?',(row['id'],))
                db.execute("UPDATE runtime_control SET status='stopped',reason='迁移保留旧空绑定并归档' WHERE binding_id=?",(row['id'],))
                db.execute("INSERT INTO management_actions(action,actor,binding_id,result,created_at) VALUES('migration.archive_empty_binding','system',?,'accepted',?)",(row['id'],now()))
            replacement = schema.replace('bindings', 'bindings_v3', 1).replace('UNIQUE(source_account,wechat_talker), UNIQUE(feishu_chat_id)', 'CHECK(archived=1 OR length(wechat_talker)>0)')
            db.execute(replacement)
            db.execute("INSERT INTO bindings_v3 SELECT * FROM bindings")
            db.execute("DROP TABLE bindings")
            db.execute("ALTER TABLE bindings_v3 RENAME TO bindings")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS bindings_source_live ON bindings(source_account,wechat_talker) WHERE archived=0")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS bindings_target_live ON bindings(feishu_chat_id) WHERE archived=0")
        for table, name in (("bindings","wechat_alias"),("bindings","feishu_alias"),("profiles","display_name")):
            cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if name not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE TABLE IF NOT EXISTS deliveries(message_id INTEGER NOT NULL, part TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL, feishu_message_id TEXT, attempted_at REAL, error TEXT, PRIMARY KEY(message_id,part))")
        db.execute("CREATE TABLE IF NOT EXISTS sender_occurrences(confirmation_id INTEGER NOT NULL, binding_id INTEGER NOT NULL, PRIMARY KEY(confirmation_id,binding_id))")
        db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")

    def recovery(self, binding_id):
        with self.connect() as db:
            row = db.execute("SELECT initialized,recovery_json FROM runtime_state WHERE binding_id=?", (binding_id,)).fetchone()
        return {"initialized": bool(row[0]), **json.loads(row[1])} if row else {"initialized": False}

    def update_recovery(self, binding_id, **values):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT recovery_json FROM runtime_state WHERE binding_id=?", (binding_id,)).fetchone()
            state = json.loads(row[0]) if row else {}
            state.update(values)
            db.execute("UPDATE runtime_state SET recovery_json=?,updated_at=? WHERE binding_id=?", (json.dumps(state, ensure_ascii=False), now(), binding_id))
        return state

    def initialize_cursor(self, binding_id, cursor):
        with self.connect() as db:
            db.execute("UPDATE runtime_state SET cursor=?,initialized=1,updated_at=? WHERE binding_id=? AND initialized=0", (cursor, now(), binding_id))

    def pending_messages(self, binding_id):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM messages WHERE binding_id=? AND state NOT IN ('sent','placeholder')", (binding_id,))]
        return sorted(rows, key=lambda r: (int(json.loads(r['payload_json']).get('create_time') or 0), int(r['local_id'] or 0)))

    def message(self, key, binding_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE binding_id=? AND wechat_message_key=?", (binding_id, key)).fetchone()
        return dict(row) if row else None

    def delivery(self, message_id, part):
        with self.connect() as db:
            row = db.execute("SELECT * FROM deliveries WHERE message_id=? AND part=?", (message_id, part)).fetchone()
        return dict(row) if row else None

    def prepare_delivery(self, message_id, part, payload, key):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO deliveries(message_id,part,state,payload_json,idempotency_key) VALUES(?,?,'pending',?,?)", (message_id, part, json.dumps(payload, ensure_ascii=False), key))
        return self.delivery(message_id, part)

    def set_delivery(self, message_id, part, state, remote_id=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state=?,feishu_message_id=COALESCE(?,feishu_message_id),attempted_at=COALESCE(attempted_at,?),error=? WHERE message_id=? AND part=?", (state, remote_id, now(), error, message_id, part))

    def _needs_migration(self, db: sqlite3.Connection) -> bool:
        row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bindings'").fetchone()
        if not row:
            return False
        columns = {r[1] for r in db.execute("PRAGMA table_info(bindings)")}
        return "source_account" not in columns or "lark_profile" not in columns

    def _create_schema(self, db: sqlite3.Connection) -> None:
        schema = """
            CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles(
                id INTEGER PRIMARY KEY AUTOINCREMENT, profile_name TEXT NOT NULL UNIQUE,
                app_id TEXT NOT NULL UNIQUE, brand TEXT NOT NULL DEFAULT 'feishu',
                purpose TEXT NOT NULL DEFAULT 'mirror', protected INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1, bot_status TEXT NOT NULL DEFAULT 'unknown',
                user_status TEXT NOT NULL DEFAULT 'unknown', last_checked_at REAL,
                last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bindings(
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_account TEXT NOT NULL DEFAULT 'default',
                wechat_chat_name TEXT NOT NULL, wechat_talker TEXT NOT NULL,
                feishu_chat_name TEXT NOT NULL, feishu_chat_id TEXT NOT NULL,
                lark_profile TEXT NOT NULL, lark_app_id TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                CHECK(length(wechat_talker)>0)
            );
            CREATE INDEX IF NOT EXISTS bindings_active_idx ON bindings(archived,enabled);
            CREATE TABLE IF NOT EXISTS runtime_control(
                binding_id INTEGER PRIMARY KEY REFERENCES bindings(id),
                status TEXT NOT NULL CHECK(status IN ('running','paused','stopped')),
                reason TEXT, actor TEXT, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_state(
                binding_id INTEGER PRIMARY KEY REFERENCES bindings(id), cursor INTEGER NOT NULL DEFAULT 0,
                last_success_at REAL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS worker_leases(
                binding_id INTEGER PRIMARY KEY REFERENCES bindings(id), pid INTEGER NOT NULL,
                token TEXT NOT NULL, started_at REAL NOT NULL, heartbeat_at REAL NOT NULL, stopped_at REAL
            );
            CREATE TABLE IF NOT EXISTS people(
                id INTEGER PRIMARY KEY AUTOINCREMENT, confirmed_name TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wechat_identities(
                id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER REFERENCES people(id),
                source_account TEXT NOT NULL, wechat_sender_id TEXT NOT NULL,
                last_display_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('confirmed','pending','rejected')),
                created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(source_account,wechat_sender_id)
            );
            CREATE TABLE IF NOT EXISTS feishu_identities(
                id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL REFERENCES people(id),
                profile_id INTEGER NOT NULL REFERENCES profiles(id), open_id TEXT NOT NULL,
                user_id TEXT, display_name TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(profile_id,open_id)
            );
            CREATE TABLE IF NOT EXISTS confirmations(
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_account TEXT NOT NULL,
                wechat_sender_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
                binding_id INTEGER REFERENCES bindings(id), person_id INTEGER REFERENCES people(id),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected')),
                notification_status TEXT NOT NULL DEFAULT 'pending', last_error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(source_account,wechat_sender_id)
            );
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, binding_id INTEGER NOT NULL REFERENCES bindings(id),
                source_account TEXT NOT NULL, wechat_message_key TEXT NOT NULL, local_id INTEGER,
                server_id TEXT, sender_wxid TEXT, sender_name TEXT, sender_person_id INTEGER REFERENCES people(id),
                kind TEXT NOT NULL, payload_json TEXT, quoted_message_key TEXT,
                state TEXT NOT NULL CHECK(state IN ('received','processing','sent','placeholder','failed','pending')),
                feishu_message_id TEXT, idempotency_key TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(binding_id,wechat_message_key)
            );
            CREATE INDEX IF NOT EXISTS messages_state_idx ON messages(binding_id,state,updated_at);
            CREATE TABLE IF NOT EXISTS media_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL REFERENCES messages(id),
                media_type TEXT NOT NULL, source_ref TEXT, staging_path TEXT,
                state TEXT NOT NULL CHECK(state IN ('pending','processing','uploaded','placeholder','manual','failed')),
                error TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS media_jobs_state_idx ON media_jobs(state,updated_at);
            CREATE TABLE IF NOT EXISTS management_actions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, actor TEXT NOT NULL,
                binding_id INTEGER REFERENCES bindings(id), details_json TEXT, result TEXT NOT NULL, created_at REAL NOT NULL
            );
            """
        # executescript would commit the migration halfway through. These
        # schema statements contain no semicolons inside SQL string literals.
        for statement in schema.split(';'):
            if statement.strip():
                db.execute(statement)

    def _migrate_v1(self, db: sqlite3.Connection) -> None:
        """Copy the old single-binding schema before replacing fixed tables."""
        old: dict[str, list[dict[str, Any]]] = {}
        tables = ("bindings", "runtime_control", "runtime_state", "worker_leases", "messages", "sender_mappings", "media_jobs", "management_actions")
        for table in tables:
            try:
                old[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()]
            except sqlite3.OperationalError:
                old[table] = []
        for table in ("media_jobs", "messages", "sender_mappings", "worker_leases", "runtime_state", "runtime_control", "management_actions", "bindings"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        self._create_schema(db)
        ts = now()
        binding = old["bindings"][0] if old["bindings"] else {}
        if not binding:
            return
        db.execute("INSERT INTO bindings(id,source_account,wechat_chat_name,wechat_talker,feishu_chat_name,feishu_chat_id,lark_profile,enabled,created_at,updated_at) VALUES(1,'default',?,?,?,?,?,?,?,?)", (binding.get("wechat_chat_name", ""), binding.get("wechat_talker", ""), binding.get("feishu_chat_name", ""), binding.get("feishu_chat_id", ""), "wechat-mirror", int(binding.get("enabled", 1)), binding.get("created_at", ts), binding.get("updated_at", ts)))
        control = old["runtime_control"][0] if old["runtime_control"] else {}
        state = old["runtime_state"][0] if old["runtime_state"] else {}
        db.execute("INSERT INTO runtime_control(binding_id,status,reason,actor,updated_at) VALUES(?,?,?,?,?)", (1, control.get("status", "stopped"), control.get("reason"), control.get("actor"), control.get("updated_at", ts)))
        db.execute("INSERT INTO runtime_state(binding_id,cursor,last_success_at,updated_at) VALUES(1,?,?,?)", (int(state.get("cursor", 0)), state.get("last_success_at"), state.get("updated_at", ts)))
        for row in old["messages"]:
            db.execute("INSERT INTO messages(id,binding_id,source_account,wechat_message_key,local_id,server_id,sender_wxid,sender_name,kind,payload_json,state,feishu_message_id,idempotency_key,attempts,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (row.get("id"), 1, "default", row.get("wechat_message_key", ""), row.get("local_id"), row.get("server_id", ""), row.get("sender_wxid"), row.get("sender_name", ""), row.get("kind", "text"), row.get("payload_json"), row.get("state", "received"), row.get("feishu_message_id"), row.get("idempotency_key"), row.get("attempts", 0), row.get("last_error"), row.get("created_at", ts), row.get("updated_at", ts)))
        for row in old["media_jobs"]:
            db.execute("INSERT INTO media_jobs(id,message_id,media_type,source_ref,staging_path,state,error,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (row.get("id"), row.get("message_id"), row.get("media_type", "media"), row.get("source_ref"), row.get("staging_path"), row.get("state", "pending"), row.get("error"), row.get("attempts", 0), row.get("created_at", ts), row.get("updated_at", ts)))
        for row in old["management_actions"]:
            db.execute("INSERT INTO management_actions(id,action,actor,binding_id,details_json,result,created_at) VALUES(?,?,?,?,?,?,?)", (row.get("id"), row.get("action", "legacy"), row.get("actor", "legacy"), row.get("binding_id"), row.get("details_json"), row.get("result", "accepted"), row.get("created_at", ts)))
        for row in old["sender_mappings"]:
            name = row.get("confirmed_name") or row.get("wechat_display_name") or "未知发言人"
            person = db.execute("SELECT id FROM people WHERE confirmed_name=? LIMIT 1", (name,)).fetchone()
            person_id = int(person[0]) if person else int(db.execute("INSERT INTO people(confirmed_name,created_at,updated_at) VALUES(?,?,?) RETURNING id", (name, row.get("updated_at", ts), row.get("updated_at", ts))).fetchone()[0])
            sender_id = row.get("wechat_sender_id") or f"legacy:{person_id}"
            db.execute("INSERT OR IGNORE INTO wechat_identities(person_id,source_account,wechat_sender_id,last_display_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (person_id if row.get("status") == "confirmed" else None, "default", sender_id, row.get("wechat_display_name", ""), row.get("status", "pending"), row.get("updated_at", ts), row.get("updated_at", ts)))

    def _seed_config(self, db: sqlite3.Connection, config: dict[str, Any]) -> None:
        if db.execute("SELECT 1 FROM schema_meta WHERE key='config_seeded'").fetchone():
            return
        ts = now()
        # Existing SQLite mappings are authoritative. Import legacy JSON only
        # into an empty database; never overwrite names or destinations on read.
        if not db.execute("SELECT 1 FROM bindings LIMIT 1").fetchone():
            profile = str(config.get("lark_profile") or "wechat-mirror")
            app_id = str(config.get("lark_app_id") or profile)
            db.execute("INSERT OR IGNORE INTO profiles(profile_name,app_id,created_at,updated_at) VALUES(?,?,?,?)", (profile, app_id, ts, ts))
            cur = db.execute("INSERT INTO bindings(source_account,wechat_chat_name,wechat_talker,feishu_chat_name,feishu_chat_id,lark_profile,lark_app_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (config.get("source_account", "default"), config.get("wechat_chat_name", ""), config["wechat_talker"], config.get("feishu_chat_name", ""), config["feishu_chat_id"], profile, app_id, ts, ts))
            bid = cur.lastrowid
            db.execute("INSERT INTO runtime_control(binding_id,status,updated_at) VALUES(?,'stopped',?)", (bid, ts))
            db.execute("INSERT INTO runtime_state(binding_id,cursor,updated_at) VALUES(?,?,?)", (bid, int(config.get("initial_cursor", 0)), ts))
            if config.get("initial_cursor", 0):
                db.execute("UPDATE runtime_state SET initialized=1 WHERE binding_id=?", (bid,))
            for sender_id, name in config.get("sender_map", {}).items():
                person = db.execute("INSERT INTO people(confirmed_name,created_at,updated_at) VALUES(?,?,?)", (name, ts, ts)).lastrowid
                db.execute("INSERT INTO wechat_identities(person_id,source_account,wechat_sender_id,last_display_name,status,created_at,updated_at) VALUES(?,?,?,?, 'confirmed',?,?)", (person, config.get("source_account", "default"), sender_id, name, ts, ts))
        db.execute("INSERT INTO schema_meta(key,value) VALUES('config_seeded','1')")

    def list_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM profiles ORDER BY profile_name").fetchall()]

    def list_people(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT p.*,COUNT(DISTINCT w.id) AS wechat_identity_count,COUNT(DISTINCT f.id) AS feishu_identity_count FROM people p LEFT JOIN wechat_identities w ON w.person_id=p.id LEFT JOIN feishu_identities f ON f.person_id=p.id GROUP BY p.id ORDER BY p.confirmed_name").fetchall()
        return [dict(x) for x in rows]

    def list_wechat_identities(self, person_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            query = "SELECT w.*,p.confirmed_name FROM wechat_identities w LEFT JOIN people p ON p.id=w.person_id"
            rows = db.execute(query + (" WHERE w.person_id=?" if person_id else "") + " ORDER BY w.updated_at DESC", (person_id,) if person_id else ()).fetchall()
        return [dict(x) for x in rows]

    def list_feishu_identities(self, person_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            query = "SELECT f.*,p.confirmed_name,pr.profile_name FROM feishu_identities f JOIN people p ON p.id=f.person_id JOIN profiles pr ON pr.id=f.profile_id"
            rows = db.execute(query + (" WHERE f.person_id=?" if person_id else "") + " ORDER BY f.updated_at DESC", (person_id,) if person_id else ()).fetchall()
        return [dict(x) for x in rows]

    def mention_identity(self, source_account: str, sender_id: str, profile_name: str) -> dict[str, Any] | None:
        # IDs are scoped to their Feishu application; no name-only matching.
        with self.connect() as db:
            row=db.execute("""SELECT p.confirmed_name, f.open_id FROM wechat_identities w
                JOIN people p ON p.id=w.person_id JOIN feishu_identities f ON f.person_id=p.id
                JOIN profiles pr ON pr.id=f.profile_id
                WHERE w.source_account=? AND w.wechat_sender_id=? AND pr.profile_name=? AND pr.enabled=1
                ORDER BY f.id LIMIT 1""", (source_account, sender_id, profile_name)).fetchone()
            return dict(row) if row else None

    def create_person(self, name: str, notes: str = "") -> dict[str, Any]:
        if not name.strip():
            raise ValueError("姓名不能为空")
        ts = now()
        with self.connect() as db:
            cur = db.execute("INSERT INTO people(confirmed_name,notes,created_at,updated_at) VALUES(?,?,?,?)", (name.strip(), notes, ts, ts))
            return dict(db.execute("SELECT * FROM people WHERE id=?", (cur.lastrowid,)).fetchone())

    def rename_person(self, person_id: int, name: str, actor: str = "codex") -> dict[str, Any]:
        if not name.strip():
            raise ValueError("姓名不能为空")
        with self.connect() as db:
            before = db.execute('SELECT confirmed_name FROM people WHERE id=?',(person_id,)).fetchone()
            if not before:raise ValueError('人员不存在')
            db.execute("UPDATE people SET confirmed_name=?,updated_at=? WHERE id=?", (name.strip(), now(), person_id))
            row = db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
            if not row:
                raise ValueError("人员不存在")
            db.execute("INSERT INTO management_actions(action,actor,details_json,result,created_at) VALUES(?,?,?,?,?)", ("rename_person", actor, json.dumps({"person_id": person_id, "before":before[0], "name": name.strip()}, ensure_ascii=False), "accepted", now()))
            return dict(row)

    def link_wechat_identity(self, person_id: int, source_account: str, sender_id: str, display_name: str = "") -> dict[str, Any]:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise ValueError("人员不存在")
            db.execute("INSERT INTO wechat_identities(person_id,source_account,wechat_sender_id,last_display_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_account,wechat_sender_id) DO UPDATE SET person_id=excluded.person_id,last_display_name=excluded.last_display_name,status='confirmed',updated_at=excluded.updated_at", (person_id, source_account, sender_id, display_name, "confirmed", now(), now()))
            db.execute("UPDATE confirmations SET person_id=?,status='confirmed',updated_at=? WHERE source_account=? AND wechat_sender_id=?", (person_id, now(), source_account, sender_id))
            return dict(db.execute("SELECT w.*,p.confirmed_name FROM wechat_identities w JOIN people p ON p.id=w.person_id WHERE w.source_account=? AND w.wechat_sender_id=?", (source_account, sender_id)).fetchone())

    def link_feishu_identity(self, person_id: int, profile_id: int, open_id: str, user_id: str = "", display_name: str = "") -> dict[str, Any]:
        open_id = str(open_id or "").strip()
        if not open_id.startswith("ou_") or len(open_id) <= 3 or any(ch.isspace() for ch in open_id):
            raise ValueError("飞书 open_id 格式不正确；请从所选应用读取 ou_ 开头的 ID")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise ValueError("人员不存在")
            db.execute("INSERT INTO feishu_identities(person_id,profile_id,open_id,user_id,display_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(profile_id,open_id) DO UPDATE SET person_id=excluded.person_id,user_id=excluded.user_id,display_name=excluded.display_name,updated_at=excluded.updated_at", (person_id, profile_id, open_id, user_id, display_name, now(), now()))
            return dict(db.execute("SELECT f.*,p.confirmed_name,pr.profile_name FROM feishu_identities f JOIN people p ON p.id=f.person_id JOIN profiles pr ON pr.id=f.profile_id WHERE f.profile_id=? AND f.open_id=?", (profile_id, open_id)).fetchone())

    def upsert_profile(self, profile_name: str, app_id: str, *, brand: str = "feishu", purpose: str = "mirror", protected: bool = False) -> dict[str, Any]:
        ts = now()
        with self.connect() as db:
            db.execute("INSERT INTO profiles(profile_name,app_id,brand,purpose,protected,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(profile_name) DO UPDATE SET app_id=excluded.app_id,brand=excluded.brand,purpose=excluded.purpose,updated_at=excluded.updated_at", (profile_name, app_id, brand, purpose, int(protected), ts, ts))
            return dict(db.execute("SELECT * FROM profiles WHERE profile_name=?", (profile_name,)).fetchone())

    def update_profile_health(self, profile_name: str, *, bot_status: str, user_status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE profiles SET bot_status=?,user_status=?,last_error=?,last_checked_at=?,updated_at=? WHERE profile_name=?", (bot_status, user_status, error, now(), now(), profile_name))

    def profile_has_bindings(self, profile_name: str) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM bindings WHERE lark_profile=? AND archived=0 LIMIT 1", (profile_name,)).fetchone())

    def list_bindings(self, include_archived: bool = True) -> list[dict[str, Any]]:
        with self.connect() as db:
            where = "" if include_archived else "WHERE b.archived=0"
            rows = db.execute(f"SELECT b.*,c.status control_status,s.cursor,w.pid,w.heartbeat_at,w.stopped_at FROM bindings b LEFT JOIN runtime_control c ON c.binding_id=b.id LEFT JOIN runtime_state s ON s.binding_id=b.id LEFT JOIN worker_leases w ON w.binding_id=b.id {where} ORDER BY b.id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["recovery"] = self.recovery(int(item["id"]))
            item["worker_alive"] = bool(item.get("stopped_at") is None and item.get("heartbeat_at") and now() - float(item["heartbeat_at"]) < 20)
            result.append(item)
        return result

    def get_binding(self, binding_id: int) -> dict[str, Any] | None:
        return next((x for x in self.list_bindings() if int(x["id"]) == int(binding_id)), None)

    def rename_binding(self, binding_id: int, *, wechat_chat_name: str | None = None, feishu_chat_name: str | None = None, actor: str = "codex") -> dict[str, Any]:
        current = self.get_binding(binding_id)
        if not current:
            raise ValueError("群绑定不存在")
        source = wechat_chat_name.strip() if wechat_chat_name is not None else current["wechat_chat_name"]
        target = feishu_chat_name.strip() if feishu_chat_name is not None else current["feishu_chat_name"]
        if not source or not target:
            raise ValueError("群名称不能为空")
        with self.connect() as db:
            db.execute("UPDATE bindings SET wechat_chat_name=?,feishu_chat_name=?,updated_at=? WHERE id=?", (source, target, now(), binding_id))
            db.execute("INSERT INTO management_actions(action,actor,binding_id,details_json,result,created_at) VALUES(?,?,?,?,?,?)", ("rename_binding", actor, binding_id, json.dumps({"before": {"wechat_chat_name": current["wechat_chat_name"], "feishu_chat_name": current["feishu_chat_name"]}, "after": {"wechat_chat_name": source, "feishu_chat_name": target}}, ensure_ascii=False), "accepted", now()))
        return self.get_binding(binding_id) or {}

    def set_profile_enabled(self, profile_name: str, enabled: bool, actor: str = "codex") -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE profile_name=?", (profile_name,)).fetchone()
            if not row:
                raise ValueError("Profile 不存在")
            if row["protected"] and not enabled:
                raise ValueError("受保护 Profile 不能停用")
            db.execute("UPDATE profiles SET enabled=?,updated_at=? WHERE profile_name=?", (int(enabled), now(), profile_name))
            if not enabled:
                binding_ids = [int(r[0]) for r in db.execute("SELECT id FROM bindings WHERE lark_profile=? AND archived=0", (profile_name,)).fetchall()]
            else:
                binding_ids = []
        for binding_id in binding_ids:
            self.set_control(binding_id, "stopped", actor=actor, reason="Profile 已停用")
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE profile_name=?", (profile_name,)).fetchone()
        return dict(row) if row else {}

    def list_media_failures(self, binding_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            query = "SELECT m.*,x.binding_id,x.wechat_message_key,x.local_id,x.sender_name FROM media_jobs m JOIN messages x ON x.id=m.message_id"
            args: tuple[Any, ...] = ()
            if binding_id is not None:
                query += " WHERE x.binding_id=?"
                args = (binding_id,)
            query += " AND" if " WHERE " in query else " WHERE "
            query += " m.state IN ('failed','manual','placeholder') ORDER BY m.updated_at DESC"
            return [dict(row) for row in db.execute(query, args).fetchall()]

    def create_binding(self, data: dict[str, Any]) -> dict[str, Any]:
        required = ("wechat_chat_name", "wechat_talker", "feishu_chat_name", "feishu_chat_id", "lark_profile")
        missing = [x for x in required if not str(data.get(x) or "").strip()]
        if missing:
            raise ValueError(f"缺少字段: {', '.join(missing)}")
        source = str(data.get("source_account") or "default")
        with self.connect() as db:
            profile = db.execute("SELECT id,app_id,enabled FROM profiles WHERE profile_name=?", (data["lark_profile"],)).fetchone()
            if not profile:
                raise ValueError("Profile 不存在，请先同步或接入 Profile")
            if not profile[2]:
                raise ValueError("Profile 已停用")
            if db.execute("SELECT 1 FROM bindings WHERE source_account=? AND wechat_talker=? AND archived=0", (source, data["wechat_talker"])).fetchone():
                raise ValueError("该微信源群已有有效绑定")
            if db.execute("SELECT 1 FROM bindings WHERE feishu_chat_id=? AND archived=0", (data["feishu_chat_id"],)).fetchone():
                raise ValueError("该飞书目标群已有有效绑定")
            if db.execute("SELECT COUNT(*) FROM bindings WHERE archived=0").fetchone()[0] >= 10:
                raise ValueError("最多支持 10 组有效绑定")
            ts = now()
            cur = db.execute("INSERT INTO bindings(source_account,wechat_chat_name,wechat_talker,feishu_chat_name,feishu_chat_id,lark_profile,lark_app_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (source, data["wechat_chat_name"], data["wechat_talker"], data["feishu_chat_name"], data["feishu_chat_id"], data["lark_profile"], data.get("lark_app_id") or profile[1], ts, ts))
            bid = cur.lastrowid
            db.execute("INSERT INTO runtime_control(binding_id,status,reason,actor,updated_at) VALUES(?,?,?,?,?)", (bid, "stopped", "new binding", "manager", ts))
            db.execute("INSERT INTO runtime_state(binding_id,cursor,updated_at) VALUES(?,?,?)", (bid, int(data.get("initial_cursor", 0)), ts))
        return self.get_binding(int(bid)) or {}

    def archive_binding(self, binding_id: int, actor: str = "codex") -> None:
        with self.connect() as db:
            db.execute("UPDATE bindings SET archived=1,enabled=0,updated_at=? WHERE id=?", (now(), binding_id))
        self.set_control(binding_id, "stopped", actor, "binding archived")

    def get_control(self, binding_id: int = 1) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runtime_control WHERE binding_id=?", (binding_id,)).fetchone()
        return dict(row) if row else {"binding_id": binding_id, "status": "stopped", "reason": None, "actor": None, "updated_at": 0}

    def get_cursor(self, binding_id: int = 1) -> int:
        with self.connect() as db:
            row = db.execute("SELECT cursor FROM runtime_state WHERE binding_id=?", (binding_id,)).fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, cursor: int, binding_id: int = 1) -> None:
        ts = now()
        with self.connect() as db:
            db.execute("INSERT INTO runtime_state(binding_id,cursor,last_success_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET cursor=MAX(runtime_state.cursor,excluded.cursor),last_success_at=excluded.last_success_at,updated_at=excluded.updated_at", (binding_id, int(cursor), ts, ts))

    def set_control(self, binding_id: int | str, status: str | None = None, actor: str = "codex", reason: str | None = None) -> None:
        # Keep the v0.1 call shape: set_control("paused", actor=...).
        if status is None and isinstance(binding_id, str) and binding_id in STATUSES:
            status, binding_id = binding_id, 1
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        bid = int(binding_id)
        ts = now()
        with self.connect() as db:
            db.execute("INSERT INTO runtime_control(binding_id,status,reason,actor,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,actor=excluded.actor,updated_at=excluded.updated_at", (bid, status, reason, actor, ts))
            db.execute("INSERT INTO management_actions(action,actor,binding_id,details_json,result,created_at) VALUES(?,?,?,?,?,?)", (status, actor, bid, json.dumps({"reason": reason}, ensure_ascii=False), "accepted", ts))

    def acquire_lease(self, binding_id: int = 1, pid: int | None = None) -> str:
        pid = pid or os.getpid()
        token = str(uuid.uuid4())
        ts = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT pid,heartbeat_at,stopped_at FROM worker_leases WHERE binding_id=?", (binding_id,)).fetchone()
            if row and row[2] is None and ts - float(row[1]) < 20:
                raise RuntimeError(f"已有同步 worker 运行中（pid={row[0]}）")
            db.execute("INSERT INTO worker_leases(binding_id,pid,token,started_at,heartbeat_at,stopped_at) VALUES(?,?,?,?,?,NULL) ON CONFLICT(binding_id) DO UPDATE SET pid=excluded.pid,token=excluded.token,started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,stopped_at=NULL", (binding_id, pid, token, ts, ts))
        return token

    def heartbeat(self, token: str, binding_id: int = 1) -> None:
        with self.connect() as db:
            db.execute("UPDATE worker_leases SET heartbeat_at=? WHERE binding_id=? AND token=?", (now(), binding_id, token))

    def release_lease(self, token: str, binding_id: int = 1) -> None:
        with self.connect() as db:
            db.execute("UPDATE worker_leases SET stopped_at=?,heartbeat_at=? WHERE binding_id=? AND token=?", (now(), now(), binding_id, token))

    def get_lease(self, binding_id: int = 1) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM worker_leases WHERE binding_id=?", (binding_id,)).fetchone()
        return dict(row) if row else None

    def resolve_sender(self, source_account: str, sender_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT w.*,p.confirmed_name FROM wechat_identities w LEFT JOIN people p ON p.id=w.person_id WHERE w.source_account=? AND w.wechat_sender_id=?", (source_account, sender_id)).fetchone()
        return dict(row) if row else None

    def record_sender(self, source_account: str, sender_id: str, display_name: str, confirmed_name: str | None = None, *, binding_id: int | None = None) -> int | None:
        # Keep compatibility with v0.1 record_sender(sender_id, display, name, status).
        if confirmed_name in {"confirmed", "pending", "rejected"}:
            source_account, sender_id, display_name, confirmed_name = "default", source_account, sender_id, display_name
        ts = now()
        with self.connect() as db:
            if confirmed_name:
                person = db.execute("SELECT person_id FROM wechat_identities WHERE source_account=? AND wechat_sender_id=? AND person_id IS NOT NULL", (source_account, sender_id)).fetchone()
                person_id = int(person[0]) if person else int(db.execute("INSERT INTO people(confirmed_name,created_at,updated_at) VALUES(?,?,?) RETURNING id", (confirmed_name, ts, ts)).fetchone()[0])
                status = "confirmed"
            else:
                person_id = None
                status = "pending"
            db.execute("INSERT INTO wechat_identities(person_id,source_account,wechat_sender_id,last_display_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_account,wechat_sender_id) DO UPDATE SET person_id=COALESCE(excluded.person_id,wechat_identities.person_id),last_display_name=excluded.last_display_name,status=excluded.status,updated_at=excluded.updated_at", (person_id, source_account, sender_id, display_name, status, ts, ts))
            if not confirmed_name:
                db.execute("INSERT INTO confirmations(source_account,wechat_sender_id,display_name,binding_id,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(source_account,wechat_sender_id) DO UPDATE SET display_name=excluded.display_name,binding_id=COALESCE(confirmations.binding_id,excluded.binding_id),updated_at=excluded.updated_at", (source_account, sender_id, display_name, binding_id, ts, ts))
            if not confirmed_name and binding_id is not None:
                db.execute("INSERT OR IGNORE INTO sender_occurrences SELECT id,? FROM confirmations WHERE source_account=? AND wechat_sender_id=?", (binding_id,source_account,sender_id))
            return person_id

    def confirm_sender(self, confirmation_id: int, name: str, actor: str = "codex") -> dict[str, Any]:
        if not name.strip():
            raise ValueError("姓名不能为空")
        ts = now()
        with self.connect() as db:
            item = db.execute("SELECT * FROM confirmations WHERE id=?", (confirmation_id,)).fetchone()
            if not item:
                raise ValueError("待确认事项不存在")
            person = None  # Identical names do not prove identical people.
            person_id = int(person[0]) if person else int(db.execute("INSERT INTO people(confirmed_name,created_at,updated_at) VALUES(?,?,?) RETURNING id", (name.strip(), ts, ts)).fetchone()[0])
            db.execute("UPDATE confirmations SET person_id=?,status='confirmed',updated_at=? WHERE id=?", (person_id, ts, confirmation_id))
            db.execute("UPDATE wechat_identities SET person_id=?,status='confirmed',updated_at=? WHERE source_account=? AND wechat_sender_id=?", (person_id, ts, item["source_account"], item["wechat_sender_id"]))
            db.execute("INSERT INTO management_actions(action,actor,binding_id,details_json,result,created_at) VALUES(?,?,?,?,?,?)", ("confirm_sender", actor, item["binding_id"], json.dumps({"confirmation_id": confirmation_id, "name": name.strip()}, ensure_ascii=False), "accepted", ts))
            return dict(db.execute("SELECT * FROM confirmations WHERE id=?", (confirmation_id,)).fetchone())

    def claim_confirmation_notification(self, source_account: str, sender_id: str) -> dict[str, Any] | None:
        """Atomically claim one pending reminder so parallel workers do not duplicate it."""
        cutoff = now() - 60
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM confirmations WHERE source_account=? AND wechat_sender_id=?", (source_account, sender_id)).fetchone()
            if not row or row["status"] != "pending" or not (row["notification_status"] in {"pending", "failed"} or (row["notification_status"] == "sending" and float(row["updated_at"]) < cutoff)):
                return None
            db.execute("UPDATE confirmations SET notification_status='sending',updated_at=? WHERE id=?", (now(), row["id"]))
            return dict(db.execute("SELECT * FROM confirmations WHERE id=? AND notification_status='sending'", (row["id"],)).fetchone())

    def finish_confirmation_notification(self, confirmation_id: int, *, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE confirmations SET notification_status=?,last_error=?,updated_at=? WHERE id=?", ("failed" if error else "sent", error, now(), confirmation_id))

    def list_confirmations(self, status: str | None = "pending") -> list[dict[str, Any]]:
        with self.connect() as db:
            query = "SELECT c.*,b.wechat_chat_name FROM confirmations c LEFT JOIN bindings b ON b.id=c.binding_id"
            rows = db.execute(query + (" WHERE c.status=?" if status else "") + " ORDER BY c.updated_at DESC", (status,) if status else ()).fetchall()
        result=[dict(x) for x in rows]
        with self.connect() as db:
            for item in result:
                item['groups']=[dict(r) for r in db.execute("SELECT b.id,b.wechat_chat_name FROM sender_occurrences o JOIN bindings b ON b.id=o.binding_id WHERE o.confirmation_id=?",(item['id'],))]
        return result

    def upsert_message(self, key: str, row: dict[str, Any], kind: str, state: str = "received", *, binding_id: int = 1, source_account: str = "default", payload: dict[str, Any] | None = None, quoted_message_key: str | None = None) -> int:
        ts = now()
        safe_payload = payload or {k: row.get(k) for k in ("talker", "local_id", "server_id", "sender_wxid", "sender", "kind_name", "text", "content", "time", "create_time", "forward_items", "voice_transcript") if k in row}
        with self.connect() as db:
            db.execute("INSERT INTO messages(binding_id,source_account,wechat_message_key,local_id,server_id,sender_wxid,sender_name,kind,payload_json,quoted_message_key,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(binding_id,wechat_message_key) DO UPDATE SET updated_at=excluded.updated_at", (binding_id, source_account, key, row.get("local_id"), str(row.get("server_id") or ""), row.get("sender_wxid"), row.get("sender"), kind, json.dumps(safe_payload, ensure_ascii=False, default=str), quoted_message_key, state, ts, ts))
            found = db.execute("SELECT id FROM messages WHERE binding_id=? AND wechat_message_key=?", (binding_id, key)).fetchone()
        return int(found[0])

    def message_state(self, key: str, binding_id: int = 1) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT state FROM messages WHERE binding_id=? AND wechat_message_key=?", (binding_id, key)).fetchone()
        return str(row[0]) if row else None

    def find_message_by_server_id(self, binding_id: int, server_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE binding_id=? AND server_id=? ORDER BY id DESC LIMIT 1", (binding_id, str(server_id))).fetchone()
        return dict(row) if row else None

    def mark_message(self, key: str, state: str, *, binding_id: int = 1, feishu_message_id: str | None = None, idempotency_key: str | None = None, error: str | None = None, sender_person_id: int | None = None, sender_name: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE messages SET state=?,feishu_message_id=COALESCE(?,feishu_message_id),idempotency_key=COALESCE(?,idempotency_key),last_error=?,sender_person_id=COALESCE(?,sender_person_id),sender_name=COALESCE(?,sender_name),attempts=attempts+1,updated_at=? WHERE binding_id=? AND wechat_message_key=?", (state, feishu_message_id, idempotency_key, error, sender_person_id, sender_name, now(), binding_id, key))

    def add_media_job(self,message_id,media_type,source_ref,staging_path,state='pending',error=None):
        with self.connect() as db:
            existing=db.execute("SELECT id FROM media_jobs WHERE message_id=? AND media_type=? ORDER BY id LIMIT 1",(message_id,media_type)).fetchone()
            if existing:
                db.execute("UPDATE media_jobs SET staging_path=?,state=?,error=?,attempts=attempts+1,updated_at=? WHERE id=?",(staging_path,state,error,now(),existing[0]))
            else:
                db.execute("INSERT INTO media_jobs(message_id,media_type,staging_path,state,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(message_id,media_type,staging_path,state,error,now(),now()))

    def audit(self,action,before=None,after=None,actor='codex',binding_id=None,result='accepted'):
        with self.connect() as db:
            db.execute("INSERT INTO management_actions(action,actor,binding_id,details_json,result,created_at) VALUES(?,?,?,?,?,?)",(action,actor,binding_id,json.dumps({'before':before,'after':after},ensure_ascii=False),result,now()))

    def setting(self,key,default=None):
        with self.connect() as db: row=db.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def save_setting(self,key,value):
        with self.connect() as db: db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(value,ensure_ascii=False)))

    def actions(self,person_id=None):
        with self.connect() as db: rows=[dict(r) for r in db.execute("SELECT * FROM management_actions ORDER BY id DESC LIMIT 200")]
        return [r for r in rows if not person_id or json.loads(r.get('details_json') or '{}').get('person_id')==person_id or str(person_id)==str((json.loads(r.get('details_json') or '{}').get('after') or {}).get('person_id'))]

    def uncertain_deliveries(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT d.message_id,d.part,d.state,d.attempted_at,d.error,m.binding_id,m.local_id,m.sender_name FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE d.state IN ('uncertain','sending') ORDER BY m.id")]

    def status(self, binding_id: int = 1) -> dict[str, Any]:
        control = self.get_control(binding_id)
        lease = self.get_lease(binding_id)
        with self.connect() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM messages WHERE binding_id=? GROUP BY state", (binding_id,))}
            media_failed = db.execute("SELECT COUNT(*) FROM media_jobs m JOIN messages x ON x.id=m.message_id WHERE x.binding_id=? AND m.state IN ('failed','manual','placeholder')", (binding_id,)).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM confirmations WHERE status='pending'").fetchone()[0]
            last = db.execute("SELECT local_id,sender_name,kind,state,updated_at FROM messages WHERE binding_id=? ORDER BY updated_at DESC LIMIT 1", (binding_id,)).fetchone()
        alive = bool(lease and lease.get("stopped_at") is None and now() - float(lease.get("heartbeat_at", 0)) < 20)
        return {"recovery": self.recovery(binding_id), "control": control, "worker": {"alive": alive, "pid": lease.get("pid") if lease else None, "heartbeat_at": lease.get("heartbeat_at") if lease else None, "started_at": lease.get("started_at") if lease else None}, "cursor": self.get_cursor(binding_id), "messages": counts, "media_failures": int(media_failed), "pending_senders": int(pending), "last_message": dict(last) if last else None}
