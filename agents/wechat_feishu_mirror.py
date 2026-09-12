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
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

try:
    from .storage import MirrorStore
    from .paths import DATA_DIR, runtime_paths
except ImportError:
    from storage import MirrorStore
    from paths import DATA_DIR, runtime_paths


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "wechat_feishu_mirror.json"
WECHAT_READER = Path(os.environ.get("WECHAT_READER", str(Path.home() / ".codex/skills/wechat-cli/scripts/reader.sh")))
STICKER_MAX_PX = 200
STICKER_MAX_BYTES = 15 * 1024 * 1024
STICKER_DIR = DATA_DIR / "stickers"
MEDIA_DIR = DATA_DIR / "media"


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


def notify_confirmation(config: dict[str, Any], store: MirrorStore, source_account: str, sender_id: str) -> None:
    """Send one private reminder to the configured administrator."""
    user_id = str(config.get("admin_feishu_user_id") or "").strip()
    if not user_id or not sender_id:
        return
    item = store.claim_confirmation_notification(source_account, sender_id)
    if not item:
        return
    groups = store.list_confirmations()
    group_names = ", ".join(sorted({str(x.get("wechat_chat_name") or "") for x in groups if x.get("wechat_sender_id") == sender_id and x.get("wechat_chat_name")}))
    text = f"微信群镜像待确认：编号 {item['id']}，昵称「{item.get('display_name') or '未知'}」，微信 ID {sender_id}"
    if group_names:
        text += f"，出现于：{group_names}"
    try:
        run_lark(config, ["+messages-send", "--user-id", user_id, "--text", text, "--idempotency-key", f"mirror-confirm-{item['id']}"])
    except Exception as exc:
        store.finish_confirmation_notification(int(item["id"]), error=str(exc)[:500])
        print(f"身份提醒发送失败 confirmation_id={item['id']}: {exc}", file=sys.stderr, flush=True)
    else:
        store.finish_confirmation_notification(int(item["id"]))


def clean_body(row: dict[str, Any]) -> str:
    kind = str(row.get("kind_name") or "text")
    raw = str(row.get("text") or row.get("content") or "").strip()
    if is_forward_row(row):
        items = forward_items(raw)
        if items:
            return format_forward_items(items)
        return "[转发聊天记录内容暂不可用]"
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


def is_forward_row(row: dict[str, Any]) -> bool:
    kind = str(row.get("kind_name") or row.get("kind") or "").lower()
    raw = str(row.get("text") or row.get("content") or "").lower()
    return kind in {"forward", "merged_forward", "chat_history", "history"} or "<recorditem" in raw or "<datalist" in raw or "<type>19</type>" in raw


def _xml_text(node: ET.Element | None, *tags: str) -> str:
    if node is None:
        return ""
    for tag in tags:
        value = node.findtext(f".//{tag}")
        if value and value.strip():
            return html.unescape(value).strip()
    return ""


def forward_items(raw: str, depth: int = 0) -> list[dict[str, Any]]:
    """Extract readable entries from WeChat merged-forward XML.

    WeChat has shipped both escaped and CDATA variants.  We intentionally
    parse only allowlisted speaker/body/link/media fields and never forward
    the original XML.
    """
    if depth > 3:
        return [{"sender": "未知发言人", "body": "[嵌套转发层级过深]", "kind": "placeholder"}]
    decoded = html.unescape(raw or "")
    blocks = re.findall(r"<recorditem\b[^>]*>(.*?)</recorditem>", decoded, re.I | re.S)
    if not blocks:
        blocks = re.findall(r"<datalist\b[^>]*>(.*?)</datalist>", decoded, re.I | re.S)
    if not blocks:
        # Some readers already return the inner list without a wrapper.
        blocks = [decoded] if re.search(r"<dataitem\b", decoded, re.I) else []
    result: list[dict[str, Any]] = []
    for block in blocks:
        dataitems = re.findall(r"<dataitem\b[^>]*>.*?</dataitem>", block, re.I | re.S)
        if not dataitems:
            dataitems = re.findall(r"<item\b[^>]*>.*?</item>", block, re.I | re.S)
        for item in dataitems or [block]:
            try:
                node = ET.fromstring(f"<root>{item}</root>")
            except ET.ParseError:
                node = None
            sender = _xml_text(node, "sourcename", "displayname", "sendername", "fromusr") or "未知发言人"
            body = _xml_text(node, "datadesc", "content", "title", "des")
            url = _xml_text(node, "url", "lowurl")
            msg_type = _xml_text(node, "type", "msgtype")
            nested = body if "<recorditem" in body or "<datalist" in body else ""
            if nested:
                child = forward_items(nested, depth + 1)
                result.append({"sender": sender, "body": format_forward_items(child, indent="  "), "kind": "nested"})
                continue
            if not body:
                body = {"3": "[图片]", "4": "[语音]", "6": "[文件]", "43": "[视频]", "47": "[表情]"}.get(msg_type, "[原消息内容不可用]")
            if body.startswith("<"):
                body = re.sub(r"<[^>]+>", " ", body)
                body = re.sub(r"\s+", " ", body).strip() or "[原消息内容不可用]"
            if url and url not in body:
                body = f"{body}\n{url}"
            result.append({"sender": sender, "body": body, "kind": "media" if body.startswith("[") else "text"})
    return result


