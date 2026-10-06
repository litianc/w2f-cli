"""Independent delivery process; idle by default until an operator enables bindings."""
import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time

import psycopg
from cloud.app import Settings
from cloud.delivery import Delivery
from cloud.feishu import Feishu
from cloud.store import Store, StoreError

HEARTBEAT=Path('/tmp/w2f-sender-health.json')


def settings():
    return Settings.from_env(role='sender')


def transport():
    return Feishu(os.environ['W2F_CLOUD_ENVIRONMENT'],os.environ['W2F_FEISHU_CREDENTIALS_FILE'],
                  os.environ.get('W2F_FEISHU_BASE_URL','https://open.feishu.cn'))


def pulse(status='ready'):
    temporary=HEARTBEAT.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(at=time.time(),status=status,release=os.environ.get('W2F_RELEASE','local'))))
    temporary.replace(HEARTBEAT)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--health',action='store_true')
    parser.add_argument('--limit',type=int,default=20)
    args=parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('limit must be 1-100')
    store=Store(settings())
    if args.health:
        try:
            state=json.loads(HEARTBEAT.read_text())
            assert state['status']=='ready' and 0 <= time.time()-state['at'] < 45
            assert state['release']==os.environ.get('W2F_RELEASE','local')
            with store.connection() as db:
                assert db.execute('SELECT version FROM w2f_delivery_schema').fetchone()['version']==2
            return 0
        except (OSError,ValueError,KeyError,AssertionError,psycopg.Error):
            return 1
    store.migrate()
    sender=Delivery(store,transport())
    if args.once:
        print(json.dumps(sender.run_once(args.limit),ensure_ascii=False))
        return 0
    stopping=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:stopping.set())
    pulse()
    while not stopping.is_set():
        try:
            sender.run_once(args.limit,heartbeat=pulse,stopping=stopping.is_set)
            pulse()
        except (psycopg.Error,StoreError):
            pulse('storage_unavailable')
        stopping.wait(2)
    pulse('stopped')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
