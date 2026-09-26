"""Behavioral probes using synthetic SQLite data and intercepted external I/O.

This module uses only the standard library; the product imports its normal
dependencies. Missing dependencies and incomplete probes are blocked, never
reported as product defects. Successful synthetic media probes do not validate
the live macOS reader, codecs, credentials, or Feishu's rendering (#14).
"""
from __future__ import annotations

import base64
import contextlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from unittest.mock import patch


class ProbeBlocked(RuntimeError):
    """The harness could not safely complete a scenario."""


class CheckFailed(AssertionError):
    """An explicit behavioral expectation failed, with synthetic evidence."""

    def __init__(self, actual: str, evidence: list[str]):
        super().__init__(actual)
        self.actual, self.evidence = actual, evidence


@dataclass(frozen=True)
class Probe:
    id: str
    title: str
    expected: str
    run: Callable
    related_issues: tuple[int, ...] = ()
    category: str = "bug"


def require(condition: bool, actual: str, evidence: list[str]) -> None:
    if not condition:
        raise CheckFailed(actual, evidence)


def _deny_external(*args, **kwargs):
    raise ProbeBlocked("An external operation was not replaced by a fixture")


class Sandbox:
    def __init__(self, root, store, manager, mirror, config):
        self.root, self.store, self.manager = root, store, manager
        self.mirror, self.config = mirror, config
        self.sent = []
        self.receipts = {}

    def sink(self, config, args):
        key = args[args.index("--idempotency-key") + 1]
        if key not in self.receipts:
            self.sent.append(list(args))
            self.receipts[key] = {"data": {"message_id": f"om_synthetic_{len(self.sent)}"}}
        return self.receipts[key]

    def row(self, number=11, kind="text"):
        return {"talker": "synthetic-room", "local_id": number,
                "create_time": 1000 + number, "sender_wxid": "synthetic-sender",
                "sender": "Synthetic nickname", "kind_name": kind,
                "text": f"Synthetic message {number}"}

    def person(self):
        return self.manager.post("people", {"op": "create", "name": "Synthetic Person"})

    def link(self, person):
        return self.manager.post("people", {"op": "link_wechat", "person_id": person["id"],
                                           "source_account": "qa", "wechat_sender_id": "synthetic-sender"})

    def texts(self):
        return [args[args.index("--text") + 1] for args in self.sent if "--text" in args]


@contextlib.contextmanager
def _sandbox():
    from agents import manage, wechat_feishu_mirror as mirror
    from agents.storage import MirrorStore

    with tempfile.TemporaryDirectory(prefix="w2f-functional-") as directory, contextlib.ExitStack() as stack:
        root = Path(directory)
        unexpected_external = []

        def deny_external(*args, **kwargs):
            unexpected_external.append(True)
            return _deny_external(*args, **kwargs)

        # Patch the transport entry points before creating product objects.
        for target in ("subprocess.run", "subprocess.Popen", "urllib.request.urlopen", "socket.create_connection"):
            stack.enter_context(patch(target, side_effect=deny_external))
        stack.enter_context(patch.object(manage, "run_cli", side_effect=deny_external))
        stack.enter_context(patch.object(mirror, "run_lark", side_effect=deny_external))
        stack.enter_context(patch.object(mirror, "wechat_cache_paths", return_value=[]))
        stack.enter_context(patch.dict(os.environ, {"W2F_MEDIA_DIR": str(root / "media"),
                                                   "CHZ_MEDIA_DIR": str(root / "media")}))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        config = {"binding_id": 1, "source_account": "qa", "wechat_talker": "synthetic-room",
                  "wechat_chat_name": "Synthetic source", "feishu_chat_id": "synthetic-chat",
                  "feishu_chat_name": "Synthetic destination", "lark_profile": "qa",
                  "lark_app_id": "synthetic-app", "initial_cursor": 10, "sender_map": {},
                  "sqlite_file": str(root / "state.db"), "data_dir": str(root),
                  "media_dir": str(root / "media")}
        store = MirrorStore(root / "state.db")
        store.initialize(config)
        store.set_control(1, "running")
        try:
            yield Sandbox(root, store, manage.Manager(store, root / "config.json"), mirror, config)
        finally:
            # Product fallback handlers may catch transport exceptions. Such a
            # swallowed harness denial still invalidates a product conclusion.
            if unexpected_external:
                raise ProbeBlocked("An unexpected external operation invalidated the scenario")