def format_forward_items(items: list[dict[str, Any]], indent: str = "") -> str:
    return "\n".join(f"{indent}{item.get('sender') or '未知发言人'}：{item.get('body') or '[原消息内容不可用]'}" for item in items)


def quote_info(row: dict[str, Any]) -> dict[str, str] | None:
    """Extract the stable parent message reference from WeChat type-57 XML."""
    raw = str(row.get("text") or row.get("content") or "").strip()
    if "<refermsg" not in raw:
        return None
    try:
        root = ET.fromstring(raw)
        parent = root.find(".//refermsg")
        if parent is None:
            return None
        value = lambda tag: str(parent.findtext(tag) or "").strip()
        sender = value("displayname") or "未知发言人"
        content = value("content")
        parent_id = value("svrid")
        source = value("fromusr") or str(row.get("talker") or "")
        if content.startswith("<"):
            try:
                nested = ET.fromstring(html.unescape(content))
                content = (nested.findtext(".//title") or nested.findtext(".//des") or "[媒体或富文本]").strip()
            except ET.ParseError:
                content = "[媒体或富文本]"
        return {"key": f"{source}:{parent_id}" if parent_id else "", "sender": sender, "content": content or "[原消息内容不可用]", "server_id": parent_id}
    except ET.ParseError:
        return None


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


def reader_media_paths(row: dict[str, Any]) -> list[Path]:
    """Ask the read-only reader for local resources belonging to one message."""
    talker = str(row.get("talker") or "")
    local_id = str(row.get("local_id") or "")
    if not talker or not local_id:
        return []
    cmd = [str(WECHAT_READER), "media", "--talker", talker, "--local-id", local_id, "--include-local-paths", "true"]
    server_id = str(row.get("server_id") or "")
    if server_id:
        cmd.extend(["--server-id", server_id])
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=20)
        if proc.returncode:
            return []
        payload = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    found: list[Path] = []
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if "path" in str(key).lower() and isinstance(item, str) and item:
                    path = Path(item).expanduser()
                    if path.is_file():
                        found.append(path)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)
    # Preserve order while avoiding duplicate paths returned by debug fields.
    return list(dict.fromkeys(found))


