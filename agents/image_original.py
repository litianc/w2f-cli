"""Persist ordinary-image quality and explicitly recover a delivered preview."""
import hashlib
import json
import tempfile
import time
from pathlib import Path


WAITING = '缩略图已送达；原图尚不可用或解码失败。可在本机微信打开并下载原图'
AUTO_DELAYS = (30, 120, 600)


def _saved_snapshot(mirror, store, key, receipt):
    saved = store.setting(key)
    if saved:
        frozen = Path(saved['path'])
        try:
            valid = (not frozen.is_symlink() and frozen.is_file()
                     and hashlib.sha256(frozen.read_bytes()).hexdigest() == saved['sha256'])
        except OSError:
            valid = False
        if valid:
            return saved
    # Only a known rejection or no attempt permits new bytes. Expiration of a
    # local file is not evidence that an uncertain remote send failed.
    if receipt and not (receipt['state'] == 'pending' and receipt['attempted_at'] is None):
        raise mirror.DeliveryUncertain('图片发送快照缺失或已变化，请先核对飞书回执')
    if saved:
        store.save_setting(key, None)
    return None


def _snapshot(mirror, store, key, path, preview=False, reason=None):
    source = Path(path)
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    frozen = source.with_name(f'{source.stem}-{key.split(":")[0]}-{digest[:16]}{source.suffix}')
    if frozen.is_symlink():
        raise mirror.DeliveryUncertain('图片发送快照路径异常，请先核对飞书回执')
    # NamedTemporaryFile gives private permissions and avoids a partial file
    # becoming the persisted upload snapshot after an interrupted write.
    with tempfile.NamedTemporaryFile(dir=source.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    try:
        temporary.replace(frozen)
    finally:
        temporary.unlink(missing_ok=True)
    saved = {'path': str(frozen), 'sha256': digest, 'thumbnail_only': bool(preview), 'reason': reason}
    store.save_setting(key, saved)
    mirror.cleanup_staged(path)
    return saved


def _update_rejected_path(store, message_id, part, path):
    # prepare_delivery deliberately freezes payloads. A confirmed rejection
    # can safely switch the local upload path, preserving target and UUID.
    with store.connect() as db:
        receipt = db.execute("SELECT payload_json FROM deliveries WHERE message_id=? AND part=? AND state='pending' AND attempted_at IS NULL",
                             (message_id, part)).fetchone()
        if receipt:
            args = json.loads(receipt['payload_json'])
            args[args.index('--image') + 1] = path
            db.execute("UPDATE deliveries SET payload_json=? WHERE message_id=? AND part=? AND state='pending' AND attempted_at IS NULL",
                       (json.dumps(args, ensure_ascii=False), message_id, part))


def _wait_for_original(mirror, config, store, message, row, saved):
    seconds = max(0, min(5, float(config.get('image_original_wait_seconds', 5))))
    key = f'image-original-grace:{message["id"]}'
    expires = store.setting(key)
    if expires is None:
        expires = time.time() + seconds
        store.save_setting(key, expires)
    deadline = time.monotonic() + min(seconds, max(0, expires - time.time()))
    while time.monotonic() < deadline:
        mirror.check_control(config, store)
        time.sleep(max(0, min(1, deadline - time.monotonic())))
        mirror.check_control(config, store)
        if time.monotonic() >= deadline:
            break
        try:
            path, preview = mirror.stage_media({**row, '_image_lookup_deadline': deadline}, 'image')
        except mirror.CliError:
            continue  # Keep the already validated preview as the fallback.
        if path and not preview:
            old_path = saved['path']
            saved = _snapshot(mirror, store, f'image-selection:{message["id"]}', path, False)
            mirror.cleanup_staged(old_path)
            break
        mirror.cleanup_staged(path)
    return saved


def stage_initial(mirror, store, message, row, *, config=None):
    key = f'image-selection:{message["id"]}'
    receipt = store.delivery(message['id'], 'media')
    saved = _saved_snapshot(mirror, store, key, receipt)
    if not saved:
        path, preview = mirror.stage_media(row, 'image')
        if not path:
            return None, preview
        saved = _snapshot(mirror, store, key, path, preview, row.get('_image_quality_reason'))
    elif (saved['thumbnail_only'] and (not receipt or
          (receipt['state'] == 'pending' and receipt['attempted_at'] is None))):
        # A rejected preview may be upgraded; an unknown upload must keep its
        # original bytes. Retain the good preview if no better resource exists.
        try:
            path, preview = mirror.stage_media(row, 'image')
        except mirror.CliError:
            path, preview = None, True
        if path and not preview:
            old_path = saved['path']
            saved = _snapshot(mirror, store, key, path, False)
            mirror.cleanup_staged(old_path)
        else:
            mirror.cleanup_staged(path)
    if saved['thumbnail_only'] and not receipt:
        saved = _wait_for_original(mirror, config or {}, store, message, row, saved)
    _update_rejected_path(store, message['id'], 'media', saved['path'])
    return saved['path'], saved['thumbnail_only']


def send_quality_notice(store, message_id, preview, target, send):
    caption = store.delivery(message_id, 'name')
    visible_preview = None
    if caption and caption['state'] == 'sent':
        args = json.loads(caption['payload_json'])
        visible_preview = '已发送缩略图' in args[args.index('--text') + 1]
    with store.connect() as db:
        notices = [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE message_id=? AND part LIKE 'image-quality-%' ORDER BY rowid", (message_id,))]
    for notice in notices:
        args = json.loads(notice['payload_json'])
        if notice['state'] in {'sending', 'uncertain'}:
            # Resolve a prior notice before a new one can contradict it. The
            # existing send helper retains its target, payload and UUID.
            send(notice['part'], args)
            notice['state'] = 'sent'
        if notice['state'] == 'sent':
            visible_preview = '缩略图' in args[args.index('--text') + 1]
    if visible_preview == preview or (visible_preview is None and not preview):
        return
    body = '（原图未同步，本次补发缩略图）' if preview else '（已获取原图，本次补发原图）'
    part = f'image-quality-{len(notices)}'
    if notices and notices[-1]['state'] == 'pending':
        args = json.loads(notices[-1]['payload_json'])
        if args[args.index('--text') + 1] == body:
            part = notices[-1]['part']
    send(part, target + ['--text', body])


def register_preview(store, message_id, *, legacy=False, automatic=False):
    """Schedule only newly delivered previews; existing manual items stay manual."""
    receipt = store.delivery(message_id, 'media')
    selection = store.setting(f'image-selection:{message_id}') or {}
    if not receipt or receipt['state'] != 'sent':
        return False
    if not selection.get('thumbnail_only'):
        if not legacy or selection:
            return False
        caption = store.delivery(message_id, 'name')
        if not caption or caption['state'] != 'sent':
            return False
        args = json.loads(caption['payload_json'])
        if '--text' not in args or '（原图未同步，已发送缩略图）' not in args[args.index('--text') + 1]:
            return False
    reason = selection.get('reason')
    prefix = {'decode_failed': '原图解码失败。', 'resource_missing': '原图暂存失败。',
              'original_unavailable': '本机尚无可用原图。'}.get(reason, '')
    now = time.time()
    with store.connect() as db:
        inserted = db.execute("""INSERT INTO media_jobs(message_id,media_type,source_ref,state,error,created_at,updated_at)
                      SELECT ?,'image_original','original:v1',?,?,?,?
                      WHERE NOT EXISTS(SELECT 1 FROM media_jobs WHERE message_id=? AND media_type='image_original') RETURNING id""",
                   (message_id, 'pending' if automatic else 'manual', prefix + WAITING, now, now, message_id)).fetchone()
        if inserted and automatic:
            db.execute('INSERT INTO settings(key,value) VALUES(?,?)',
                       (f'image-original-auto:{inserted[0]}', json.dumps({'active': True, 'next_attempt_at': now + AUTO_DELAYS[0]})))
    return True


def register_historical(store, message_id):
    with store.connect() as db:
        row = db.execute('SELECT payload_json,state FROM messages WHERE id=?', (message_id,)).fetchone()
    if (not row or row['state'] not in {'sent', 'placeholder'}
            or json.loads(row['payload_json']).get('kind_name') != 'image'
            or not register_preview(store, message_id, legacy=True)):
        raise ValueError('请选择已有缩略图成功回执的普通图片；旧记录需先核对实际发送质量')
    return {'queued': False, 'message': '已登记补原图待办；点击“补原图”后才会发送，旧记录请先核对实际图片'}


def finish_auto_attempt(store, job_id):
    """Advance the durable finite schedule only after an attempt completes."""
    key = f'image-original-auto:{job_id}'
    with store.connect() as db:
        job = db.execute('SELECT state,attempts,error FROM media_jobs WHERE id=?', (job_id,)).fetchone()
        active = job['state'] in {'pending', 'processing'} and job['attempts'] < len(AUTO_DELAYS)
        if job['state'] in {'pending', 'processing'} and not active:
            db.execute("UPDATE media_jobs SET state='manual',error=?,updated_at=? WHERE id=?",
                       ('自动补原图已尝试 3 次。' + (job['error'] or WAITING), time.time(), job_id))
        value = {'active': active, 'next_attempt_at': time.time() + AUTO_DELAYS[job['attempts']] if active else None}
        db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(value)))