def _people_lifecycle(s):
    person = s.person()
    identity = s.link(person)
    profile = next(p for p in s.store.list_profiles() if p["profile_name"] == "qa")
    feishu = s.manager.post("people", {"op": "link_feishu", "person_id": person["id"],
                                      "profile_id": profile["id"], "open_id": "ou_synthetic"})
    empty = s.person()
    rejected = []
    requests = [
        {"op": "unlink_wechat", "id": identity["id"], "person_id": person["id"], "confirm": True},
        {"op": "unlink_feishu", "id": feishu["id"], "person_id": person["id"], "confirm": True},
        {"op": "delete", "id": empty["id"], "confirm": True},
        {"op": "archive", "id": person["id"], "confirm": True},
    ]
    for request in requests:
        try:
            s.manager.post("people", request)
        except ValueError:
            rejected.append(request["op"])
    remaining = s.manager.get("people")
    linked = any(item["person_id"] == person["id"] for item in remaining["wechat"] + remaining["feishu"])
    empty_exists = any(p["id"] == empty["id"] for p in remaining["people"])
    archived = any(p["id"] == person["id"] and p["status"] == "archived" for p in s.store.list_people())
    evidence = ["Create a synthetic person, link both identities, and create an empty person via Manager.",
                "Call proposed #15 people operations with confirm=true: " + ", ".join(r["op"] for r in requests) + ".",
                f"Rejected operations: {', '.join(rejected) or 'none'}.",
                f"Linked identities remain={linked}; empty person remains={empty_exists}; archived={archived}.",
                "These operation names are proposed acceptance contracts, not existing documented API endpoints."]
    require(not rejected and not linked and not empty_exists and archived,
            "The proposed identity lifecycle is unavailable or did not update persisted state.", evidence)
    return "Identity unlink, empty-person deletion, and archive completed.", evidence


def _archived_attribution(s):
    person = s.person()
    s.link(person)
    # No archive endpoint exists yet. Seed the schema-supported state to verify
    # downstream consumers; this is explicitly not a live user reproduction.
    with s.store.connect() as db:
        db.execute("UPDATE people SET status='archived' WHERE id=?", (person["id"],))
    with patch.object(s.mirror, "run_lark", side_effect=s.sink):
        s.mirror.send_row(s.config, s.row(), {}, s.store)
    used_old_name = s.texts()[0].startswith("Synthetic Person：")
    evidence = ["Create/link a synthetic person, then seed people.status='archived' in the temporary DB.",
                "Send a new synthetic text through production send_row with a fake Feishu transport.",
                f"New message uses archived confirmed name={used_old_name}.",
                "State-seeding coverage for #15; no claim that an existing UI can archive a person."]
    require(not used_old_name, "Archived person still supplies the confirmed name for new messages.", evidence)
    return "Archived person's confirmed name is no longer used.", evidence


def _reconfirmation(s):
    s.store.record_sender("qa", "synthetic-sender", "Synthetic nickname", binding_id=1)
    confirmation = s.store.list_confirmations()[0]
    s.store.finish_confirmation_notification(confirmation["id"])
    s.manager.post("confirmations", {"op": "confirm", "id": confirmation["id"], "name": "Synthetic Person"})
    s.store.record_sender("qa", "synthetic-sender", "Changed synthetic nickname", binding_id=1)
    identity = s.store.resolve_sender("qa", "synthetic-sender")
    pending = s.store.list_confirmations()
    claim = s.store.claim_confirmation_notification("qa", "synthetic-sender")
    coherent = identity["status"] != "pending" or (identity["person_id"] is None and pending and claim)
    evidence = ["record_sender -> finish_confirmation_notification -> Manager confirm -> record_sender without a confirmed name.",
                f"Identity status={identity['status']}; person retained={identity['person_id'] is not None}.",
                f"Pending confirmations={len(pending)}; reminder can be claimed={claim is not None}.",
                "Store API state-transition probe; an end-user re-pending operation is not currently exposed."]
    require(bool(coherent), "Re-recording a confirmed sender leaves a pending identity with a stale person and no pending confirmation.", evidence)
    return "A re-recorded sender remains in a coherent confirmed or reviewable pending state.", evidence


