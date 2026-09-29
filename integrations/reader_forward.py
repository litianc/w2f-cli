"""Resolve one merged-forward media item from an exact source row, read-only."""
import hashlib
import re
from pathlib import Path
try:
    from .reader_sync import connection
    from .forward_records import parse_record, walk_records
except ImportError:
    from reader_sync import connection
    from forward_records import parse_record, walk_records


MAX_VIDEO_BYTES = 100 * 1024 * 1024


def image_candidates(reader, db, talker, media):
    """Encrypted cache files are candidates, verified after decoding by worker."""
    roots = [Path(root).resolve() for root in db.resource_roots]
    group = hashlib.md5(talker.encode()).hexdigest()
    resources, seen = [], set()
    for descriptor, preview in ((media,False),(media.get('thumbnail') or {},True)):
        digest, size = descriptor.get('md5',''), int(descriptor.get('size') or 0)
        if not re.fullmatch(r'[a-f0-9]{32}',digest) or not 0 < size <= MAX_VIDEO_BYTES:
            continue
        details = reader.hardlink_resources(db, {'md5':digest}, 'image', None, True)
        candidates = [Path(p) for detail in details for p in detail.get('local_paths', [])]
        for root in roots:
            base = root/'attach'/group
            if base.is_symlink():
                continue
            candidates.extend(p for p in base.glob('????-??/Rec/*/Img/*')
                              if p.resolve().is_relative_to(base.resolve()))
        for path in candidates:
            try:
                resolved = path.resolve()
                if (path.is_symlink() or not path.is_file() or (resolved,preview) in seen
                        or not any(resolved.is_relative_to(root) for root in roots)):
                    continue
                length = path.stat().st_size
                if not size <= length <= size + 31:
                    continue
                # V1/V2 envelopes add 15 header bytes and 1..16 padding bytes.
                # XOR/plain files retain their original length.
                if length != size:
                    with path.open('rb') as handle:header=handle.read(15)
                    if header[:6] not in (b'\x07\x08V1\x08\x07',b'\x07\x08V2\x08\x07'):
                        continue
                    head = int.from_bytes(header[6:10],'little')
                    if length != size + 15 + (16-head%16):
                        continue
                resources.append({'local_paths':[str(resolved)],'thumbnail_only':preview})
                seen.add((resolved,preview))
            except OSError:
                continue
    return resources


def matches_video(path, media):
    size = int(media.get('size') or 0)
    digest = str(media.get('md5') or '')
    if not 0 < size <= MAX_VIDEO_BYTES or not re.fullmatch(r'[a-f0-9]{32}', digest):
        return False
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            return False
        with path.open('rb') as handle:
            computed = hashlib.md5()
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                computed.update(chunk)
            return computed.hexdigest() == digest
    except OSError:
        return False


def read_forward(reader, db, args):
    talker = reader.resolve_username(db, args.talker)
    table = reader.message_table(talker)
    selected = None
    for source in db.message_dbs:
        with connection(db, source) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            rows = conn.execute(f'SELECT {reader.message_projection(conn, table)} FROM "{table}" WHERE local_id=?', (args.local_id,)).fetchall()
            for row in rows:
                if int(row['server_id'] or 0) != args.server_id or int(row['create_time']) != args.create_time:
                    raise reader.ReaderError('转发源消息身份不匹配', 'forward_identity_mismatch')
                raw = reader.visible_message_content(reader.decode_message_content(dict(row)), talker, '')
                item = dict(walk_records(parse_record(raw))).get(args.item_path)
                if not item or item['kind'] not in {'video','image'}:
                    raise reader.ReaderError('转发媒体子项不存在', 'forward_item_missing')
                if selected is not None and selected != item:
                    raise reader.ReaderError('转发源消息身份不唯一', 'forward_identity_ambiguous')
                selected = item
    if selected is None:
        raise reader.ReaderError('转发源消息不存在', 'forward_source_missing')
    media = selected.get('media') or {}
    paths = []
    if selected['kind']=='video' and media.get('md5') and 0 < media.get('size', 0) <= MAX_VIDEO_BYTES:
        details = reader.hardlink_resources(db, {'md5':media['md5']}, 'video', None, True)
        candidates = [Path(p) for detail in details for p in detail.get('local_paths', [])]
        roots = [Path(root).resolve() for root in db.resource_roots]
        def accept(path):
            return (any(path.resolve().is_relative_to(root) for root in roots)
                    and matches_video(path, media))
        paths = [str(p.resolve()) for p in candidates if accept(p)]
        if not paths:
            # macOS stores attachments opened within a merged forward in Rec.
            # Search only this conversation's cache and verify full file bytes;
            # a bare child filename (e.g. 1.mp4) is never an identity.
            group = hashlib.md5(talker.encode()).hexdigest()
            for root in roots:
                base = root / 'attach' / group
                for path in base.glob('????-??/Rec/*/V/*.mp4'):
                    if path.resolve().is_relative_to(base.resolve()) and accept(path):
                        paths.append(str(path.resolve()))
                        break
                if paths:
                    break
    resources = image_candidates(reader,db,talker,media) if selected['kind']=='image' else [{'local_paths':[p]} for p in dict.fromkeys(paths)]
    return {'contract':'forward_resource_v1', 'talker':talker, 'local_id':args.local_id,
            'server_id':args.server_id, 'create_time':args.create_time, 'item_path':args.item_path,
            'media':media, 'resources':resources}
