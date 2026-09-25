#!/usr/bin/env python3
"""Agent worker: mirror one WeChat chat into one Feishu group.

The worker is intentionally driven by the read-only rion-wechat-cli tail
stream and the separately configured wechat-mirror lark-cli profile.
"""
from __future__ import annotations

import argparse
import atexit
import html
import hashlib
import json
import os
import re
import selectors
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

if sys.platform == 'win32':
    import msvcrt

    def _lock_exclusive(handle, blocking: bool) -> None:
        """Whole-file advisory lock; blocking waits indefinitely like flock."""
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise
                time.sleep(0.1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_exclusive(handle, blocking: bool) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))

    def _unlock(handle) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)

try:
    from .storage import MirrorStore
    from .paths import DATA_DIR, DEFAULT_CONFIG, runtime_paths
except ImportError:
    from storage import MirrorStore
    from paths import DATA_DIR, DEFAULT_CONFIG, runtime_paths


ROOT = Path(__file__).resolve().parents[1]
WECHAT_READER = Path(os.environ.get("WECHAT_READER", str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills/wechat-cli/scripts/reader.sh")))
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


class CliError(RuntimeError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class DeliveryUncertain(RuntimeError):
    pass


class ControlChanged(RuntimeError):
    pass


def safe_error(exc):
    text = str(exc)
    text = re.sub(r'https?://[^\s"<>]+', '[链接已隐藏]', text)
    text = re.sub(r'(?i)(aeskey|app.?secret|access.?token|refresh.?token)[\s:=\"\']+[^\s,<>]+', r'\1=[已隐藏]', text)
    return text[:600]


def run_lark(config: dict[str, Any], args: list[str]) -> dict[str, Any]:
    # CLI local file flags require paths beneath cwd. Keep cwd in local data,
    # never copy private media back into the source checkout.
    arguments = list(args)
    cwd = Path(config.get('data_dir', DATA_DIR)).expanduser().resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    files = []
    for i, argument in enumerate(arguments[:-1]):
        if argument in {'--image','--file','--audio','--video','--video-cover'}:
            value = Path(arguments[i+1])
            if value.is_absolute():
                files.append((i+1, value.resolve()))
    if files:
        cwd = Path(os.path.commonpath([str(value.parent) for _, value in files]))
        for i, value in files:
            arguments[i] = str(value.relative_to(cwd))
    lark = shutil.which('lark-cli') or str(Path.home()/'.local/bin/lark-cli')
    cmd = [lark,'--profile',config['lark_profile'],'im',*arguments,'--as','bot','--format','json']
    try:
        proc = subprocess.run(cmd, cwd=cwd, text=True, encoding="utf-8", capture_output=True, timeout=180,
                              env={**os.environ,'LARKSUITE_CLI_NO_UPDATE_NOTIFIER':'1','LARKSUITE_CLI_NO_SKILLS_NOTIFIER':'1'})
    except subprocess.TimeoutExpired:
        raise CliError('飞书响应超时，发送结果待确认', uncertain=True) from None
    if proc.returncode:
        try:
            error = json.loads(proc.stderr).get('error', {})
        except ValueError:
            error = {}
        # A structured API rejection proves the operation was rejected. A
        # transport/process failure does not prove it was never accepted.
        raise CliError(safe_error(error.get('message') or '飞书 CLI 失败'), uncertain=not bool(error.get('code') or error.get('type') in {'validation','confirmation_required'}))
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        raise CliError('飞书返回了不可识别的响应', uncertain=True) from None
    if result.get('ok') is False:
        raise CliError(safe_error(result.get('error')))
    return result


def notify_confirmation(config: dict[str, Any], store: MirrorStore, source_account: str, sender_id: str) -> None:
    """Send one private reminder to the configured administrator."""
    user_id = str(config.get("admin_feishu_user_id") or "").strip()
    if not user_id or not sender_id:
        return
    item = store.claim_confirmation_notification(source_account, sender_id)
    if not item:
        return
    groups = store.list_confirmations()
    group_names = ', '.join(sorted({g['wechat_chat_name'] for x in groups if x['source_account']==source_account and x['wechat_sender_id']==sender_id for g in (x.get('groups') or [{'wechat_chat_name':x.get('wechat_chat_name') or ''}])}))
    text = f"微信群镜像待确认：编号 {item['id']}，昵称「{item.get('display_name') or '未知'}」，微信 ID {sender_id}"
    if group_names:
        text += f"，出现于：{group_names}"
    try:
        run_lark({**config, "lark_profile": config.get("admin_profile") or "wechat-mirror"}, ["+messages-send", "--user-id", user_id, "--text", text, "--idempotency-key", f"mirror-confirm-{item['id']}"])
    except Exception as exc:
        store.finish_confirmation_notification(int(item["id"]), error=safe_error(exc))
        print(f"身份提醒发送失败 confirmation_id={item['id']}: {safe_error(exc)}", file=sys.stderr, flush=True)
    else:
        store.finish_confirmation_notification(int(item["id"]))


def group_event_body(row: dict[str, Any], config: dict[str, Any], store: MirrorStore) -> str:
    raw = str(row.get('text') or row.get('content') or '')
    marker = re.search(r'\$username\$.*?邀请.*?\$names\$.*?加入了群聊', raw, re.S)
    if not marker:
        return '群成员变更（姓名暂不可用）' if ('$username$' in raw or '$names$' in raw) else (raw or '[群成员变更]')
    tokens = raw[marker.end():].strip().split()
    pairs = [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens) - 1, 2)]
    source = str(config.get('source_account') or 'default')
    profile = str(config.get('lark_profile') or '')
    def member(sender_id, nickname):
        identity = store.resolve_sender(source, sender_id)
        name = str((identity or {}).get('confirmed_name') or nickname or '成员')
        # Raw WeChat IDs must never appear as a substitute for a display name.
        if name == sender_id or name.endswith('@chatroom'):
            name = '成员'
        mention = store.mention_identity(source, sender_id, profile)
        if mention and str(mention['open_id']).startswith('ou_'):
            return f'<at user_id="{mention["open_id"]}">{html.escape(name)}</at>'
        return name
    inviter = member(*pairs[0]) if pairs else '成员'
    invited = '、'.join(member(*pair) for pair in pairs[1:]) if len(pairs) > 1 else '成员'
    return f'{inviter} 邀请 {invited} 加入了群聊'


def is_group_event(row: dict[str, Any]) -> bool:
    raw = str(row.get('text') or row.get('content') or '')
    return ('$username$' in raw or '$names$' in raw or
            (str(row.get('sender_wxid') or '').endswith('@chatroom') and
             str(row.get('kind_name') or '').lower() in {'system', 'sys', 'group_event'}))


def clean_body(row: dict[str, Any]) -> str:
    kind = str(row.get("kind_name") or "text")
    raw = str(row.get("text") or row.get("content") or "")
    if is_forward_row(row):
        items = forward_items(raw)
        if items:
            return format_forward_items(items)
        return "[转发聊天记录内容暂不可用]"
    if kind in {"sticker", "emoji"}:
        return "[表情]"
    if kind == "channel_video":
        return channel_video_body(raw)
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


def _cdata(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", value, flags=re.S)
    return value.strip()


def channel_video_body(raw: str) -> str:
    """Render a WeChat Channels share as a stable page link.

    Type-51 XML contains several signed CDN URLs (video, thumbnail, avatar),
    often mixed with an upgrade/help URL. Those URLs expire and must not be
    forwarded. ``objectId`` + ``objectNonceId`` gives a stable page address;
    a short ``weixin.qq.com/sph/`` share URL is the fallback when available.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        root = None
    def values(tag: str) -> list[str]:
        if root is None:
            return []
        return [_cdata(node.text or "") for node in root.findall(f".//{tag}") if _cdata(node.text or "")]
    object_id = next(iter(values("objectId")), "")
    nonce = next(iter(values("objectNonceId")), "")
    share = next((u for u in re.findall(r"https?://[^\s<>\"']+", raw) if re.search(r"(?:weixin\.qq\.com/sph/|channels\.weixin\.qq\.com/)", u)), "")
    if object_id and nonce:
        url = "https://channels.weixin.qq.com/web/pages/feed?oid=" + urllib.parse.quote(object_id, safe="") + "&nid=" + urllib.parse.quote(nonce, safe="")
    elif share:
        url = html.unescape(share)
    else:
        url = ""
    desc = next(iter(values("desc")), "")
    title = next(iter(values("title")), "")
    # Old clients use an upgrade notice as title while the real description is
    # still present in finderFeed.
    if not desc or "升级" in desc or "版本不支持" in desc:
        desc = ""
    if not desc and title and "升级" not in title and "版本不支持" not in title:
        desc = title
    creator = next(iter(values("nickname")), "")
    lines = ["视频号" + (f"：{desc}" if desc else "")]
    if creator:
        lines.append(f"作者：{creator}")
    if url:
        lines.append(url)
    else:
        lines.append("[视频号链接暂不可用，待人工排查]")
    return "\n".join(lines)


def is_forward_row(row: dict[str, Any]) -> bool:
    kind = str(row.get("kind_name") or row.get("kind") or "").lower()
    raw = str(row.get("text") or row.get("content") or "").lower()
    return kind in {"forward", "forward_chat", "merged_forward", "chat_history", "history"} or "<recorditem" in raw or "<datalist" in raw or "<type>19</type>" in raw


def _xml_text(node: ET.Element | None, *tags: str) -> str:
    if node is None:
        return ""
    for tag in tags:
        value = node.findtext(f".//{tag}")
        if value and value.strip():
            return html.unescape(value).strip()
    return ""


def forward_items(raw: str, depth: int = 0) -> list[dict[str, Any]]:
    from integrations.forward_records import parse_record
    return parse_record(raw, depth)


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


def reader_media_result(row: dict[str, Any]) -> tuple[list[Path], bool]:
    """Ask the read-only reader for local resources belonging to one message.

    Returns (paths, thumbnail_only); the optional contract field
    "thumbnail_only" marks a degraded image the reader could only find at
    preview quality, so the sender can flag it on the mirrored message.
    """
    talker = str(row.get("talker") or "")
    local_id = str(row.get("local_id") or "")
    if not talker or not local_id:
        return [], False
    # Rion uses a boolean flag. Its positional chat argument silently accepts
    # a trailing "true", which makes the lookup return an empty resource list.
    cmd = [str(WECHAT_READER), "media", "--talker", talker, "--local-id", local_id, "--include-local-paths"]
    server_id = str(row.get("server_id") or "")
    if server_id:
        cmd.extend(["--server-id", server_id])
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=45)
        if proc.returncode == 2 and "--include-local-paths" in proc.stderr and "expected one argument" in proc.stderr:
            # Older WDA adapters require the explicit value. Keep deployed
            # Windows readers working until they are updated.
            legacy = list(cmd)
            legacy.insert(legacy.index("--include-local-paths") + 1, "true")
            proc = subprocess.run(legacy, cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=45)
        if proc.returncode:
            return [], False
        payload = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [], False
    found = []
    thumbnail_only = False
    for resource in payload.get('data', payload).get('resources', []):
        thumbnail_only = thumbnail_only or bool(resource.get('thumbnail_only'))
        for value in resource.get('local_paths', []):
            path = Path(value).expanduser()
            if path.is_file():
                found.append(path)
        # Older reader versions used one local_path.
        if resource.get('local_path'):
            path = Path(resource['local_path']).expanduser()
            if path.is_file():
                found.append(path)
    return list(dict.fromkeys(found)), thumbnail_only


def reader_media_paths(row: dict[str, Any]) -> list[Path]:
    """Local reader resources for one message (see reader_media_result)."""
    return reader_media_result(row)[0]


def wechat_cache_paths(row: dict[str, Any]) -> list[Path]:
    """Find already-cached WeChat media without treating CDN tickets as URLs.

    The cache layout differs between WeChat releases, so callers may provide
    ``wechat_cache_dirs`` or ``WECHAT_CACHE_DIRS``.  Matching requires the
    stable local/server message identifier and never searches by a bare
    filename from an unrelated group.
    """
    roots = row.get("wechat_cache_dirs") or os.environ.get("WECHAT_CACHE_DIRS", "")
    if isinstance(roots, str):
        roots = [item for item in roots.split(os.pathsep) if item]
    local_id = str(row.get("local_id") or "")
    server_id = str(row.get("server_id") or "")
    if not roots or not (local_id or server_id):
        return []
    identifiers = tuple(value for value in (local_id, server_id) if value)
    found: list[Path] = []
    for root_value in roots:
        root = Path(root_value).expanduser()
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".dat", ".mp4"}:
                    continue
                if not any((re.search(rf"(?<!\d){re.escape(identifier)}(?!\d)", path.stem) if identifier.isdigit() else identifier in path.name) for identifier in identifiers):
                    continue
                found.append(path)
        except OSError:
            continue
    found = list(dict.fromkeys(found))
    talker = str(row.get("talker") or row.get("wechat_talker") or "")
    if talker and len(found) > 1:
        # Current desktop WeChat uses an MD5(talker) directory. Prefer that
        # namespace when present, while keeping flat fixture/cache layouts
        # usable for older clients.
        talker_hash = hashlib.md5(talker.encode()).hexdigest()
        scoped = [path for path in found if talker_hash in str(path)]
        if scoped:
            found = scoped
    return found


def media_doctor(row: dict[str, Any]) -> dict[str, Any]:
    """Report actual media availability; configuration flags alone are not enough."""
    reader_paths = reader_media_paths(row)
    cache_paths = wechat_cache_paths(row)
    return {
        "reader_configured": bool(WECHAT_READER),
        "reader_paths": [str(path) for path in reader_paths],
        "cache_paths": [str(path) for path in cache_paths],
        "available": bool(reader_paths or cache_paths),
        "status": "available" if reader_paths or cache_paths else "resource_missing",
    }


def stage_media(row: dict[str, Any], kind: str) -> tuple[str | None, bool]:
    """Stage a reader-provided local file, then fall back to a normal URL.

    Returns (path, thumbnail_only). When several local candidates exist the
    largest file wins: the desktop WeChat cache often keeps a thumbnail next
    to the original, and size reliably prefers the original.
    """
    local_candidates, thumbnail_only = reader_media_result(row)
    if not local_candidates:
        local_candidates = wechat_cache_paths(row)
    try:
        local_candidates = sorted(local_candidates, key=lambda item: item.stat().st_size, reverse=True)
    except OSError:
        pass
    source_path = local_candidates[0] if local_candidates else None
    media_dir = Path(os.environ.get("W2F_MEDIA_DIR", os.environ.get("CHZ_MEDIA_DIR", str(row.get("media_dir") or MEDIA_DIR)))).expanduser()
    namespace = str(row.get("binding_id",1)) + "-" + hashlib.sha1(str(row.get("talker") or "default").encode()).hexdigest()[:10]
    local_id = int(row.get("local_id") or 0)
    if source_path:
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
            suffix = source_path.suffix.lower() or {"image": ".jpg", "video": ".mp4", "voice": ".ogg", "file": ".bin"}.get(kind, ".bin")
            path = media_dir / f"{namespace}-{local_id}{suffix}"
            if kind == "image" and suffix == ".dat":
                if not decode_wechat_dat(source_path, path):
                    raise CliError('decode_failed: 无法解码本机图片资源')
                path = next((media_dir / f"{namespace}-{local_id}{ext}" for ext in (".png",".jpg",".gif",".webp") if (media_dir / f"{namespace}-{local_id}{ext}").is_file()), path)
            else:
                shutil.copy2(source_path, path)
            return str(path), thumbnail_only
        except OSError as exc:
            print(f"本地媒体暂存失败 local_id={local_id}: {type(exc).__name__}", file=sys.stderr, flush=True)
    url = media_url(row)
    if not url or not url.startswith(("http://", "https://")):
        return None, thumbnail_only
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "wechat-feishu-mirror/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read(100 * 1024 * 1024 + 1)
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if len(payload) > 100 * 1024 * 1024:
            raise CliError('resource_too_large: 媒体下载超过 100 MB，等待人工处理')
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "video/mp4": ".mp4", "audio/ogg": ".ogg"}.get(content_type)
        if not ext:
            ext = {"image": ".jpg", "video": ".mp4", "file": ".bin", "voice": ".ogg"}.get(kind, ".bin")
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / f"{namespace}-{local_id}{ext}"
        path.write_bytes(payload)
        return str(path), thumbnail_only
    except Exception as exc:
        if isinstance(exc,CliError):raise
        raise CliError('download_failed: 媒体下载失败 '+type(exc).__name__) from None


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
        namespace = str(row.get("binding_id",1)) + "-" + hashlib.sha1(str(row.get("talker") or "default").encode()).hexdigest()[:10]
        sticker_dir = Path(row.get("media_dir") or STICKER_DIR) / "stickers"
        sticker_dir.mkdir(parents=True, exist_ok=True)
        # GIF preserves animated stickers. Other formats are emitted as PNG
        # because Feishu's image upload path accepts it consistently.
        if source_format == "GIF" and len(frames) > 1:
            path = sticker_dir / f"{namespace}-{local_id}.gif"
            frames[0].convert("P", palette=Image.Palette.ADAPTIVE).save(
                path, save_all=True, append_images=[f.convert("P", palette=Image.Palette.ADAPTIVE) for f in frames[1:]],
                duration=durations, loop=int(source.info.get("loop", 0) or 0), disposal=2, optimize=True,
            )
        else:
            path = sticker_dir / f"{namespace}-{local_id}.png"
            frames[0].save(path, format="PNG", optimize=True)
        return str(path)
    except Exception as exc:
        print(f"贴图归一化失败 local_id={row.get('local_id')}: {type(exc).__name__}", file=sys.stderr, flush=True)
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


def delivery_key(config, key, part):
    scope = f"{config.get('source_account','default')}:{config.get('binding_id',1)}:{config.get('lark_app_id','')}:{config['feishu_chat_id']}:{key}:{part}"
    return 'wx-' + hashlib.sha256(scope.encode()).hexdigest()[:40]


def check_control(config, store):
    if not config.get('_guard'):
        return
    bid = int(config['binding_id'])
    binding = store.get_binding(bid)
    profile = next((p for p in store.list_profiles() if p['app_id'] == binding['lark_app_id']), None) if binding else None
    if not binding or binding['archived'] or not binding['enabled'] or not profile or not profile['enabled'] or store.get_control(bid)['status'] != 'running':
        raise ControlChanged('绑定已暂停、停止或应用不可用')
    config.update(binding)
    config['lark_profile'] = profile['profile_name']


def send_part(config, store, message, part, arguments):
    check_control(config, store)
    record = store.prepare_delivery(message['id'], part, arguments, delivery_key(config, message['wechat_message_key'], part))
    if record['state'] == 'sent':
        return {'data': {'message_id': record['feishu_message_id']}}
    if record['state'] in {'sending','uncertain'} and time.time() - (record['attempted_at'] or 0) > 3500:
        store.set_delivery(message['id'], part, 'uncertain', error='发送回执缺失且已超过飞书去重窗口；请核对飞书消息后确认回执')
        raise DeliveryUncertain('发送回执不确定，已停止后续发送，等待核对')
    arguments = json.loads(record['payload_json'])
    store.set_delivery(message['id'], part, 'sending')
    try:
        result = run_lark(config, [*arguments, '--idempotency-key', record['idempotency_key']])
    except Exception as exc:
        # Retrying a definitive rejection is safe even beyond the remote UUID
        # window. Unknown outcomes must preserve the original attempt time.
        definite = isinstance(exc, CliError) and not exc.uncertain
        store.set_delivery(message['id'], part, 'pending' if definite else 'uncertain', error=safe_error(exc))
        if definite:
            with store.connect() as db:
                db.execute('UPDATE deliveries SET attempted_at=NULL WHERE message_id=? AND part=?', (message['id'],part))
        raise
    remote_id = str((result.get('data') or {}).get('message_id') or '')
    if not remote_id:
        store.set_delivery(message['id'], part, 'uncertain', error='飞书未返回消息 ID')
        raise DeliveryUncertain('发送回执缺少消息 ID')
    store.set_delivery(message['id'], part, 'sent', remote_id)
    return result


def resolve_name(store, config, sender_id, nickname, unique_key):
    source = str(config.get('source_account','default'))
    identity = store.resolve_sender(source, sender_id) if sender_id else None
    if identity and identity.get('confirmed_name'):
        return identity['confirmed_name'], identity.get('person_id')
    # Without a stable ID, each observation stays separate from namesakes.
    pending = sender_id or 'unidentified:' + unique_key
    store.record_sender(source, pending, nickname, binding_id=int(config.get('binding_id',1)))
    notify_confirmation(config, store, source, pending)
    return nickname or '未知发言人', None


def split_text(text, byte_limit=12000):
    parts, current, length = [], [], 0
    for char in text:
        size = len(char.encode('utf-8'))
        if length + size > byte_limit:
            parts.append(''.join(current)); current = []; length = 0
        current.append(char); length += size
    if current:
        parts.append(''.join(current))
    return parts or ['']


def forward_body(row, config, store):
    items = row.get('forward_items') or forward_items(str(row.get('text') or row.get('content') or ''))
    def render(records, prefix=''):
        lines=[]
        for i,item in enumerate(records):
            name,_ = resolve_name(store, config, item.get('sender_id',''), item.get('sender',''), f"{message_key(row)}:forward:{prefix}{i}")
            lines.append(f"{name}：{item.get('body') or '[转发记录]'}")
            if item.get('children'):
                lines.extend('  '+line for line in render(item['children'], prefix+str(i)+'.'))
        return lines
    return '\n'.join(render(items)) if items else '[转发聊天记录内容暂不可用]'


def send_row(config: dict[str, Any], row: dict[str, Any], state: dict[str, Any], store: MirrorStore) -> None:
    key = message_key(row); bid = int(config.get('binding_id',1))
    kind = str(row.get('kind_name') or 'text')
    store.upsert_message(key,row,kind,binding_id=bid,source_account=str(config.get('source_account','default')))
    message = store.message(key,bid)
    if message['state'] in {'sent','placeholder'} and not config.get('_retry_media'):
        return
    if config.get('_guard') and config.get('_check_profile'):
        from agents.manage import profile_list
        profile_list(store)
    check_control(config,store)
    # Freeze attribution on the first send attempt so a partially delivered
    # message does not change names when it resumes.
    if str(message.get('idempotency_key') or '').startswith('wx-'):
        name, person = message['sender_name'], message.get('sender_person_id')
    elif is_group_event(row):
        name, person = '群事件', None
    else:
        name,person = resolve_name(store,config,str(row.get('sender_wxid') or ''),str(row.get('sender') or ''),key)
    store.mark_message(key,'processing',binding_id=bid,sender_name=name,sender_person_id=person,idempotency_key=delivery_key(config,key,'message'))
    message = store.message(key,bid)
    send = lambda part,args: send_part(config,store,message,part,args)
    target = ['+messages-send','--chat-id',config['feishu_chat_id']]
    if config.get('_retry_media') and message.get('feishu_message_id'):
        target=['+messages-reply','--message-id',message['feishu_message_id']]
    placeholder = False
    try:
        transcript = str(row.get('voice_transcript') or row.get('voiceTranscript') or '').strip()
        if kind in {'voice','audio'} and transcript:
            result=send('text-0',target+['--text',f'{name}：{transcript}'])
        elif kind in {'sticker','emoji','image','video','file','voice','audio','media'}:
            # A saved receipt lets an upload survive a crash after the file was
            # deleted, without attempting to download an expired URL again.
            sent_media = store.delivery(message['id'],'media')
            fallback = store.delivery(message['id'],'fallback')
            media_input = None
            if sent_media and sent_media['state']=='sent':
                result = {'data':{'message_id':sent_media['feishu_message_id']}}
            elif fallback and fallback['state']=='sent' and not config.get('_retry_media'):
                result = {'data':{'message_id':fallback['feishu_message_id']}}; placeholder=True
            else:
                label = {'sticker':'表情','emoji':'表情','image':'图片','video':'视频','file':'文件','voice':'语音','audio':'语音'}.get(kind,'媒体')
                enriched = {**row,'binding_id':bid,'media_dir':config.get('media_dir', str(MEDIA_DIR))}
                try:
                    if kind in {'sticker','emoji'}:
                        media_input,thumbnail_only = stage_sticker(enriched),False
                    else:
                        media_input,thumbnail_only = stage_media(enriched,kind)
                    if not media_input:
                        raise CliError('resource_missing: 本机未找到可上传资源')
                    if not config.get('_retry_media'):
                        note='（原图未同步，已发送缩略图）' if thumbnail_only else ''
                        send('name',target+['--text',f'{name}：{note}'])
                    flag='--image' if kind in {'image','sticker','emoji'} else '--file'
                    media_args = [flag,media_input]
                    if kind=='video':
                        cover = video_cover(Path(media_input))
                        if cover:
                            media_args=['--video',media_input,'--video-cover',str(cover)]
                    elif kind in {'audio','voice'} and Path(media_input).suffix in {'.opus','.ogg'}:
                        media_args=['--audio',media_input]
                    result=send('media',target+media_args)
                    store.add_media_job(message['id'],kind,None,media_input,'uploaded')
                    cleanup_staged(media_input)
                    if kind=='video' and cover: cleanup_staged(str(cover))
                except CliError as exc:
                    if exc.uncertain: raise
                    result=send('fallback',target+['--text',f'{name}：[{label}资源不可用，待人工排查]'])
                    store.add_media_job(message['id'],kind,None,media_input,'placeholder',safe_error(exc))
                    placeholder=True
        else:
            plan_key=f'render:{message["id"]}'
            plan=store.setting(plan_key)
            if not plan:
                body = group_event_body(row,config,store) if is_group_event(row) else (forward_body(row,config,store) if is_forward_row(row) else clean_body(row))
                quote=quote_info(row)
                parent=store.find_message_by_server_id(bid,quote['server_id']) if quote and quote.get('server_id') else None
                if quote and parent and parent.get('feishu_message_id'):
                    target=['+messages-reply','--message-id',parent['feishu_message_id']]
                elif quote:
                    raw=str(row.get('text') or row.get('content') or '')
                    try:
                        node=ET.fromstring(raw).find('.//refermsg')
                        sender_id=node.findtext('chatusr') or node.findtext('fromusr') or ''
                        if sender_id.endswith('@chatroom'): sender_id=''
                    except (ET.ParseError,AttributeError): sender_id=''
                    quoted_name,_=resolve_name(store,config,sender_id,quote['sender'],key+':quote')
                    body += f"\n> {quoted_name}：{quote['content']}"
                raw_parts=split_text(body)
                total=len(raw_parts)
                parts=[f'{name}转发聊天记录（第 {i}/{total} 段）\n{part}' for i,part in enumerate(raw_parts,1)] if is_forward_row(row) else [f'{name}：{part}' for part in raw_parts]
                plan={'target':target,'parts':parts,'placeholder':is_forward_row(row) and body=='[转发聊天记录内容暂不可用]'}
                store.save_setting(plan_key,plan)
            for index,part in enumerate(plan['parts']):
                result=send(f'text-{index}',plan['target']+['--text',part])
            placeholder=plan['placeholder']
        store.mark_message(key,'placeholder' if placeholder else 'sent',binding_id=bid,feishu_message_id=message['feishu_message_id'] if config.get('_retry_media') else str(result['data']['message_id']))
    except Exception as exc:
        store.mark_message(key,'failed',binding_id=bid,error=safe_error(exc))
        raise
    print(f'已同步 binding={bid} local_id={row.get("local_id")} kind={kind}',flush=True)


def video_cover(path):
    if not shutil.which('ffmpeg'):
        return None
    cover = path.with_suffix('.cover.jpg')
    try:
        result=subprocess.run(['ffmpeg','-v','error','-y','-i',str(path),'-frames:v','1','-vf','scale=640:-2',str(cover)],capture_output=True,timeout=30)
        return cover if result.returncode==0 and cover.exists() else None
    except (OSError,subprocess.TimeoutExpired): return None


def source_page(config, cursor, high=None, after=None):
    args=[str(WECHAT_READER),'sync-page','--talker',config['wechat_talker'],'--after-local-id',str(cursor),'--limit',str(config.get('page_size',100))]
    if high is not None: args+=['--through-local-id',str(high)]
    if after: args+=['--page-after-time',str(after['time']),'--page-after-local-id',str(after['local_id'])]
    try:
        proc=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=60)
        value=json.loads(proc.stdout)
    except (OSError,subprocess.TimeoutExpired,ValueError):
        raise RuntimeError('微信读取失败，请检查 CLI 和 sync-page 扩展') from None
    if proc.returncode or value.get('ok') is False:
        raise RuntimeError(safe_error(value.get('error',{})))
    data=value.get('data',{})
    if data.get('contract')!='ordered_sync_v1':
        raise RuntimeError('读取器缺少完整分页契约，保留游标并停止补发')
    return data


def recover_once(config,store,reader=source_page):
    """Ingest a fixed source interval durably, drain it, then advance cursor.

    Both normal polling and offline recovery use this exact pipeline. No
    sleeps between backlog pages, and no newer snapshot overtakes this one.
    """
    bid=int(config['binding_id']); check_control(config,store)
    state=store.recovery(bid); base=store.get_cursor(bid)
    if not state['initialized']:
        page=reader(config,0)
        store.initialize_cursor(bid,int(page['through_local_id']))
        store.update_recovery(bid,phase='idle',remaining=0,last_scan_at=time.time())
        return 0
    high=state.get('high'); after=state.get('after')
    if not state.get('scan_complete'):
        while True:
            check_control(config,store)
            page=reader(config,base,high,after)
            if page.get('contract')!='ordered_sync_v1': raise RuntimeError('无完整分页契约')
            high=int(page['through_local_id'])
            rows=page['messages']
            if high < base or (page['has_more'] and (not rows or page.get('next')==after)):
                raise RuntimeError('源分页游标没有推进；保留同步位置')
            for row in rows:
                if row.get('talker')!=config['wechat_talker'] or not base < int(row['local_id']) <= high:
                    raise RuntimeError('分页源群或消息范围不匹配')
                store.upsert_message(message_key(row),row,row.get('kind_name','text'),binding_id=bid,source_account=config.get('source_account','default'))
            after=page.get('next') or after
            # Persist pagination only after every row in this page is durable.
            store.update_recovery(bid,phase='scanning',high=high,after=after,scan_complete=not page['has_more'],remaining=len(store.pending_messages(bid)),last_scan_at=time.time(),error=None)
            if not page['has_more']: break
    pending=store.pending_messages(bid)
    store.update_recovery(bid,phase='catching_up' if pending else 'idle',remaining=len(pending))
    delivered=0
    for item in pending:
        check_control(config,store)
        row=json.loads(item['payload_json'])
        send_row(config,row,{},store)
        delivered+=1
        store.update_recovery(bid,phase='catching_up',remaining=len(pending)-delivered,last_delivery_at=time.time())
    # No cursor can jump over an unsent row. A crash before this commit replays
    # the same interval, whose saved receipts suppress duplicates.
    store.set_cursor(int(high if high is not None else base),bid)
    store.update_recovery(bid,phase='idle',remaining=0,high=None,after=None,scan_complete=False,error=None,completed_at=time.time())
    return delivered


def retry_media_jobs(config,store):
    with store.connect() as db:
        jobs=[dict(r) for r in db.execute("SELECT m.id,m.message_id,m.attempts,x.payload_json FROM media_jobs m JOIN messages x ON x.id=m.message_id WHERE x.binding_id=? AND m.state IN ('pending','processing') ORDER BY m.id",(config['binding_id'],))]
    for job in jobs:
        if job['attempts'] >= 3:
            with store.connect() as db: db.execute("UPDATE media_jobs SET state='manual',error=COALESCE(error,'自动重试已达到 3 次'),updated_at=? WHERE id=?",(time.time(),job['id']))
            continue
        check_control(config,store)
        # Back off automatically: attempts 1/2 wait 30s/120s; tests and
        # explicit management retries can bypass this by setting updated_at.
        delay=(30,120)[min(job['attempts'],2)-1] if job['attempts'] else 0
        with store.connect() as db:
            updated=db.execute('SELECT updated_at FROM media_jobs WHERE id=?',(job['id'],)).fetchone()[0]
        if delay and time.time()-updated < delay:
            continue
        with store.connect() as db:db.execute("UPDATE media_jobs SET state='processing' WHERE id=?",(job['id'],))
        try:send_row({**config,'_retry_media':True},json.loads(job['payload_json']),{},store)
        except Exception as exc:
            with store.connect() as db:
                attempts=db.execute('SELECT attempts FROM media_jobs WHERE id=?',(job['id'],)).fetchone()[0]
                state='manual' if attempts >= 3 else 'pending'
                db.execute("UPDATE media_jobs SET state=?,error=?,updated_at=? WHERE id=?",(state,safe_error(exc),time.time(),job['id']))


def expire_failed_media(config,store):
    root=Path(config.get('media_dir',MEDIA_DIR)).resolve()
    with store.connect() as db:
        jobs=db.execute("SELECT id,staging_path FROM media_jobs WHERE state IN ('failed','placeholder','manual') AND updated_at<? AND staging_path IS NOT NULL",(time.time()-7*86400,)).fetchall()
        for job in jobs:
            path=Path(job['staging_path']).resolve()
            if path.is_relative_to(root):path.unlink(missing_ok=True)
            db.execute('UPDATE media_jobs SET staging_path=NULL WHERE id=?',(job['id'],))


def reader_wakeup_loop(config: dict[str, Any], wakeup: threading.Event, stopped: threading.Event) -> None:
    """Use Rion's read-only watch as a hint; sync-page remains authoritative."""
    store = MirrorStore(config['sqlite_file'])
    while not stopped.is_set():
        cursor = store.get_cursor(int(config['binding_id']))
        cmd = [str(WECHAT_READER), 'watch', '--talker', config['wechat_talker'],
               '--since-local-id', str(cursor), '--follow', '--jsonl',
               '--poll-interval', '2', '--limit', '100', '--scan-limit', '1000']
        proc = None
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while not stopped.is_set() and proc.poll() is None:
                    for _key, _mask in selector.select(timeout=0.5):
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk: break
                        # Rows are only wake-up signals. Never log or trust this
                        # stream for ordering; recover_once scans the durable DB.
                        wakeup.set()
                # Drain a final chunk from a reader that exited after emitting.
                if proc.poll() is not None and not stopped.is_set():
                    if os.read(proc.stdout.fileno(), 65536):wakeup.set()
        except (OSError, ValueError):
            pass  # Keep the normal poll as the safety net.
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=2)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=2)
            if proc is not None and proc.stdout:proc.stdout.close()
        stopped.wait(5)