def _confirmation_replay(s):
    s.store.record_sender("qa", "synthetic-sender", "Synthetic nickname", binding_id=1)
    item = s.store.list_confirmations()[0]
    request = {"op": "confirm", "id": item["id"], "name": "Synthetic Person"}
    s.manager.post("confirmations", request)
    before = s.store.resolve_sender("qa", "synthetic-sender")["person_id"]
    rejected = False
    try:
        s.manager.post("confirmations", request)
    except ValueError:
        rejected = True
    after = s.store.resolve_sender("qa", "synthetic-sender")["person_id"]
    count = len(s.store.list_people())
    evidence = ["Create one unknown sender and confirm its pending item using Manager.post.",
                "Submit the identical confirmation request a second time (double click or stale client retry).",
                f"People after replay={count}; linked person changed={before != after}; replay rejected={rejected}."]
    require(count == 1 and before == after, "Replaying a completed confirmation creates an extra person and changes the identity association.", evidence)
    return "Replayed confirmation is rejected or idempotent without creating another person.", evidence


def _rename_history(s):
    person = s.person()
    s.link(person)
    with patch.object(s.mirror, "run_lark", side_effect=s.sink):
        s.mirror.send_row(s.config, s.row(), {}, s.store)
        s.manager.post("people", {"op": "rename", "id": person["id"], "name": "Renamed Synthetic Person"})
        s.mirror.send_row(s.config, s.row(12), {}, s.store)
        s.mirror.send_row(s.config, s.row(), {}, s.store)
    old = s.store.message("synthetic-room:11", 1)
    new = s.store.message("synthetic-room:12", 1)
    evidence = ["Send text, rename through Manager, send the next text, and replay the old text.",
                f"Remote sends={len(s.sent)}; historical name preserved={old['sender_name'] == 'Synthetic Person'}; new name applied={new['sender_name'] == 'Renamed Synthetic Person'}."]
    require(len(s.sent) == 2 and old["sender_name"] == "Synthetic Person" and new["sender_name"] == "Renamed Synthetic Person",
            "Rename or replay changed historical attribution or duplicated a send.", evidence)
    return "Rename affects subsequent messages and replay does not send a duplicate.", evidence


class _History:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self, config, cursor, high=None, after=None):
        high = max((row["local_id"] for row in self.rows), default=cursor) if high is None else high
        rows = [row for row in self.rows if cursor < row["local_id"] <= high]
        if after:
            rows = [row for row in rows if (row["create_time"], row["local_id"]) > (after["time"], after["local_id"])]
        page = rows[:2]
        return {"contract": "ordered_sync_v1", "messages": page, "through_local_id": high,
                "has_more": len(rows) > len(page),
                "next": {"time": page[-1]["create_time"], "local_id": page[-1]["local_id"]} if page else None}


