"""Validated file staging and explicit recovery of legacy title-only messages."""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from integrations.file_messages import MAX_FILE_BYTES, file_share, matches_file, safe_filename


def staging_directory(mirror, row):
    root = Path(row.get('media_dir') or mirror.MEDIA_DIR).expanduser().resolve()
    namespace = f"{row.get('binding_id', 1)}-" + hashlib.sha256(str(row['talker']).encode()).hexdigest()[:16]
    directory = root / f"{namespace}-{int(row['local_id'])}-file"
    if directory.is_symlink() or directory.resolve().parent != root:
        raise mirror.CliError('resource_invalid: 文件暂存目录异常')
    return directory


def saved_upload(mirror, row, receipt):
    if not receipt or (receipt['state'] == 'pending' and receipt['attempted_at'] is None):
        return None
    args = json.loads(receipt['payload_json'])
    path = Path(args[args.index('--file') + 1])
    root = Path(row.get('media_dir') or mirror.MEDIA_DIR).expanduser().resolve()
    metadata = file_share(row)
    if (not path.resolve().is_relative_to(root) or path.is_symlink()
            or any(p.is_symlink() for p in path.parents if p != root and p.is_relative_to(root))
            or not metadata or not metadata['md5'] or not matches_file(path, metadata)):
        raise mirror.DeliveryUncertain('原文件发送快照无法核验，请先确认飞书回执')
    return str(path)


def update_rejected_path(store, message_id, part, path):
    with store.connect() as db:
        receipt = db.execute("SELECT payload_json FROM deliveries WHERE message_id=? AND part=? AND state='pending' AND attempted_at IS NULL", (message_id, part)).fetchone()
        if receipt:
            args = json.loads(receipt['payload_json'])
            args[args.index('--file') + 1] = path
            db.execute("UPDATE deliveries SET payload_json=? WHERE message_id=? AND part=? AND state='pending' AND attempted_at IS NULL", (json.dumps(args, ensure_ascii=False), message_id, part))


def stage_initial(mirror, store, message, row):
    path = saved_upload(mirror, row, store.delivery(message['id'], 'media'))
    if not path:
        path, _ = mirror.stage_media(row, 'file')
        if path:
            update_rejected_path(store, message['id'], 'media', path)
    return path, False


def stage_file(mirror, row):
    metadata = file_share(row)
    if not metadata or not metadata['size']:
        raise mirror.CliError('resource_invalid: 文件缺少可靠的大小信息')
    if metadata['size'] > MAX_FILE_BYTES:
        raise mirror.CliError('resource_too_large: 文件超过 100 MB，等待人工处理')
    candidates, _ = mirror.reader_media_result(row)
    valid = [p for p in candidates if metadata['md5'] and matches_file(p, metadata)]
    if not valid:
        command = [str(mirror.WECHAT_READER), 'file-resource', '--talker', str(row['talker']),
                   '--local-id', str(row['local_id']), '--server-id', str(row.get('server_id') or 0),
                   '--create-time', str(row.get('create_time') or 0)]
        pending = file_share(row, with_type=True)['app_type'] == 74
        if pending and metadata['md5'] and row.get('sender_wxid'):
            command += ['--pending-file-metadata', json.dumps({**metadata, 'sender_wxid': row['sender_wxid']}, ensure_ascii=False)]
        try:
            result = mirror.subprocess.run(command, cwd=mirror.ROOT, capture_output=True,
                                           text=True, encoding='utf-8', timeout=45)
            if result.returncode:
                raise mirror.CliError('resource_missing: 文件读取失败，请检查读取器扩展及本机下载状态')
            data = json.loads(result.stdout).get('data', {})
            expected = {'contract': 'file_resource_v1', 'talker': row['talker'],
                        'local_id': int(row['local_id']), 'server_id': int(row.get('server_id') or 0),
                        'create_time': int(row.get('create_time') or 0), 'file': metadata}
            if any(data.get(k) != v for k, v in expected.items()):
                raise mirror.CliError('resource_invalid: 文件资源与源消息身份不匹配')
            if (data.get('source_server_id', expected['server_id']) != expected['server_id']
                    and not (pending and data.get('completion_verified') is True)):
                raise mirror.CliError('resource_invalid: 文件完成状态未经核验')
            valid = [Path(p) for r in data.get('resources', []) for p in r.get('local_paths', [])
                     if matches_file(Path(p), metadata)]
        except (OSError, ValueError, mirror.subprocess.TimeoutExpired):
            raise mirror.CliError('resource_missing: 文件读取失败或超时') from None
    if not valid:
        raise mirror.CliError('resource_missing: 本机未找到完整文件，请在微信中下载后重试')
    directory = staging_directory(mirror, row)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Preserve the original name for Feishu, while isolating equal names from
    # different groups/messages and retaining bytes for uncertain-send retries.
    path = directory / safe_filename(metadata['name'])
    if path.is_symlink() or path.resolve() == valid[0].resolve():
        raise mirror.CliError('resource_invalid: 文件暂存路径不能指向源文件')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as target, valid[0].open('rb') as source:
            temporary = Path(target.name)
            remaining = metadata['size'] + 1
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                target.write(chunk)
                remaining -= len(chunk)
        if not matches_file(temporary, metadata):
            raise mirror.CliError('resource_invalid: 文件下载未完成或校验失败')
        os.replace(temporary, path)
    except OSError:
        raise mirror.CliError('resource_missing: 文件暂存失败') from None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return str(path), False