def retry_status(store, job):
    schedule = store.setting(f'image-original-auto:{job["id"]}', {})
    active = bool(schedule.get('active') and job['state'] in {'pending', 'processing'})
    due = None
    if active:
        # Attempt count and timestamp commit together, so a crash before the
        # schedule-setting write cannot accidentally skip the next backoff.
        attempts = job['attempts']
        due = ((job['created_at'] if attempts == 0 else job['updated_at']) + AUTO_DELAYS[attempts]
               if attempts < len(AUTO_DELAYS) else time.time())
    return {'auto_retry': active, 'next_attempt_at': due,
            'max_attempts': len(AUTO_DELAYS)}


def next_retry_delay(store, binding_id, normal_delay):
    """Wake for queued media retries even in a quiet chat."""
    with store.connect() as db:
        jobs = [dict(r) for r in db.execute("""SELECT j.* FROM media_jobs j JOIN messages m ON m.id=j.message_id
                  WHERE m.binding_id=? AND j.state IN ('pending','processing')""", (binding_id,))]
    delay = normal_delay
    for job in jobs:
        schedule = store.setting(f'image-original-auto:{job["id"]}', {})
        if schedule.get('active'):
            due = retry_status(store,job)['next_attempt_at']
        else:
            baseline = int(store.setting(f'media_retry_base:{job["id"]}', 0))
            attempts = max(0, job['attempts'] - baseline)
            due = job['updated_at'] + (30 if attempts == 1 else 120 if attempts == 2 else 0)
        delay = min(delay, max(0, due - time.time()))
    return delay