def realtime_wakeup_loop(config: dict[str, Any], wakeup: threading.Event, stopped: threading.Event) -> None:
    """Wake the worker as soon as the reader reports source changes.

    Subscribes to the adapter's SSE change stream (WeChatDataAnalysis on
    Windows; other readers may expose the same shape). The event only skips
    the remaining poll wait — every scan still runs the regular cursor and
    receipt pipeline via recover_once. Reconnects periodically on failure.
    """
    base = str(config.get('realtime_stream_url') or os.environ.get('WDA_BASE', 'http://localhost:10392')).rstrip('/')
    url = base + '/api/chat/realtime/stream'
    while not stopped.wait(5):
        try:
            request = urllib.request.Request(url, headers={'Accept': 'text/event-stream'})
            with urllib.request.urlopen(request, timeout=600) as response:
                wakeup.set()  # a fresh stream counts as one immediate scan
                for raw in response:
                    if stopped.is_set():
                        return
                    line = raw.decode('utf-8', 'replace').strip()
                    if line.startswith('data:'):
                        wakeup.set()
        except (OSError, ValueError):
            continue  # dropped stream; retry after the wait above


def stream(config, state=None):
    store=MirrorStore(config['sqlite_file']); store.initialize()
    bid=int(config['binding_id']); token=store.acquire_lease(bid)
    stopped=threading.Event()
    wakeup=threading.Event()
    def heartbeat():
        while not stopped.wait(5): store.heartbeat(token,bid)
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    if config.get('realtime_wakeup'):
        threading.Thread(target=realtime_wakeup_loop,args=(config,wakeup,stopped),daemon=True).start()
    if str(config.get('reader_wakeup', '')).lower() in {'true', '1', 'yes'}:
        threading.Thread(target=reader_wakeup_loop,args=(config,wakeup,stopped),daemon=True).start()
    try:
        wait_until=0
        while True:
            binding=store.get_binding(bid)
            control=store.get_control(bid)['status']
            if control=='stopped' or binding['archived'] or not binding['enabled']: return
            if control=='paused':
                store.update_recovery(bid,phase='paused');stopped.wait(0.5);wait_until=0;continue
            if time.monotonic()<wait_until:
                stopped.wait(0.1)
                if wakeup.is_set(): wakeup.clear();wait_until=0
                continue
            try:
                # Reconcile CLI profile names/App IDs before each source pass.
                from agents.manage import profile_list
                profile_list(store)
                check_control(config,store)
                delivered = recover_once(config,store)
                retry_media_jobs(config,store)
                expire_failed_media(config,store)
                wait_until=time.monotonic()+(0 if delivered else float(config.get('poll_interval',600)))
            except ControlChanged:
                wait_until=0
            except Exception as exc:
                phase='needs_review' if isinstance(exc,DeliveryUncertain) else 'disconnected'
                store.update_recovery(bid,phase=phase,error=safe_error(exc),failed_at=time.time())
                print(f'同步暂缓 binding={bid}: {safe_error(exc)}',file=sys.stderr,flush=True)
                wait_until=time.monotonic()+min(60,float(config.get('poll_interval',600)))
    finally:
        stopped.set();thread.join(timeout=1);store.release_lease(token,bid)


