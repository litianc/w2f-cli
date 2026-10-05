"""Explicit-state CLI for the first local collection slice."""
import argparse
import json
import sqlite3
import sys

from .collector import CollectionError, Collector


def main():
    for pipe in (sys.stdout, sys.stderr):
        if hasattr(pipe, 'reconfigure'):
            pipe.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='v0.4.0 本地采集内核（尚未实现云端上传）')
    parser.add_argument('--state-dir', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--reader', required=True)
    init.add_argument('--account', required=True)
    init.add_argument('--page-size', type=int, default=100)
    init.add_argument('--max-pending-bytes', type=int, default=64*1024*1024)
    enable = sub.add_parser('enable')
    enable.add_argument('--talker', required=True)
    sub.add_parser('collect', help='执行一轮有界快照采集，不发送飞书或上传云端')
    sub.add_parser('status')
    pending = sub.add_parser('pending')
    pending.add_argument('--after', type=int, default=0)
    pending.add_argument('--limit', type=int, default=100)
    a = parser.parse_args()
    try:
        collector = Collector(a.state_dir)
        if a.command == 'init':
            result = collector.initialize(a.reader, a.account, a.page_size, a.max_pending_bytes)
        elif a.command == 'enable':
            result = collector.enable(a.talker)
        elif a.command == 'pending':
            result = collector.pending(a.after, a.limit)
        else:
            result = getattr(collector, a.command)()
        print(json.dumps(result, ensure_ascii=False))
        return int(a.command == 'collect' and any(row['error_code'] for row in result['streams']))
    except (CollectionError, OSError, sqlite3.Error) as exc:
        code = exc.code if isinstance(exc, CollectionError) else 'local_state_error'
        # Reader stdout/stderr may contain private content; only emit our own errors.
        message = str(exc) if isinstance(exc, CollectionError) else '本地状态无法初始化或读写；请检查独立目录、权限和磁盘空间'
        print(json.dumps(dict(ok=False, error=dict(code=code, message=message)), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