def recover(mirror, config, store, job):
    with store.connect() as db:
        value = db.execute('SELECT * FROM messages WHERE id=? AND binding_id=?',
                           (job['message_id'], config['binding_id'])).fetchone()
    if not value:
        raise mirror.DeliveryUncertain('补原图的消息与绑定不一致')
    message = dict(value)
    row = json.loads(message['payload_json'])
    if row.get('kind_name') != 'image' or row.get('talker') != config['wechat_talker']:
        raise mirror.DeliveryUncertain('补原图的源消息身份不一致')
    parent = store.delivery(message['id'], 'media')
    if not parent or parent['state'] != 'sent' or not parent['feishu_message_id']:
        raise mirror.DeliveryUncertain('缺少原图片成功回执，请先核对')
    part = 'image-original'
    receipt = store.delivery(message['id'], part)
    key = f'image-original:{message["id"]}'
    path = None
    if not receipt or receipt['state'] != 'sent':
        saved = _saved_snapshot(mirror, store, key, receipt)
        if not saved:
            enriched = {**row, 'binding_id': config['binding_id'],
                        'media_dir': config.get('media_dir', str(mirror.MEDIA_DIR)),
                        'macos_image_key_derivation': config.get('macos_image_key_derivation') is True}
            try:
                path, preview = mirror.stage_media(enriched, 'image')
            finally:
                # A pause during lookup also interrupts missing/failed
                # resources, before either outcome consumes the retry budget.
                mirror.check_control(config, store)
            if not path or preview:
                mirror.cleanup_staged(path)
                automatic = store.setting(f'image-original-auto:{job["id"]}', {}).get('active', False)
                with store.connect() as db:
                    db.execute("UPDATE media_jobs SET state=?,error=?,attempts=attempts+1,updated_at=? WHERE id=?",
                               ('pending' if automatic else 'manual', WAITING, time.time(), job['id']))
                return
            saved = _snapshot(mirror, store, key, path)
        path = saved['path']
        with store.connect() as db:
            db.execute('UPDATE media_jobs SET staging_path=? WHERE id=?', (path, job['id']))
        _update_rejected_path(store, message['id'], part, path)
        mirror.send_part(config, store, message, part,
                         ['+messages-reply', '--message-id', parent['feishu_message_id'], '--image', path])
    with store.connect() as db:
        db.execute("UPDATE media_jobs SET state='uploaded',error=NULL,attempts=attempts+1,updated_at=? WHERE id=?",
                   (time.time(), job['id']))
    mirror.cleanup_staged(path or (store.setting(key) or {}).get('path'))