def worker_lock(store,bid):
    directory=store.path.resolve().parent/'locks';directory.mkdir(parents=True,exist_ok=True)
    return directory/f'binding-{bid}.lock'


def main():
    # JSON on stdout must be stable UTF-8 for every consumer; Windows pipes
    # would otherwise fall back to the local code page.
    for pipe in (sys.stdout, sys.stderr):
        if hasattr(pipe, 'reconfigure'):
            pipe.reconfigure(encoding='utf-8', errors='replace')
    parser=argparse.ArgumentParser()
    parser.add_argument('command',nargs='?',choices=('run','start','stop','pause','resume','status'),default='run')
    parser.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    parser.add_argument('--db',type=Path)
    parser.add_argument('--binding-id',type=int,default=1)
    parser.add_argument('--once',action='store_true',help='Read one source page without sending or advancing cursor')
    args=parser.parse_args();config=runtime_paths(load_json(args.config,{}))
    # A configured reader path must hold for every process in the chain
    # (manager, spawner, worker) regardless of inherited environment.
    reader_override = str(config.get('wechat_reader') or '').strip()
    if reader_override:
        global WECHAT_READER
        os.environ['WECHAT_READER'] = reader_override
        WECHAT_READER = Path(reader_override)
    if args.db: config['sqlite_file']=str(args.db.resolve())
    store=MirrorStore(config['sqlite_file']);store.initialize(config)
    bid=args.binding_id;binding=store.get_binding(bid)
    if not binding:
        print(json.dumps({'error':'绑定不存在，请先完成管理台引导'},ensure_ascii=False));return 2
    config.update(binding);config['binding_id']=bid;config['_guard']=True;config['_check_profile']=True
    if args.once:
        print(json.dumps(source_page(config,store.get_cursor(bid)),ensure_ascii=False));return 0
    if args.command=='status':
        print(json.dumps(store.status(bid),ensure_ascii=False,indent=2));return 0
    if args.command in {'stop','pause','resume'}:
        value={'stop':'stopped','pause':'paused','resume':'running'}[args.command]
        store.set_control(bid,value,reason='manual '+args.command)
        # Worker checks control between rows and while idle. No blind PID kill
        # (a stale PID may refer to a different process).
        if args.command=='stop':
            deadline=time.monotonic()+8
            while time.monotonic()<deadline and store.status(bid)['worker']['alive']: time.sleep(0.2)
        print(json.dumps(store.status(bid),ensure_ascii=False,indent=2));return 0
    if binding['archived'] or not binding['enabled']:
        raise ValueError('已归档或停用的绑定不能启动')
    profile=next((p for p in store.list_profiles() if p['app_id']==binding['lark_app_id']),None)
    if not profile or not profile['enabled']: raise ValueError('发送应用已停用或缺失')
    if args.command=='start':
        # A launch lock serializes simultaneous UI/CLI starts while the child
        # acquires its lifetime lock. Dead leases never defeat the file lock.
        with worker_lock(store,bid).with_suffix('.launch').open('a') as launch:
            _lock_exclusive(launch,blocking=True)
            with worker_lock(store,bid).open('a') as check:
                try: _lock_exclusive(check,blocking=False)
                except OSError:
                    print(json.dumps(store.status(bid),ensure_ascii=False));return 0
                _unlock(check)
            store.set_control(bid,'running',reason='manual start')
            log=Path(config['log_dir'])/f'binding-{bid}.log';log.parent.mkdir(parents=True,exist_ok=True)
            with log.open('a', encoding='utf-8', errors='replace') as out:
                child=subprocess.Popen([sys.executable,'-m','agents.wechat_feishu_mirror','run','--config',str(args.config.resolve()),'--db',str(store.path.resolve()),'--binding-id',str(bid)],cwd=ROOT,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if child.poll() is not None: raise RuntimeError('Worker 启动失败，请查看绑定日志')
                if store.status(bid)['worker']['alive']: break
                time.sleep(0.1)
            print(json.dumps(store.status(bid),ensure_ascii=False));return 0
    with worker_lock(store,bid).open('a') as lock:
        try: _lock_exclusive(lock,blocking=False)
        except OSError: return 1
        def stop(signum, frame): raise KeyboardInterrupt
        signal.signal(signal.SIGTERM,stop)
        try: stream(config)
        except KeyboardInterrupt: pass
    return 0


if __name__=='__main__': raise SystemExit(main())
