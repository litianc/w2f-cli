#!/usr/bin/env python3
"""Agent worker: mirror one WeChat chat into one Feishu group.

The worker is intentionally driven by the read-only rion-wechat-cli tail
stream and the separately configured wechat-mirror lark-cli profile.
"""
from __future__ import annotations

import argparse
import atexit
import fcntl
import html
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

try:
    from .storage import MirrorStore
except ImportError:
    from storage import MirrorStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "wechat_feishu_mirror.json"
WECHAT_READER = Path(os.environ.get("WECHAT_READER", str(Path.home() / ".codex/skills/wechat-cli/scripts/reader.sh")))
STICKER_MAX_PX = 200
STICKER_MAX_BYTES = 15 * 1024 * 1024
STICKER_DIR = ROOT / "staging" / "stickers"
MEDIA_DIR = ROOT / "staging" / "media"


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default.copy()
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def run_lark(config: dict[str, Any], args: list[str]) -> dict[str, Any]:
    cmd = ["lark-cli", "--profile", config["lark_profile"], "im", *args, "--as", "bot", "--format", "json"]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"lark-cli exit {proc.returncode}")
    return json.loads(proc.stdout)


def clean_body(row: dict[str, Any]) -> str:
    kind = str(row.get("kind_name") or "text")
    raw = str(row.get("text") or row.get("content") or "").strip()
    if kind in {"sticker", "emoji"}:
        return "[表情]"
    if kind in {"image", "video", "file", "voice", "audio", "media"}:
        labels = {"image": "图片", "video": "视频", "file": "文件", "voice": "语音", "audio": "语音", "media": "媒体"}
        return f"[{labels.get(kind, '媒体')}]"
    if raw.startswith("<"):
        # Keep useful fields from app messages while never forwarding raw XML.
        title = re.search(r"<title>(.*?)</title>", raw, re.S)
        urls = re.findall(r"https?://[^\s<>\"']+", raw)
        pieces = []
        if title:
            pieces.append(re.sub(r"\s+", " ", title.group(1)).strip())
        if urls:
            pieces.extend(urls[:3])
        if pieces:
            return "\n".join(dict.fromkeys(pieces))
        return "[富文本消息]"
    return raw or "[空消息]"


def sticker_url(row: dict[str, Any]) -> str | None:
    """Extract a short-lived WeChat CDN URL without logging its query string."""
    raw = str(row.get("text") or row.get("content") or "")
    match = re.search(r'(?:cdnurl|externurl)\s*=\s*["\']([^"\']+)', raw, re.I)
    return html.unescape(match.group(1)) if match else None


def media_url(row: dict[str, Any]) -> str | None:
    """Extract a downloadable URL from common WeChat XML media fields."""
    raw = str(row.get("text") or row.get("content") or "")
    for field in ("cdnurl", "url", "videourl", "voiceurl", "fileurl", "externurl"):
        match = re.search(rf'{re.escape(field)}\s*=\s*["\']([^"\']+)', raw, re.I)
        if match:
            return html.unescape(match.group(1))
    return None


def stage_media(row: dict[str, Any], kind: str) -> str | None:
    """Download a normal image/video/file into the local retry staging area."""
    url = media_url(row)
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "wechat-feishu-mirror/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read(STICKER_MAX_BYTES + 1)
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if len(payload) > STICKER_MAX_BYTES:
            return None
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "video/mp4": ".mp4", "audio/ogg": ".ogg"}.get(content_type)
        if not ext:
            ext = {"image": ".jpg", "video": ".mp4", "file": ".bin", "voice": ".ogg"}.get(kind, ".bin")
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        path = MEDIA_DIR / f"{int(row.get('local_id') or 0)}{ext}"
        path.write_bytes(payload)
        return str(path.relative_to(ROOT))
    except Exception as exc:
        print(f"媒体下载失败 local_id={row.get('local_id')}: {exc}", file=sys.stderr, flush=True)
        return None


