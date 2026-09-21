"""Read-only rion reader extension: complete, chronological, bounded pages.

Installed next to rion_wechat_reader.py by scripts/install_reader_sync.py.
No keys, credentials or machine-specific configuration are shipped here.
"""
import sys
from contextlib import closing


def register(sub):
    p = sub.add_parser('sync-page', help='Read a complete ordered synchronization page')
    p.add_argument('--talker', required=True)
    p.add_argument('--after-local-id', type=int, default=0)
    p.add_argument('--through-local-id', type=int)
    p.add_argument('--page-after-time', type=int)
    p.add_argument('--page-after-local-id', type=int, default=0)
    p.add_argument('--limit', type=int, default=100)


def read_page(reader, db, args):
    username = reader.resolve_username(db, args.talker)
    table = reader.message_table(username)
    contacts = db.contact_index()
    sources, latest, anchor_exists = [], 0, args.after_local_id == 0
    for source in db.message_dbs:
        with closing(db.connect(source)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            sources.append(source)
            latest = max(latest, int(conn.execute(f'SELECT MAX(local_id) FROM "{table}"').fetchone()[0] or 0))
            anchor_exists |= bool(conn.execute(f'SELECT 1 FROM "{table}" WHERE local_id=?', (args.after_local_id,)).fetchone())
    if not sources:
        raise reader.ReaderError('源群消息表不存在，不能把不可读当作没有新消息', 'source_missing')
    high = latest if args.through_local_id is None else args.through_local_id
    if latest < args.after_local_id or high > latest or not anchor_exists:
        raise reader.ReaderError('源游标锚点缺失或历史被重置；保留游标，需检查本机历史', 'source_gap')
    limit = max(1, min(500, args.limit))
    result = {}
    for source in sources:
        with closing(db.connect(source)) as conn:
            names = {int(r['rowid']): str(r['user_name']) for r in conn.execute('SELECT rowid,user_name FROM Name2Id')}
            clauses, params = ['local_id > ?', 'local_id <= ?'], [args.after_local_id, high]
            if args.page_after_time is not None:
                clauses.append('(create_time > ? OR (create_time = ? AND local_id > ?))')
                params.extend([args.page_after_time, args.page_after_time, args.page_after_local_id])
            sql = f'SELECT {reader.message_projection(conn, table)} FROM "{table}" WHERE ' + ' AND '.join(clauses) + ' ORDER BY create_time ASC,local_id ASC LIMIT ?'
            for r in conn.execute(sql, (*params, limit + 1)):
                item = dict(r)
                sender = names.get(int(item.get('real_sender_id') or 0), '')
                content = reader.visible_message_content(reader.decode_message_content(item), username, sender)
                local_id = int(item['local_id'])
                if local_id in result and str(result[local_id].get('server_id')) != str(item.get('server_id')):
                    raise reader.ReaderError('不同分片出现重复本地 ID；需要重新核对源身份', 'source_collision')
                item.update(talker=username, chat=contacts.get(username, {}).get('display_name') or username,
                            sender=contacts.get(sender, {}).get('display_name') or sender, sender_wxid=sender,
                            from_me=bool(db.self_username and sender == db.self_username), text=str(content), content=str(content),
                            time=reader.time_iso(item.get('create_time')), kind_name=reader.message_kind(int(item.get('local_type') or 0), str(content)))
                if item["kind_name"] == "forward_chat":
                    from forward_records import parse_record
                    item["forward_items"] = parse_record(str(content))
                result[local_id] = item
    ordered = sorted(result.values(), key=lambda r: (int(r.get('create_time') or 0), int(r['local_id'])))
    rows = ordered[:limit]
    return {'contract': 'ordered_sync_v1', 'messages': rows, 'through_local_id': high,
            'has_more': len(ordered) > limit,
            'next': {'time': int(rows[-1].get('create_time') or 0), 'local_id': int(rows[-1]['local_id'])} if rows else None}
