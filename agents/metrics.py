"""阶段 2 验收指标：从既有表统计，不新增写入路径。

纯聚合，无 I/O——存储层给出原始行，这里只做分位数、成功率和计数，
便于对种子数据直接断言。
"""
from __future__ import annotations
import time

TERMINAL_MEDIA = ("uploaded", "placeholder", "manual", "failed")
MEDIA_KINDS = {"image", "video", "file", "voice", "audio", "media", "sticker", "emoji"}


def percentile(values, q):
    """最近位次分位数；空样本为 None。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))
    return ordered[min(rank, len(ordered)) - 1]


def _fill(values):
    shape = {"n": len(values), "p50": None, "p95": None, "max": None}
    if values:
        shape.update(p50=percentile(values, 50), p95=percentile(values, 95), max=max(values))
    return shape


def _latency_block(samples, excluded):
    text = [lag for kind, lag in samples if kind not in MEDIA_KINDS]
    media = [lag for kind, lag in samples if kind in MEDIA_KINDS]
    return {"text": _fill(text), "media": _fill(media), "excluded": excluded}


def _media_block(states):
    terminal = {name: int(states.get(name, 0)) for name in TERMINAL_MEDIA}
    in_flight = int(states.get("pending", 0)) + int(states.get("processing", 0))
    total = sum(terminal.values())
    return {**terminal, "in_flight": in_flight,
            "success_rate": round(terminal["uploaded"] / total, 4) if total else None}


def _guard_block(actions):
    guard = {}
    for action, actor in actions:
        if action == "bindings.start":
            slot = guard.setdefault(action, {})
            slot[actor] = slot.get(actor, 0) + 1
        else:
            guard[action] = guard.get(action, 0) + 1
    return guard


def _binding_block(data, cursor):
    return {
        "cursor": cursor,
        "max_ingested_local_id": data.get("max_local_id"),
        "latency": _latency_block(data.get("latency", []), data.get("excluded", 0)),
        "media_delivery": _media_block(data.get("media_states", {})),
        "stuck": {name: int(data.get("stuck_states", {}).get(name, 0)) for name in ("received", "processing", "failed")},
        "guard": _guard_block(data.get("actions", [])),
    }


def build_report(rows, window_days, now=None):
    """原始行 → 指标报表；空库也保持完整形状。"""
    binding_ids = sorted(set(rows.get("bindings", {})) | set(rows.get("cursor", {})))
    bindings, all_data = [], {"latency": [], "excluded": 0, "media_states": {}, "stuck_states": {}, "actions": [], "max_local_id": None}
    for bid in binding_ids:
        data = rows.get("bindings", {}).get(bid, {})
        bindings.append({"binding_id": bid, **_binding_block(data, rows.get("cursor", {}).get(bid))})
        for kind, lag in data.get("latency", []):
            all_data["latency"].append((kind, lag))
        all_data["excluded"] += int(data.get("excluded", 0))
        if data.get("max_local_id") is not None:
            all_data["max_local_id"] = max(all_data["max_local_id"] or 0, int(data["max_local_id"]))
        for name, count in data.get("media_states", {}).items():
            all_data["media_states"][name] = all_data["media_states"].get(name, 0) + int(count)
        for name, count in data.get("stuck_states", {}).items():
            all_data["stuck_states"][name] = all_data["stuck_states"].get(name, 0) + int(count)
        all_data["actions"].extend(data.get("actions", []))
    totals = _binding_block(all_data, None)
    return {"window_days": window_days, "generated_at": now if now is not None else time.time(),
            "bindings": bindings, "totals": totals}