def stage_sticker(row: dict[str, Any]) -> str | None:
    """Download and normalize a WeChat sticker for Feishu image upload.

    WeChat's CDN commonly returns ``application/octet-stream`` even for GIF or
    WebP data, so Pillow detects the format from the bytes. The normalized
    image fits a 200x200 box without upscaling, matching the usual WeChat chat
    bubble size while preserving aspect ratio and animation when possible.
    """
    url = sticker_url(row)
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "wechat-feishu-mirror/1.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read(STICKER_MAX_BYTES + 1)
        if len(payload) > STICKER_MAX_BYTES:
            return None
        source = Image.open(BytesIO(payload))
        source.load()
        source_format = (source.format or "PNG").upper()
        frame_count = getattr(source, "n_frames", 1)
        frames: list[Image.Image] = []
        durations: list[int] = []
        for index in range(frame_count):
            source.seek(index)
            frame = source.convert("RGBA")
            frame.thumbnail((STICKER_MAX_PX, STICKER_MAX_PX), Image.Resampling.LANCZOS)
            frames.append(frame.copy())
            durations.append(int(source.info.get("duration", 100) or 100))
        if not frames:
            return None

        local_id = int(row.get("local_id") or 0)
        STICKER_DIR.mkdir(parents=True, exist_ok=True)
        # GIF preserves animated stickers. Other formats are emitted as PNG
        # because Feishu's image upload path accepts it consistently.
        if source_format == "GIF" and len(frames) > 1:
            path = STICKER_DIR / f"{local_id}.gif"
            frames[0].convert("P", palette=Image.Palette.ADAPTIVE).save(
                path, save_all=True, append_images=[f.convert("P", palette=Image.Palette.ADAPTIVE) for f in frames[1:]],
                duration=durations, loop=int(source.info.get("loop", 0) or 0), disposal=2, optimize=True,
            )
        else:
            path = STICKER_DIR / f"{local_id}.png"
            frames[0].save(path, format="PNG", optimize=True)
        return str(path.relative_to(ROOT))
    except Exception as exc:
        print(f"贴图归一化失败 local_id={row.get('local_id')}: {exc}", file=sys.stderr, flush=True)
        return None


def message_key(row: dict[str, Any]) -> str:
    return f"{row.get('talker', '')}:{row.get('local_id', row.get('server_id', ''))}"


