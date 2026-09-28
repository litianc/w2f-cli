"""Exact macOS attachment lookup when the hardlink index lacks the XML digest."""
import hashlib
import re
from pathlib import Path
try:
    from .reader_sync import connection
except ImportError:
    from reader_sync import connection


def fields(data):
    """Bounded protobuf wire reader; only length-delimited values are returned."""
    if len(data) > 65536:
        raise ValueError('oversized metadata')
    pos = 0
    def integer():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError('truncated metadata')
            byte = data[pos]; pos += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError('invalid varint')
    while pos < len(data):
        tag = integer(); number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError('invalid field')
        if wire == 0:
            integer(); continue
        if wire == 2:
            size = integer()
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
        else:
            raise ValueError('unsupported wire type')
        end = pos + size
        if end > len(data):
            raise ValueError('truncated field')
        value = data[pos:end]; pos = end
        if wire == 2:
            yield number, value


def read_images(reader, db, args):
    talker = reader.resolve_username(db, args.talker)
    table = reader.message_table(talker)
    digests = set()
    found = False
    for source in db.message_dbs:
        with connection(db, source) as conn:
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if 'packed_info_data' not in columns:
                continue
            rows = conn.execute(f'SELECT server_id,create_time,local_type,packed_info_data FROM "{table}" WHERE local_id=?', (args.local_id,)).fetchall()
            for row in rows:
                if (int(row['create_time']) != args.create_time or int(row['local_type']) != 3
                    or (args.server_id and int(row['server_id'] or 0) != args.server_id)):
                    raise reader.ReaderError('图片消息身份不匹配', 'image_identity_mismatch')
                found = True
                try:
                    packed = bytes(row['packed_info_data'] or b'')
                    for number, nested in fields(packed):
                        if number == 3:
                            for field, value in fields(nested):
                                if field == 4 and re.fullmatch(rb'[a-fA-F0-9]{32}', value):
                                    digests.add(value.decode().lower())
                except (ValueError, TypeError):
                    raise reader.ReaderError('图片附件字段不可解析', 'image_metadata_invalid') from None
    if len(digests) > 1:
        raise reader.ReaderError('图片附件身份不唯一', 'image_identity_ambiguous')
    if not found:
        raise reader.ReaderError('图片源消息不存在', 'image_source_missing')
    resources = []
    if digests:
        digest = next(iter(digests))
        group = hashlib.md5(talker.encode()).hexdigest()
        seen = set()
        for root in db.resource_roots:
            base = Path(root) / 'attach' / group
            if not base.is_dir() or base.is_symlink():
                continue
            for month in sorted(base.iterdir()):
                if not re.fullmatch(r'\d{4}-\d{2}', month.name):
                    continue
                for suffix, preview in (('.dat', False), ('_h.dat', False), ('_t.dat', True)):
                    path = month / 'Img' / (digest + suffix)
                    resolved = path.resolve()
                    if not resolved.is_relative_to(base.resolve()) or not resolved.is_file() or resolved in seen:
                        continue
                    seen.add(resolved)
                    resources.append({'local_paths': [str(resolved)], 'thumbnail_only': preview})
    return {'contract': 'image_resource_v1', 'talker': talker, 'local_id': args.local_id,
            'server_id': args.server_id, 'create_time': args.create_time, 'resources': resources}