def stage_media(row: dict[str, Any], kind: str) -> str | None:
    """Stage a reader-provided local file, then fall back to a normal URL."""
    local_candidates = reader_media_paths(row)
    source_path = local_candidates[0] if local_candidates else None
    media_dir = Path(os.environ.get("CHZ_MEDIA_DIR", str(MEDIA_DIR))).expanduser()
    namespace = hashlib.sha1(str(row.get("talker") or "default").encode()).hexdigest()[:10]
    local_id = int(row.get("local_id") or 0)
    if source_path:
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
            suffix = source_path.suffix.lower() or {"image": ".jpg", "video": ".mp4", "voice": ".ogg", "file": ".bin"}.get(kind, ".bin")
            path = media_dir / f"{namespace}-{local_id}{suffix}"
            if kind == "image" and suffix == ".dat":
                if not decode_wechat_dat(source_path, path):
                    return None
                path = next(media_dir.glob(f"{namespace}-{local_id}.*"), path)
            else:
                shutil.copy2(source_path, path)
            return str(path)
        except OSError as exc:
            print(f"本地媒体暂存失败 local_id={local_id}: {exc}", file=sys.stderr, flush=True)
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
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / f"{namespace}-{local_id}{ext}"
        path.write_bytes(payload)
        return str(path)
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
        namespace = hashlib.sha1(str(row.get("talker") or "default").encode()).hexdigest()[:10]
        STICKER_DIR.mkdir(parents=True, exist_ok=True)
        # GIF preserves animated stickers. Other formats are emitted as PNG
        # because Feishu's image upload path accepts it consistently.
        if source_format == "GIF" and len(frames) > 1:
            path = STICKER_DIR / f"{namespace}-{local_id}.gif"
            frames[0].convert("P", palette=Image.Palette.ADAPTIVE).save(
                path, save_all=True, append_images=[f.convert("P", palette=Image.Palette.ADAPTIVE) for f in frames[1:]],
                duration=durations, loop=int(source.info.get("loop", 0) or 0), disposal=2, optimize=True,
            )
        else:
            path = STICKER_DIR / f"{namespace}-{local_id}.png"
            frames[0].save(path, format="PNG", optimize=True)
        return str(path)
    except Exception as exc:
        print(f"贴图归一化失败 local_id={row.get('local_id')}: {exc}", file=sys.stderr, flush=True)
        return None


def cleanup_staged(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).expanduser().unlink(missing_ok=True)
    except OSError:
        pass


def decode_wechat_dat(source: Path, destination: Path) -> bool:
    """Decode common one-byte-XOR WeChat image .dat files conservatively."""
    try:
        payload = source.read_bytes()
    except OSError:
        return False
    signatures = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"GIF8", ".gif"), (b"RIFF", ".webp"))
    for signature, extension in signatures:
        if not payload:
            break
        key = payload[0] ^ signature[0]
        decoded = bytes(byte ^ key for byte in payload)
        if not decoded.startswith(signature):
            continue
        try:
            image = Image.open(BytesIO(decoded))
            image.verify()
        except Exception:
            continue
        destination.with_suffix(extension).write_bytes(decoded)
        return True
    return False


def message_key(row: dict[str, Any]) -> str:
    return f"{row.get('talker', '')}:{row.get('local_id', row.get('server_id', ''))}"


