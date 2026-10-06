"""Restricted operator CLI; run inside the cloud container with its private secrets."""
import argparse
import json
import os
from pathlib import Path
import sys

import psycopg
from cloud.app import Settings
from cloud.store import Store, StoreError
from w2f_protocol.ingest import ProtocolError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('migrate')
    sub.add_parser('new-generation', help='After restoring DB, before reopening ingress: fence old receipts and pause delivery')
    provision = sub.add_parser('provision', help='Create user/device/bindings atomically; one active device per account')
    provision.add_argument('--spec-file', required=True, help='JSON path or - for stdin')
    provision.add_argument('--credentials-file', required=True, help='New absolute private output path')
    for name in ('show', 'disable'):
        p = sub.add_parser(name)
        p.add_argument('--kind', choices=['user','device','binding'] + (['message'] if name == 'show' else []), required=True)
        p.add_argument('--id', required=True)
    args = parser.parse_args()
    try:
        store = Store(Settings.from_env())
        if args.command == 'migrate':
            store.migrate()
            result = dict(schema_version=1)
        elif args.command == 'new-generation':
            result = store.new_generation()
        elif args.command == 'provision':
            spec = json.loads(sys.stdin.read() if args.spec_file == '-' else Path(args.spec_file).read_text(encoding='utf-8'))
            path = Path(args.credentials_file)
            if not path.is_absolute() or path.parent.is_symlink():
                raise ProtocolError('Use an absolute private output path')
            # Reserve before mutating the DB. Failure leaves an empty private file for explicit operator recovery.
            with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as output:
                credentials = store.provision(spec)
                output.write(json.dumps(credentials, ensure_ascii=False) + '\n')
                output.flush()
                os.fsync(output.fileno())
            result = dict(created=True, device_id=spec['device_id'])
        else:
            result = getattr(store, args.command)(args.kind, args.id)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except (StoreError, ProtocolError, psycopg.Error, OSError, ValueError):
        print(json.dumps(dict(ok=False, error='operation_failed; check IDs, schema and private configuration')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