def send_row(config: dict[str, Any], row: dict[str, Any], state: dict[str, Any], store: MirrorStore) -> None:
    msg_key = message_key(row)
    kind = str(row.get("kind_name") or "text")
    # Store only the allowlisted fields selected by MirrorStore; message rows
    # can contain large base64/media fields that should not enter SQLite logs.
    store.upsert_message(msg_key, row, kind)
    if store.message_state(msg_key) in {"sent", "placeholder"}:
        print(f"已跳过已完成消息 local_id={row.get('local_id')}", flush=True)
        return
    sender_id = str(row.get("sender_wxid") or "")
    mapped_name = config.get("sender_map", {}).get(sender_id)
    display_name = str(row.get("sender") or "").strip()
    name = mapped_name or display_name or "未知发言人"
    if not mapped_name:
        # Continue mirroring with WeChat's display name while Codex asks the
        # administrator to confirm the person for future messages.
        pending_key = sender_id or f"unknown:{display_name or 'empty'}"
        store.record_sender(pending_key, display_name, None, "pending")
        state.setdefault("pending_senders", {})[pending_key] = {
            "display_name": row.get("sender", ""),
            "last_local_id": row.get("local_id"),
        }
        print(f"身份待确认，先使用显示名同步 local_id={row.get('local_id')} display={name}", file=sys.stderr, flush=True)
    else:
        store.record_sender(sender_id, display_name, name, "confirmed")
    body = clean_body(row)
    # Feishu limits idempotency keys to 50 bytes; reserve room for the
    # per-part suffix used by a sticker's name/image pair.
    key = "wechat-mirror-" + hashlib.sha256(msg_key.encode()).hexdigest()[:26]
    store.mark_message(msg_key, "processing", idempotency_key=key)
    try:
      if kind in {"sticker", "emoji"}:
        # A Feishu image message cannot carry a text caption. Send the sender
        # name as the adjacent text bubble, then upload the original sticker.
        run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：", "--idempotency-key", key + "-name"])
        image_input = stage_sticker(row)
        if image_input:
            result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--image", image_input, "--idempotency-key", key + "-image"])
            store.add_media_job(store.upsert_message(msg_key, row, kind, state="processing"), kind, image_input, image_input, "uploaded")
        else:
            result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：[表情资源不可用]", "--idempotency-key", key + "-fallback"])
            store.add_media_job(store.upsert_message(msg_key, row, kind, state="processing"), kind, None, None, "placeholder", "resource unavailable")
            store.mark_message(msg_key, "placeholder", feishu_message_id=str((result.get("data") or {}).get("message_id") or ""), idempotency_key=key)
            print(f"已同步 local_id={row.get('local_id')} sender={name} kind={kind}", flush=True)
            return
      elif kind in {"image", "video", "file", "voice", "audio", "media"}:
        # Media messages have no reliable caption channel; keep the required
        # real-name attribution as an adjacent text bubble.
        run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：", "--idempotency-key", key + "-name"])
        media_input = stage_media(row, kind)
        if media_input:
            media_flag = "--audio" if kind in {"voice", "audio"} else "--video" if kind == "video" else "--image" if kind == "image" else "--file"
            # Feishu video messages require a cover. Without a local cover,
            # send the downloaded video as a file so the content is retained.
            if media_flag == "--video":
                media_flag = "--file"
            result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], media_flag, media_input, "--idempotency-key", key + "-media"])
            message_id = store.upsert_message(msg_key, row, kind, state="processing")
            store.add_media_job(message_id, kind, media_url(row), media_input, "uploaded")
        else:
            result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：[资源不可用，待人工排查]", "--idempotency-key", key + "-fallback"])
            message_id = store.upsert_message(msg_key, row, kind, state="processing")
            store.add_media_job(message_id, kind, media_url(row), None, "placeholder", "resource unavailable")
            store.mark_message(msg_key, "placeholder", feishu_message_id=str((result.get("data") or {}).get("message_id") or ""), idempotency_key=key)
            print(f"已同步 local_id={row.get('local_id')} sender={name} kind={kind}（占位符）", flush=True)
            return
      else:
        result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：{body}", "--idempotency-key", key])
      store.mark_message(msg_key, "sent", feishu_message_id=str((result.get("data") or {}).get("message_id") or ""), idempotency_key=key)
    except Exception as exc:
      store.mark_message(msg_key, "failed", idempotency_key=key, error=str(exc)[:2000])
      raise
    print(f"已同步 local_id={row.get('local_id')} sender={name} kind={row.get('kind_name', 'text')}", flush=True)