def repair_parent(db, message_id, metadata):
    """Only repair a delivered title or an attachment with a proven wrong name."""
    for part in ('text-0', 'media'):
        parent = db.execute("SELECT * FROM deliveries WHERE message_id=? AND part=? AND state='sent'", (message_id, part)).fetchone()
        if not parent or not parent['feishu_message_id']:
            continue
        if part == 'media':
            try:
                args = json.loads(parent['payload_json'])
                path = args[args.index('--file') + 1]
                if not isinstance(path, str) or Path(path).name == safe_filename(metadata['name']):
                    continue
            except (ValueError, IndexError, TypeError, AttributeError):
                continue
        return parent
    raise ValueError('缺少已送达的文件标题或名称异常附件回执')


def register_historical(store, message_id):
    with store.connect() as db:
        message = db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
        metadata = file_share(json.loads(message['payload_json'])) if message else None
        if not metadata:
            raise ValueError('该记录不是可恢复的普通文件消息')
        parent = repair_parent(db, message_id, metadata)
        reason = '旧记录仅发送文件标题，待补附件' if parent['part'] == 'text-0' else '旧附件使用内部文件名，待按原名补发'
        now = time.time()
        db.execute("INSERT INTO media_jobs(message_id,media_type,source_ref,state,error,created_at,updated_at) SELECT ?, 'file','file-attachment','manual',?,?,? WHERE NOT EXISTS(SELECT 1 FROM media_jobs WHERE message_id=? AND source_ref='file-attachment')",
                   (message_id, reason, now, now, message_id))
        return dict(db.execute("SELECT id,state FROM media_jobs WHERE message_id=? AND source_ref='file-attachment'", (message_id,)).fetchone())


def recover(mirror, config, store, job):
    with store.connect() as db:
        message = db.execute('SELECT * FROM messages WHERE id=? AND binding_id=?',
                             (job['message_id'], config['binding_id'])).fetchone()
    if not message:
        raise mirror.DeliveryUncertain('文件补发与绑定不一致')
    message = dict(message)
    row = json.loads(message['payload_json'])
    if row.get('talker') != config['wechat_talker'] or not file_share(row):
        raise mirror.DeliveryUncertain('文件源消息身份不匹配')
    try:
        with store.connect() as db:
            parent = repair_parent(db, message['id'], file_share(row))
    except ValueError as exc:
        raise mirror.DeliveryUncertain(str(exc)) from None
    receipt = store.delivery(message['id'], 'file-attachment')
    path = None
    if not receipt or receipt['state'] != 'sent':
        enriched = {**row, 'binding_id': config['binding_id'],
                    'media_dir': config.get('media_dir', str(mirror.MEDIA_DIR))}
        path = saved_upload(mirror, enriched, receipt)
        if not path:
            try:
                path, _ = stage_file(mirror, enriched)
            finally:
                mirror.check_control(config, store)
            update_rejected_path(store, message['id'], 'file-attachment', path)
        mirror.send_part(config, store, message, 'file-attachment',
                         ['+messages-reply', '--message-id', parent['feishu_message_id'], '--file', path])
    with store.connect() as db:
        db.execute("UPDATE media_jobs SET state='uploaded',error=NULL,attempts=attempts+1,updated_at=? WHERE id=?", (time.time(), job['id']))
    mirror.cleanup_staged(path)