def _pause_resume(s):
    s.link(s.person())
    s.config["_guard"] = True
    source = _History([s.row(n) for n in range(11, 16)])

    def sink(config, args):
        result = s.sink(config, args)
        if len(s.sent) == 2:
            s.store.set_control(1, "paused")
        return result

    paused = False
    with patch.object(s.mirror, "run_lark", side_effect=sink):
        try:
            s.mirror.recover_once(s.config, s.store, source)
        except s.mirror.ControlChanged:
            paused = True
    before = len(s.sent)
    s.store.set_control(1, "running")
    with patch.object(s.mirror, "run_lark", side_effect=s.sink):
        s.mirror.recover_once(s.config, s.store, source)
        s.mirror.recover_once(s.config, s.store, source)
    texts = [f"Synthetic Person：Synthetic message {n}" for n in range(11, 16)]
    evidence = ["Recover a two-page synthetic backlog, pause after two receipts, resume, then poll again.",
                f"Pause observed={paused}; sends at pause={before}; total sends={len(s.sent)}; final cursor={s.store.get_cursor()}."]
    require(paused and before == 2 and s.texts() == texts and s.store.get_cursor() == 15,
            "Pause/resume lost ordering, progress, or delivery idempotency.", evidence)
    return "Pause/resume preserves order, receipts, and cursor without duplicate sends.", evidence


def _lost_receipt(s):
    s.link(s.person())
    source = _History([s.row(11), s.row(12)])
    fail_once = True

    def sink(config, args):
        nonlocal fail_once
        result = s.sink(config, args)
        if fail_once:
            fail_once = False
            raise s.mirror.CliError("synthetic accepted response lost", uncertain=True)
        return result

    interrupted = False
    with patch.object(s.mirror, "run_lark", side_effect=sink):
        try:
            s.mirror.recover_once(s.config, s.store, source)
        except s.mirror.CliError:
            interrupted = True
        cursor_before = s.store.get_cursor()
        s.mirror.recover_once(s.config, s.store, source)
    evidence = ["Fake destination accepts the first message but drops its response, then accepts an idempotent retry.",
                f"Failure observed={interrupted}; cursor after failure={cursor_before}; unique accepted sends={len(s.sent)}; final cursor={s.store.get_cursor()}."]
    require(interrupted and cursor_before == 10 and len(s.sent) == 2 and s.store.get_cursor() == 12,
            "Lost delivery response caused duplicate acceptance or cursor advancement past failure.", evidence)
    return "Lost response retries the same delivery identity and preserves cursor safety.", evidence


def _synthetic_media(s):
    s.link(s.person())
    # Valid one-pixel PNG. The audio bytes exercise staging/dispatch, not decoding.
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==")
    voice = s.root / "synthetic.ogg"
    voice.write_bytes(b"OggS-synthetic-dispatch-fixture")

    class Response(io.BytesIO):
        headers = {"Content-Type": "image/png"}

    uploads = []

    def media_sink(config, args):
        for flag in ("--image", "--audio"):
            if flag in args:
                path = Path(args[args.index(flag) + 1])
                require(path.is_file() and path.stat().st_size > 0,
                        "A media upload received a missing or empty staged file.",
                        ["Synthetic transport inspected the staged file before returning a receipt."])
                uploads.append(path)
        return s.sink(config, args)

    sticker = {**s.row(11, "sticker"), "text": '<msg><emoji cdnurl="https://example.test/synthetic.png" /></msg>'}
    with patch.object(s.mirror, "run_lark", side_effect=media_sink), \
            patch("urllib.request.urlopen", side_effect=lambda *args, **kwargs: Response(png)), \
            patch.object(s.mirror, "reader_media_result", return_value=([voice], False)):
        s.mirror.send_row(s.config, sticker, {}, s.store)
        s.mirror.send_row(s.config, s.row(12, "voice"), {}, s.store)
        s.mirror.send_row(s.config, sticker, {}, s.store)
    flags = [flag for args in s.sent for flag in ("--image", "--audio") if flag in args]
    failures = len(s.store.list_media_failures())
    cleaned = all(not path.exists() for path in uploads)
    evidence = ["Production stage_sticker normalizes a synthetic PNG fetched from a fake HTTP response.",
                "Production stage_media copies a synthetic .ogg resource from the fake reader; Feishu sends are captured.",
                f"Media dispatch flags={','.join(flags)}; media failures={failures}; total parts={len(s.sent)}; staging cleaned={cleaned}.",
                "This verifies staging/dispatch/receipts only. It does not verify macOS resource discovery, audio decoding, or live Feishu playback (#14)."]
    require(flags == ["--image", "--audio"] and failures == 0 and len(s.sent) == 4 and cleaned,
            "Synthetic available media did not use the expected image/audio dispatch paths.", evidence)
    return "Synthetic sticker/image and voice/audio dispatch succeeded; live #14 remains unverified.", evidence