def send_row(config: dict[str, Any], row: dict[str, Any], state: dict[str, Any], store: MirrorStore) -> None:
    msg_key = message_key(row)
    kind = str(row.get("kind_name") or "text")
    quote = quote_info(row)
    # Store only the allowlisted fields selected by MirrorStore; message rows
    # can contain large base64/media fields that should not enter SQLite logs.
    binding_id = int(config.get("binding_id", 1))
    source_account = str(config.get("source_account", "default"))
    store.upsert_message(msg_key, row, kind, binding_id=binding_id, source_account=source_account, quoted_message_key=quote.get("key") if quote else None)
    if store.message_state(msg_key, binding_id) in {"sent", "placeholder"}:
        print(f"已跳过已完成消息 local_id={row.get('local_id')}", flush=True)
        return
    sender_id = str(row.get("sender_wxid") or "")
    sender_record = store.resolve_sender(source_account, sender_id) if sender_id else None
    mapped_name = (sender_record or {}).get("confirmed_name") or config.get("sender_map", {}).get(sender_id)
    display_name = str(row.get("sender") or "").strip()
    name = mapped_name or display_name or "未知发言人"
    if not mapped_name:
        # Continue mirroring with WeChat's display name while Codex asks the
        # administrator to confirm the person for future messages.
        pending_key = sender_id or f"unknown:{display_name or 'empty'}"
        store.record_sender(source_account, pending_key, display_name, None, binding_id=binding_id)
        notify_confirmation(config, store, source_account, pending_key)
        state.setdefault("pending_senders", {})[pending_key] = {
            "display_name": row.get("sender", ""),
            "last_local_id": row.get("local_id"),
        }
        print(f"身份待确认，先使用显示名同步 local_id={row.get('local_id')} display={name}", file=sys.stderr, flush=True)
    else:
        store.record_sender(source_account, sender_id, display_name, name, binding_id=binding_id)
    body = clean_body(row)
    quoted_parent = store.find_message_by_server_id(binding_id, quote["server_id"]) if quote and quote.get("server_id") else None
    quoted_key = quote.get("key") if quote else None
    if quote:
        body = f"{body}\n> {quote['sender']}：{quote['content']}"
    # Feishu limits idempotency keys to 50 bytes; reserve room for the
    # per-part suffix used by a sticker's name/image pair.
    key = "wechat-mirror-" + hashlib.sha256(msg_key.encode()).hexdigest()[:26]
    store.mark_message(msg_key, "processing", binding_id=binding_id, idempotency_key=key)
    try:
      if kind in {"sticker", "emoji"}:
        # A Feishu image message cannot carry a text caption. Send the sender
        # name as the adjacent text bubble, then upload the original sticker.
        run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：", "--idempotency-key", key + "-name"])
        image_input = stage_sticker(row)
        if image_input:
            result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--image", image_input, "--idempotency-key", key + "-image"])
            store.add_media_job(store.upsert_message(msg_key, row, kind, state="processing", binding_id=binding_id, source_account=source_account), kind, image_input, image_input, "uploaded")
            cleanup_staged(image_input)
        else:
            result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：[表情资源不可用]", "--idempotency-key", key + "-fallback"])
            store.add_media_job(store.upsert_message(msg_key, row, kind, state="processing", binding_id=binding_id, source_account=source_account), kind, None, None, "placeholder", "resource unavailable")
            store.mark_message(msg_key, "placeholder", binding_id=binding_id, feishu_message_id=str((result.get("data") or {}).get("message_id") or ""), idempotency_key=key)
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
            message_id = store.upsert_message(msg_key, row, kind, state="processing", binding_id=binding_id, source_account=source_account)
            store.add_media_job(message_id, kind, media_url(row), media_input, "uploaded")
            cleanup_staged(media_input)
        else:
            result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：[资源不可用，待人工排查]", "--idempotency-key", key + "-fallback"])
            message_id = store.upsert_message(msg_key, row, kind, state="processing", binding_id=binding_id, source_account=source_account)
            store.add_media_job(message_id, kind, media_url(row), None, "placeholder", "resource unavailable")
            store.mark_message(msg_key, "placeholder", binding_id=binding_id, feishu_message_id=str((result.get("data") or {}).get("message_id") or ""), idempotency_key=key)
            print(f"已同步 local_id={row.get('local_id')} sender={name} kind={kind}（占位符）", flush=True)
            return
      else:
        if quote and quoted_parent and quoted_parent.get("feishu_message_id"):
            result = run_lark(config, ["+messages-reply", "--message-id", str(quoted_parent["feishu_message_id"]), "--text", f"{name}：{clean_body(row)}", "--idempotency-key", key])
        else:
            result = run_lark(config, ["+messages-send", "--chat-id", config["feishu_chat_id"], "--text", f"{name}：{body}", "--idempotency-key", key])
      store.mark_message(msg_key, "sent", binding_id=binding_id, feishu_message_id=str((result.get("data") or {}).get("message_id") or ""), idempotency_key=key)
    except Exception as exc:
      store.mark_message(msg_key, "failed", binding_id=binding_id, idempotency_key=key, error=str(exc)[:2000])
      raise
    print(f"已同步 local_id={row.get('local_id')} sender={name} kind={row.get('kind_name', 'text')}", flush=True)


