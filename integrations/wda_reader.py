"""Windows WeChat reader adapter: WeChatDataAnalysis API -> w2f reader contract.

Replaces the macOS-only rion reader on Windows by serving the same CLI
commands (`sync-page`, `resolve-chat`, `media`) against a locally running
WeChatDataAnalysis (WDA) desktop API. Output and errors follow the JSON
shapes that agents/wechat_feishu_mirror.py and agents/manage.py expect:

- sync-page   -> {"ok":true,"data":{"contract":"ordered_sync_v1",...}}
- resolve-chat -> {"ok":true,"data":{"candidates":[...]}}
- media       -> {"ok":true,"data":{"resources":[...]}} (empty until ticket 05)

Configuration via environment: WDA_BASE (default http://localhost:10392).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WDA_BASE = os.environ.get("WDA_BASE", "http://localhost:10392").rstrip("/")
UA = {"User-Agent": "w2f-wda-reader/0.1"}


class WdaError(RuntimeError):
    pass


def api_get(path: str, **params) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{WDA_BASE}{path}?{query}"
    request = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise WdaError(f"WDA HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WdaError(f"WDA 不可达（{WDA_BASE}）：{exc}") from None


def member_names(talker: str) -> dict[str, str]:
    """wxid -> display name for one group; best effort, never fatal."""
    try:
        payload = api_get("/api/chat/contacts/group_members", username=talker, limit=1000)
        return {str(m.get("username") or ""): str(m.get("displayName") or "")
                for m in payload.get("members", []) if m.get("username")}
    except WdaError:
        return {}


def latest_local_id(talker: str) -> int:
    payload = api_get("/api/chat/messages", username=talker, order="desc", limit=1)
    messages = payload.get("messages") or []
    return int(messages[0]["localId"]) if messages else 0


def fetch_window(talker: str, want: int) -> list[dict]:
    """Newest `want` messages, oldest first, paging backwards via offset."""
    collected: list[dict] = []
    offset, window = 0, 500
    while len(collected) < want:
        payload = api_get("/api/chat/messages", username=talker, order="asc",
                          limit=window, offset=offset)
        batch = payload.get("messages") or []
        if not batch:
            break
        collected = batch + collected
        if not payload.get("hasMore"):
            break
        offset += len(batch)
    return collected


def map_row(talker: str, message: dict, names: dict[str, str]) -> dict:
    """One WDA message -> one w2f payload row (see ticket 02 mapping table)."""
    wda_type = int(message.get("type") or 0)
    base, sub = wda_type & 0xFFFFFFFF, wda_type >> 32
    content = str(message.get("content") or "")
    sender_wxid = str(message.get("senderUsername") or "")
    row = {
        "talker": talker,
        "local_id": int(message.get("localId") or 0),
        "server_id": str(message.get("serverId") or ""),
        "sender_wxid": sender_wxid,
        "sender": names.get(sender_wxid, sender_wxid),
        "create_time": int(message.get("createTime") or 0),
    }

    if base == 1:
        kind, text = "text", content
    elif base == 3:
        kind, text = "image", content          # XML with cdnurl -> w2f media_url
    elif base == 34:
        kind, text = "voice", content
    elif base == 43 or wda_type == 62:
        kind, text = "video", content
    elif base == 47:
        kind, text = "emoji", content          # XML cdnurl -> stage_sticker
    elif base == 48:
        place = str(message.get("locationPoiname") or message.get("locationLabel") or "").strip()
        kind, text = "text", f"[位置] {place}" if place else "[位置]"
    elif base == 50:
        kind, text = "text", "[语音通话]" if str(message.get("voipType")) == "0" else "[通话]"
    elif base == 10002:
        kind, text = "text", "[消息已撤回]"
    elif base == 10000:
        kind, text = "text", content or "[系统消息]"
    elif base == 49:
        kind, text = _map_app_message(sub, message, content)
    else:
        kind, text = "text", content           # w2f clean_body 兜底提取/占位

    row["kind_name"] = kind
    row["text"] = text
    if kind == "voice":
        transcript = str(message.get("voiceTranscript") or "").strip()
        if transcript:
            row["voice_transcript"] = transcript
    if kind == "forward_chat":
        row["forward_items"] = _forward_items(content)
    return row


def _map_app_message(sub: int, message: dict, content: str) -> tuple[str, str]:
    if sub == 57:
        return "text", content                 # keep refermsg XML for quote_info
    if sub == 19:
        return "forward_chat", content
    if sub in (6, 74):
        return "file", content
    if sub == 51:
        return "channel_video", content
    if sub in (5, 4, 68, 3, 63, 33, 36):       # links, articles, mini programs
        title = str(message.get("title") or "").strip()
        url = str(message.get("url") or "").strip()
        return "text", f"{title}\n{url}" if title and url else (title or "[富文本消息]")
    if sub == 2000:
        return "text", "[转账] " + str(message.get("title") or "").strip()
    if sub == 2001:
        return "text", "[红包]"
    if sub == 62:
        return "text", content                 # 拍一拍，WDA 已渲染模板文本
    if content:
        return "text", content
    return "text", "[富文本消息]"


def _forward_items(content: str) -> list:
    """Pre-parse merged-forward records, mirroring reader_sync.py behaviour."""
    if "<recorditem" not in content and "<datalist" not in content:
        return []
    try:
        from forward_records import parse_record
        return parse_record(content)
    except Exception:
        return []


def cmd_sync_page(args: argparse.Namespace) -> dict:
    talker = args.talker
    after = int(args.after_local_id)
    high = latest_local_id(talker)
    if args.through_local_id is not None:
        high = min(high, int(args.through_local_id))
    if high <= after:
        return _page([], high, False, None)

    # Member names and the message window are independent lookups; the WDA
    # API costs ~2s per call, so overlap them on the delivery path.
    names: dict[str, str] = {}
    window: list[dict] = []
    def load_names() -> None:
        nonlocal names
        names = member_names(talker)
    def load_window() -> None:
        nonlocal window
        window = fetch_window(talker, want=high - after)
    threads = [threading.Thread(target=load_names), threading.Thread(target=load_window)]
    for t in threads: t.start()
    for t in threads: t.join()
    pending = window
    after_time = args.page_after_time
    after_local = int(args.page_after_local_id or 0)

    def visible(message: dict) -> bool:
        local_id = int(message.get("localId") or 0)
        if not after < local_id <= high:
            return False
        if after_time is not None:
            created = int(message.get("createTime") or 0)
            if created < int(after_time) or (created == int(after_time) and local_id <= after_local):
                return False
        return True

    rows = [map_row(talker, m, names) for m in pending if visible(m)]
    rows.sort(key=lambda r: r["local_id"])
    rows = rows[: max(1, int(args.limit))]
    has_more = after + len(rows) < high and bool(rows)
    # `next` anchors on the last row of this page so the next call resumes
    # exactly after it even when several messages share one timestamp.
    last = rows[-1] if rows else None
    nxt = {"time": last["create_time"], "local_id": last["local_id"]} if last and has_more else None
    return _page(rows, high, has_more, nxt)


def _page(rows: list, high: int, has_more: bool, nxt: dict | None) -> dict:
    return {"ok": True, "data": {"contract": "ordered_sync_v1", "messages": rows,
                                 "through_local_id": int(high), "has_more": bool(has_more),
                                 "next": nxt}}


def cmd_resolve_chat(args: argparse.Namespace) -> dict:
    query = (args.query or "").strip().lower()
    limit = max(1, int(args.limit))
    payload = api_get("/api/chat/sessions", page=1, page_size=500)
    candidates = []
    for session in payload.get("sessions") or []:
        if args.type_filter == "group" and not session.get("isGroup"):
            continue
        name = str(session.get("name") or "")
        username = str(session.get("username") or "")
        if query and query not in name.lower() and query not in username.lower():
            continue
        candidates.append({"name": name, "username": username, "display_name": name})
        if len(candidates) >= limit:
            break
    return {"ok": True, "data": {"candidates": candidates}}


def find_message(talker: str, local_id: int) -> dict | None:
    """Locate one message in the newest window (recent-media retry case)."""
    for message in fetch_window(talker, want=500):
        if int(message.get("localId") or 0) == local_id:
            return message
    return None


CONTENT_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "video/mp4": ".mp4", "video/quicktime": ".mov",
    "audio/aac": ".aac", "audio/mp4": ".m4a", "audio/amr": ".amr", "audio/silk": ".silk",
}


def fetch_media(path: str, **params) -> tuple[bytes, str] | None:
    """Download decrypted media bytes; None on 404/absent resource."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    request = urllib.request.Request(f"{WDA_BASE}{path}?{query}", headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                return None
            return response.read(), (response.headers.get("Content-Type") or "").split(";")[0].strip()
    except urllib.error.HTTPError:
        return None
    except (urllib.error.URLError, OSError) as exc:
        raise WdaError(f"WDA 不可达（{WDA_BASE}）：{exc}") from None