def stream(config: dict[str, Any], state: dict[str, Any]) -> None:
    store = MirrorStore(config["sqlite_file"])
    store.initialize(config)
    token = store.acquire_lease()
    atexit.register(store.release_lease, token)
    stop_heartbeat = threading.Event()
    def heartbeat() -> None:
        while not stop_heartbeat.wait(5):
            store.heartbeat(token)
    heartbeat_thread = threading.Thread(target=heartbeat, name="mirror-heartbeat", daemon=True)
    heartbeat_thread.start()
    while True:
        cursor = max(int(state.get("cursor", 0)), store.get_cursor())
        cmd = [str(WECHAT_READER), "tail", "--talker", config["wechat_talker"], "--since-local-id", str(cursor),
               "--limit", "100", "--poll-interval", str(config.get("poll_interval", 2)), "--follow", "--jsonl",
               "--include-media-paths", "false"]
        proc = subprocess.Popen(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("ok") is False:
                        error = row.get("error", {})
                        print(f"微信读取失败 code={error.get('code', 'unknown')}，从已保存游标重试", file=sys.stderr, flush=True)
                        break
                    if row.get("talker") != config["wechat_talker"] or not isinstance(row.get("local_id"), int):
                        print("忽略非消息输出", file=sys.stderr, flush=True)
                        continue
                    while store.get_control().get("status") == "paused":
                        time.sleep(1)
                    send_row(config, row, state, store)
                    state["cursor"] = max(int(state.get("cursor", 0)), int(row.get("local_id") or 0))
                    store.set_cursor(state["cursor"])
                    store.heartbeat(token)
                    save_json(Path(config["state_file"]), state)
                except Exception as exc:  # keep tail alive; retry later
                    print(f"处理消息失败：{exc}", file=sys.stderr, flush=True)
                    # Restart from the last successful cursor instead of letting
                    # a later success advance past this failed message.
                    break
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        print("微信 tail 已退出，5 秒后重连", file=sys.stderr, flush=True)
        time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("run", "start", "stop", "pause", "resume", "status"), default="run")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="启动一次 tail 快照，便于验收")
    args = parser.parse_args()
    config = load_json(args.config, {})
    state_path = Path(config.get("state_file", ROOT / "state" / "wechat_feishu_mirror.json"))
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    config["state_file"] = str(state_path)
    sqlite_path = Path(config.get("sqlite_file", ROOT / "state" / "wechat_feishu_mirror.sqlite3"))
    if not sqlite_path.is_absolute():
        sqlite_path = ROOT / sqlite_path
    config["sqlite_file"] = str(sqlite_path)
    store = MirrorStore(sqlite_path)
    store.initialize(config)
    if args.once:
        state = load_json(state_path, {"cursor": config.get("initial_cursor", 0), "pending_senders": {}})
        # Migrate the prototype JSON cursor on the first formal start so the
        # SQLite store does not backfill messages already mirrored by it.
        if int(state.get("cursor", 0)) > store.get_cursor():
            store.set_cursor(int(state["cursor"]))
        config["poll_interval"] = 0.2
        # A bounded snapshot is useful in tests and never backfills history.
        cmd = [str(WECHAT_READER), "tail", "--talker", config["wechat_talker"], "--since-local-id", str(state.get("cursor", 0)),
               "--limit", "10", "--mode", "snapshot", "--include-media-paths", "false"]
        payload = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=True)
        print(payload.stdout, end="")
        return 0
    if args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command in {"pause", "resume"}:
        store.set_control("paused" if args.command == "pause" else "running", actor="codex", reason=f"manual {args.command}")
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "stop":
        lease = store.get_lease()
        if lease and lease.get("stopped_at") is None:
            pid = int(lease["pid"])
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                deadline = time.time() + 8
                while time.time() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.2)
            store.release_lease(str(lease["token"]))
        store.set_control("stopped", actor="codex", reason="manual stop")
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "start":
        current = store.status()
        if current["worker"]["alive"]:
            print(json.dumps(current, ensure_ascii=False, indent=2))
            return 0
        store.set_control("running", actor="codex", reason="manual start")
        log_path = ROOT / "logs" / "wechat_feishu_mirror.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", "--config", str(args.config.resolve())], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(0.4)
        print(json.dumps({"started_pid": child.pid, **store.status()}, ensure_ascii=False, indent=2))
        return 0
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有同步进程正在运行", file=sys.stderr)
            return 1
        state = load_json(state_path, {"cursor": config.get("initial_cursor", 0), "pending_senders": {}})
        if store.get_control().get("status") == "stopped":
            store.set_control("running", actor="worker", reason="worker started")
        state.get("pending_senders", {}).pop("", None)
        save_json(state_path, state)
        def stop(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            stream(config, state)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
