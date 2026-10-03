"""Locate a file using an exact source message and decoded attachment metadata."""
import json
from pathlib import Path
try:
    from .reader_sync import connection
    from .file_messages import file_share, matches_file
except ImportError:
    from reader_sync import connection
    from file_messages import file_share, matches_file


def read_file(reader, db, args):
    talker = reader.resolve_username(db, args.talker)
    table = reader.message_table(talker)
    metadata = None
    source_server_id = args.server_id
    completion_verified = False
    for source in db.message_dbs:
        with connection(db, source) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            rows = conn.execute(f'SELECT {reader.message_projection(conn, table)} FROM "{table}" WHERE local_id=?', (args.local_id,)).fetchall()
            for value in rows:
                row = dict(value)
                if (int(row.get('create_time') or 0) != args.create_time
                        or int(row.get('local_type') or 0) & 0xffffffff != 49):
                    raise reader.ReaderError('文件源消息身份不匹配', 'file_identity_mismatch')
                sender = conn.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (row.get('real_sender_id'),)).fetchone()
                sender_id = str(sender[0]) if sender else ''
                content = reader.visible_message_content(reader.decode_message_content(row), talker, sender_id)
                current = file_share({'content': content})
                if current is None or (metadata is not None and metadata != current):
                    raise reader.ReaderError('文件源消息内容不明确', 'file_metadata_invalid')
                actual_server_id = int(row.get('server_id') or 0)
                if actual_server_id != args.server_id:
                    # Type 74 is rewritten in place as type 6 after upload.
                    # A changed server ID alone is never enough to accept it.
                    try:
                        pending = json.loads(getattr(args, 'pending_file_metadata', None) or 'null')
                    except ValueError:
                        pending = None
                    matches = (isinstance(pending, dict) and bool(current['md5'])
                               and all(pending.get(k) == v for k, v in current.items())
                               and file_share({'content': content}, with_type=True)['app_type'] == 6
                               and int(row.get('local_type') or 0) >> 32 == 6
                               and bool(pending.get('sender_wxid')) and actual_server_id != 0)
                    if not matches or not sender_id or sender_id != pending['sender_wxid']:
                        raise reader.ReaderError('文件源消息身份不匹配', 'file_identity_mismatch')
                    completion_verified = True
                if metadata is not None and actual_server_id != source_server_id:
                    raise reader.ReaderError('文件源消息身份不唯一', 'file_identity_mismatch')
                source_server_id = actual_server_id
                metadata = current
    if metadata is None:
        raise reader.ReaderError('文件源消息不存在', 'file_source_missing')
    resources = []
    # Digest lookup is stronger than a shared filename. Never use the index's
    # filename OR branch when we have a digest for this exact message.
    if metadata['md5']:
        candidates = reader.hardlink_resources(db, {'md5': metadata['md5']}, 'file', None, True)
        roots = [Path(p).resolve() for p in db.resource_roots]
        seen = set()
        for resource in candidates:
            for value in resource.get('local_paths', []):
                path = Path(value)
                if (path in seen or not any(path.resolve().is_relative_to(root) for root in roots)
                        or not matches_file(path, metadata)):
                    continue
                seen.add(path)
                resources.append({'local_paths': [str(path)], 'file_name': metadata['name']})
    return {'contract': 'file_resource_v1', 'talker': talker, 'local_id': args.local_id,
            'server_id': args.server_id, 'create_time': args.create_time,
            'source_server_id': source_server_id, 'completion_verified': completion_verified,
            'file': metadata, 'resources': resources}