def _cached_file(root: Path, key: str) -> Path | None:
    return next((p for p in root.glob(f"{key}.*") if p.is_file() and p.stat().st_size > 0), None)


def _media_result(target: Path, thumbnail_only: bool) -> dict:
    resource: dict = {"local_paths": [str(target)]}
    if thumbnail_only:
        resource["thumbnail_only"] = True
    return {"ok": True, "data": {"resources": [resource]}}


def cmd_media(args: argparse.Namespace) -> dict:
    """Real local file for one message's media, per the HTTP-staging route.

    Images prefer the original: the desktop cache usually holds only a
    thumbnail, so the first attempt asks WDA to fetch the original from CDN
    (fetch_remote); when that fails (expired/absent) we fall back to the
    thumbnail and flag it "thumbnail_only" so the worker can say so on the
    mirrored message. Empty resources keep w2f on its placeholder +
    media_jobs retry path; we never fabricate a path. Errors on WDA reach
    stay fatal (exit 1) so the worker surfaces the disconnect instead of
    mass-placeholdering.
    """
    talker = args.talker
    local_id = int(args.local_id)
    message = find_message(talker, local_id)
    if not message:
        return {"ok": True, "data": {"resources": []}}

    wda_type = int(message.get("type") or 0)
    base = wda_type & 0xFFFFFFFF
    server_id = str(message.get("serverId") or args.server_id or "")
    attempts: list[tuple[str, dict, str, bool]] = []
    if message.get("imageMd5"):
        md5 = message["imageMd5"]
        attempts.append(("/api/chat/media/image",
                         {"md5": md5, "deep_scan": "true", "fetch_remote": "true"},
                         f"{md5}.orig", False))
        attempts.append(("/api/chat/media/image",
                         {"md5": md5, "deep_scan": "true"},
                         f"{md5}.thumb", True))
    if message.get("emojiMd5"):
        attempts.append(("/api/chat/media/emoji", {"md5": message["emojiMd5"], "username": talker}, message["emojiMd5"], False))
    if message.get("videoMd5"):
        attempts.append(("/api/chat/media/video", {"md5": message["videoMd5"], "username": talker,
                                                   "server_id": server_id or None}, message["videoMd5"], False))
    if message.get("fileMd5"):
        attempts.append((f"/api/media/resource/{message['fileMd5']}", {}, message["fileMd5"], False))
    if base == 34 and server_id.isdigit():
        attempts.append(("/api/chat/media/voice", {"server_id": int(server_id)}, f"voice-{server_id}", False))

    root = Path(os.environ.get("W2F_WDA_CACHE", str(Path.home() / ".local/share/w2f-cli/wda-cache")))
    root.mkdir(parents=True, exist_ok=True)
    for path, params, cache_key, thumbnail_only in attempts:
        if thumbnail_only:
            # An earlier run may already have staged the original; serve it.
            # Match the .orig suffix exactly — a bare prefix would also hit
            # this attempt's own .thumb cache file.
            original = _cached_file(root, cache_key.removesuffix(".thumb") + ".orig")
            if original:
                return _media_result(original, False)
        cached = _cached_file(root, cache_key)
        if cached:
            return _media_result(cached, thumbnail_only)
        fetched = fetch_media(path, **params)
        if not fetched or not fetched[0]:
            continue
        payload, content_type = fetched
        target = root / f"{cache_key}{CONTENT_TYPES.get(content_type, '.bin')}"
        target.write_bytes(payload)
        return _media_result(target, thumbnail_only)
    return {"ok": True, "data": {"resources": []}}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 默认 GBK 会损坏中文
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="wda_reader", description="WDA -> w2f 读取器适配层")
    sub = parser.add_subparsers(dest="command", required=True)

    page = sub.add_parser("sync-page", help="有序增量分页（ordered_sync_v1）")
    page.add_argument("--talker", required=True)
    page.add_argument("--after-local-id", type=int, default=0)
    page.add_argument("--through-local-id", type=int)
    page.add_argument("--page-after-time", type=int)
    page.add_argument("--page-after-local-id", type=int, default=0)
    page.add_argument("--limit", type=int, default=100)
    page.set_defaults(handler=cmd_sync_page)

    resolve = sub.add_parser("resolve-chat", help="搜索会话（供管理台选群）")
    resolve.add_argument("query", nargs="?", default="")
    resolve.add_argument("--type-filter", default="group")
    resolve.add_argument("--limit", type=int, default=30)
    resolve.set_defaults(handler=cmd_resolve_chat)

    media = sub.add_parser("media", help="单条消息的本地媒体资源（票 05 实现）")
    media.add_argument("--talker", required=True)
    media.add_argument("--local-id", required=True)
    media.add_argument("--server-id")
    media.add_argument("--include-local-paths", default="true")
    media.set_defaults(handler=cmd_media)

    args = parser.parse_args()
    try:
        result = args.handler(args)
    except WdaError as exc:
        print(json.dumps({"ok": False, "error": {"message": str(exc)}}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
