"""Disposable manager HTTP server with synthetic data and fake CLI boundaries.

This deliberately uses Handler, not serve(): serve() starts real reminder work.
Run this harness in a dedicated process; its boundary patches are process-wide.
"""
from __future__ import annotations

import json
import secrets
import sys
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.manage import Handler, Manager
from agents.storage import MirrorStore


class BoundaryBlocked(RuntimeError):
    """An unexpected external operation was blocked by the test harness."""


class QAHTTPServer(ThreadingHTTPServer):
    daemon_threads = False

    def handle_error(self, request, client_address):
        # Closing a browser can cancel background refreshes during teardown.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


@contextmanager
def isolated_manager():
    """Yield a real local manager backed only by a newly created SQLite file."""
    with tempfile.TemporaryDirectory(prefix="w2f-qa-browser-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        store = MirrorStore(root / "state.sqlite3")
        store.initialize()
        profile = store.upsert_profile("qa-browser", "cli_qa_browser")
        config_path = root / "config.json"
        config_path.write_text(json.dumps({
            "data_dir": str(root), "sqlite_file": str(store.path),
            "media_dir": str(root / "media"), "log_dir": str(root / "logs"),
            "state_file": str(root / "state.json"), "wechat_reader": str(root / "fake-reader"),
        }), encoding="utf-8")
        calls, violations = [], []

        def deny(*args, **kwargs):
            violations.append("An unconfigured external boundary was blocked")
            raise BoundaryBlocked(violations[-1])

        def fake_cli(argv, **kwargs):
            # The bindings page fetches available groups in the background.
            # Every other CLI call must be added explicitly with synthetic data.
            if len(argv) > 1 and argv[1:] == ["contacts", "--groups-only", "--limit", "100000"]:
                calls.append("reader.contacts")
                return {"ok": True, "data": {"contacts": []}}
            return deny()

        stack.enter_context(patch("agents.manage.run_cli", side_effect=fake_cli))
        stack.enter_context(patch("agents.manage.run_command", side_effect=fake_cli))
        stack.enter_context(patch("agents.wechat_feishu_mirror.source_page", side_effect=deny))
        stack.enter_context(patch("agents.wechat_feishu_mirror.run_lark", side_effect=deny))
        # Prevent future product changes from accidentally bypassing the wrappers.
        import subprocess
        blocked_processes = SimpleNamespace(run=deny, Popen=deny, TimeoutExpired=subprocess.TimeoutExpired,
                                             PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
                                             STDOUT=subprocess.STDOUT)
        stack.enter_context(patch("agents.manage.subprocess", blocked_processes))
        stack.enter_context(patch("agents.wechat_feishu_mirror.subprocess", blocked_processes))

        manager = Manager(store, config_path)
        token = secrets.token_urlsafe(24)
        handler = type("QABrowserHandler", (Handler,), {"manager": manager, "token": token})
        # Wait for in-flight SQLite handlers before deleting the temporary DB.
        server = QAHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            yield SimpleNamespace(store=store, profile=profile, origin=origin,
                                  url=f"{origin}/#token={token}", root=root,
                                  external_calls=calls, violations=violations)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
