"""SSE wakeup loop must skip the poll wait on data events, survive drops."""
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agents.wechat_feishu_mirror import realtime_wakeup_loop, reader_wakeup_loop


class FakeSseHandler(BaseHTTPRequestHandler):
    server_version = 'FakeSSE/1'

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        try:
            self.wfile.write(b': ping\n\n')
            self.wfile.flush()
            self.server.served_ping.set()
            self.server.served_data.wait(5)
            if self.server.served_data.is_set():
                self.wfile.write(b'data: {"type":"change"}\n\n')
                self.wfile.flush()
            self.server.hold.wait(5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class RealtimeWakeupTest(unittest.TestCase):
    def test_data_event_sets_wakeup_and_stop_exits(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeSseHandler)
        server.served_ping = threading.Event()
        server.served_data = threading.Event()
        server.hold = threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addEndpoint = f'http://127.0.0.1:{server.server_port}'
        try:
            wakeup = threading.Event()
            stopped = threading.Event()
            config = {'realtime_stream_url': self.addEndpoint}
            worker = threading.Thread(target=realtime_wakeup_loop, args=(config, wakeup, stopped), daemon=True)
            worker.start()
            self.assertTrue(server.served_ping.wait(10), 'SSE client never connected')
            # The initial connection counts as one immediate scan.
            self.assertTrue(wakeup.wait(5), 'connection did not trigger the first wakeup')
            wakeup.clear()
            time.sleep(0.3)
            self.assertFalse(wakeup.is_set(), 'ping comment must not wake the worker')
            server.served_data.set()
            self.assertTrue(wakeup.wait(5), 'data event did not wake the worker')
            stopped.set()
            server.hold.set()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), 'loop did not exit after stop')
        finally:
            server.shutdown()
            server.server_close()


class ReaderWakeupTest(unittest.TestCase):
    def test_rion_watch_wakes_worker_on_new_row(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from agents import wechat_feishu_mirror as mirror
        from agents.storage import MirrorStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=MirrorStore(root/'state.db')
            store.initialize({'wechat_talker':'room','feishu_chat_id':'chat'})
            reader=root/'reader.sh'
            reader.write_text('#!/bin/sh\nprintf \'{"local_id":42}\\n\'\n', encoding='utf-8')
            reader.chmod(0o755)
            wakeup=threading.Event();stopped=threading.Event()
            with patch.object(mirror,'WECHAT_READER',reader):
                worker=threading.Thread(target=reader_wakeup_loop,args=({'sqlite_file':str(store.path),'binding_id':1,'wechat_talker':'room'},wakeup,stopped),daemon=True)
                worker.start()
                self.assertTrue(wakeup.wait(3))
                stopped.set();worker.join(3)
                self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