def stream(config: dict[str, Any], state: dict[str, Any]) -> None:
    store = MirrorStore(config["sqlite_file"])
    store.initialize(config)
    binding_id = int(config.get("binding_id", 1))
    source_account = str(config.get("source_account", "default"))
    token = store.acquire_lease(binding_id)
    atexit.register(store.release_lease, token, binding_id)
    stop_heartbeat = threading.Event()
    def heartbeat() -> None:
        while not stop_heartbeat.wait(5):
            store.heartbeat(token, binding_id)
    heartbeat_thread = threading.Thread(target=heartbeat, name="mirror-heartbeat", daemon=True)
    heartbeat_thread.start()
    while True:
        cursor = max(int(state.get("cursor", 0)), store.get_cursor())
        cmd = [str(WECHAT_READER), "tail", "--talker", config["wechat_talker"], "--since-local-id", str(cursor),
               "--limit", "100", "--poll-interval", str(config.get("poll_interval", 600)), "--follow", "--jsonl",
               "--include-media-paths", "true"]
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
                    while store.get_control(binding_id).get("status") == "paused":
                        time.sleep(1)
                    send_row(config, row, state, store)
                    state["cursor"] = max(int(state.get("cursor", 0)), int(row.get("local_id") or 0))
                    store.set_cursor(state["cursor"], binding_id)
                    store.heartbeat(token, binding_id)
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
    parser.add_argument("--binding-id", type=int, default=1, help="SQLite binding id")
    parser.add_argument("--once", action="store_true", help="启动一次 tail 快照，便于验收")
    args = parser.parse_args()
    config = runtime_paths(load_json(args.config, {}))
    if args.command in {"run", "start"} and not all(str(config.get(key) or "").strip() for key in ("wechat_talker", "feishu_chat_id", "lark_profile")):
        print("尚未完成初始化，请先运行 python -m agents.manage setup init", file=sys.stderr)
        return 2
    state_path = Path(config.get("state_file", DATA_DIR / "state" / "wechat_feishu_mirror.json"))
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    if args.binding_id != 1 and state_path.name == "wechat_feishu_mirror.json":
        state_path = state_path.parent / "bindings" / f"{args.binding_id}.json"
    config["state_file"] = str(state_path)
    sqlite_path = Path(config.get("sqlite_file", DATA_DIR / "wechat_feishu_mirror.sqlite3"))
    if not sqlite_path.is_absolute():
        sqlite_path = ROOT / sqlite_path
    config["sqlite_file"] = str(sqlite_path)
    store = MirrorStore(sqlite_path)
    store.initialize(config)
    binding = store.get_binding(args.binding_id)
    if binding:
        config.update({
            "binding_id": int(binding["id"]),
            "source_account": binding["source_account"],
            "wechat_chat_name": binding["wechat_chat_name"],
            "wechat_talker": binding["wechat_talker"],
            "feishu_chat_name": binding["feishu_chat_name"],
            "feishu_chat_id": binding["feishu_chat_id"],
            "lark_profile": binding["lark_profile"],
            "lark_app_id": binding.get("lark_app_id"),
        })
    if args.once:
        state = load_json(state_path, {"cursor": store.get_cursor(args.binding_id), "pending_senders": {}})
        # Migrate the prototype JSON cursor on the first formal start so the
        # SQLite store does not backfill messages already mirrored by it.
        if int(state.get("cursor", 0)) > store.get_cursor(args.binding_id):
            store.set_cursor(int(state["cursor"]), args.binding_id)
        config["poll_interval"] = 0.2
        # A bounded snapshot is useful in tests and never backfills history.
        cmd = [str(WECHAT_READER), "tail", "--talker", config["wechat_talker"], "--since-local-id", str(state.get("cursor", 0)),
               "--limit", "10", "--mode", "snapshot", "--include-media-paths", "true"]
        payload = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=True)
        print(payload.stdout, end="")
        return 0
    if args.command == "status":
        print(json.dumps(store.status(args.binding_id), ensure_ascii=False, indent=2))
        return 0
    if args.command in {"pause", "resume"}:
        store.set_control(args.binding_id, "paused" if args.command == "pause" else "running", actor="codex", reason=f"manual {args.command}")
        print(json.dumps(store.status(args.binding_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "stop":
        lease = store.get_lease(args.binding_id)
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
            store.release_lease(str(lease["token"]), args.binding_id)
        store.set_control(args.binding_id, "stopped", actor="codex", reason="manual stop")
        print(json.dumps(store.status(args.binding_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "start":
        current = store.status(args.binding_id)
        if current["worker"]["alive"]:
            print(json.dumps(current, ensure_ascii=False, indent=2))
            return 0
        store.set_control(args.binding_id, "running", actor="codex", reason="manual start")
        log_path = Path(config.get("log_dir", DATA_DIR / "logs")) / "wechat_feishu_mirror.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", "--config", str(args.config.resolve()), "--binding-id", str(args.binding_id)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
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
        state = load_json(state_path, {"cursor": store.get_cursor(args.binding_id), "pending_senders": {}})
        if store.get_control(args.binding_id).get("status") == "stopped":
            store.set_control(args.binding_id, "running", actor="worker", reason="worker started")
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