def _missing_media(s):
    s.link(s.person())
    with patch.object(s.mirror, "run_lark", side_effect=s.sink), \
            patch.object(s.mirror, "reader_media_result", return_value=([], False)):
        for row in (s.row(11, "sticker"), s.row(12, "voice"), s.row(13)):
            s.mirror.send_row(s.config, row, {}, s.store)
    evidence = ["Provide synthetic sticker/voice messages without any resource, then a normal text.",
                f"Captured parts={len(s.sent)}; queued media failures={len(s.store.list_media_failures())}; following text state={s.store.message_state('synthetic-room:13')}.",
                "Missing resources are intentionally injected; this does not reproduce the cause of #14."]
    require(len(s.sent) == 3 and len(s.store.list_media_failures()) == 2
            and s.store.message_state("synthetic-room:13") == "sent"
            and "表情资源不可用" in s.texts()[0] and "语音资源不可用" in s.texts()[1],
            "Missing media did not preserve placeholders, repair evidence, and subsequent message delivery.", evidence)
    return "Injected missing media produces repairable placeholders while following text is delivered.", evidence


PROBES = (
    Probe("people.lifecycle", "人员身份解绑、删除与归档能力", "Unlink both identities, delete an empty person, and archive a person as proposed in #15.", _people_lifecycle, (15,), "enhancement"),
    Probe("people.archived-attribution", "归档人员不应继续提供后续消息实名", "New messages must not use the confirmed name of a schema-archived person.", _archived_attribution, (15,)),
    Probe("identity.reconfirmation", "身份再次登记时待确认状态应一致", "A pending identity has no confirmed person and has a reviewable confirmation and claimable reminder.", _reconfirmation, (15,), "enhancement"),
    Probe("identity.confirmation-replay", "重复确认请求不应重复创建人员", "A duplicate confirmation request is idempotent or rejected without changing the linked person.", _confirmation_replay),
    Probe("identity.rename-history", "实名更正与历史投递幂等", "Renaming affects new messages, preserves history, and does not resend completed messages.", _rename_history),
    Probe("worker.pause-resume", "积压恢复期间暂停与恢复", "Backfill pause/resume preserves order, receipts, and cursor without duplicate delivery.", _pause_resume),
    Probe("worker.lost-receipt", "发送成功但回执丢失后的恢复", "An uncertain delivery retries with the same identity and cannot advance the cursor prematurely.", _lost_receipt),
    Probe("media.synthetic-dispatch", "合成表情和语音资源投递链路", "Available synthetic resources use image/audio dispatch and persist receipts without a placeholder.", _synthetic_media, (14,)),
    Probe("media.missing-resource", "媒体缺失占位与后续消息投递", "Missing media creates tracked placeholders and does not prevent following text delivery.", _missing_media, (14,)),
)


def run_checks() -> list[dict]:
    """Run each scenario in a fresh isolated DB; never return raw exception text."""
    results = []
    for probe in PROBES:
        result = {"id": probe.id, "title": probe.title, "status": "blocked",
                  "expected": probe.expected, "actual": "", "evidence": [],
                  "related_issues": list(probe.related_issues), "category": probe.category}
        try:
            with _sandbox() as sandbox:
                result["actual"], result["evidence"] = probe.run(sandbox)
            result["status"] = "passed"
        except CheckFailed as exc:
            result.update(status="failed", actual=exc.actual, evidence=exc.evidence)
        except Exception as exc:
            # Missing dependencies, new unexpected code paths, harness errors,
            # and environment failures need investigation, not auto-filed bugs.
            result.update(status="blocked", actual=f"Probe could not complete ({type(exc).__name__}).",
                          evidence=["No product conclusion was made. Check local dependencies and harness compatibility; raw exception text is withheld."])
        results.append(result)
    return results
