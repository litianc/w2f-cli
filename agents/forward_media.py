"""Durable, per-child image/video delivery for merged-forward records."""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from integrations.forward_records import parse_record, walk_records
from integrations.reader_forward import matches_video


def _item_plan(row, records, record_parts, kind):
    items = row.get('forward_items') or parse_record(str(row.get('text') or row.get('content') or ''))
    media = []
    for ordinal, (path, item) in enumerate(walk_records(items)):
        if item.get('kind') != kind:
            continue
        line = records[ordinal]
        # The rendered record already resolved and froze the inner speaker.
        name = line.lstrip().split('：', 1)[0]
        media.append({'path':path, 'name':name, 'record_number':ordinal+1,
                       'media':item.get('media') or {}, 'text_part':record_parts[ordinal]})
    return media


def video_plan(row,records,record_parts):
    return _item_plan(row,records,record_parts,'video')


def image_plan(row,records,record_parts):
    return _item_plan(row,records,record_parts,'image')


def stage_video(mirror, config, row, video):
    media = video['media']
    if not re.fullmatch(r'\d+(?:\.\d+)*', video['path']):
        raise mirror.CliError('resource_invalid: 转发子项身份无效')
    directory = Path(config.get('media_dir') or mirror.MEDIA_DIR).expanduser()
    namespace = hashlib.sha256(str(row['talker']).encode()).hexdigest()[:16]
    path = directory / f"{config['binding_id']}-{namespace}-{int(row['local_id'])}-forward-{video['path']}.mp4"
    if matches_video(path, media):
        return str(path)
    command = [str(config.get('wechat_reader') or mirror.WECHAT_READER), 'forward-resource',
               '--talker', row['talker'], '--local-id', str(row['local_id']),
               '--server-id', str(row.get('server_id') or 0), '--create-time', str(row['create_time']),
               '--item-path', video['path']]
    try:
        result = mirror.subprocess.run(command, cwd=mirror.ROOT, capture_output=True,
                                       text=True, encoding='utf-8', timeout=60)
        if result.returncode:
            raise mirror.CliError('resource_missing: 转发视频读取失败，请检查读取器扩展及本机下载状态')
        data = json.loads(result.stdout).get('data', {})
        expected = {'contract':'forward_resource_v1','talker':row['talker'],
                    'local_id':int(row['local_id']),'server_id':int(row.get('server_id') or 0),
                    'create_time':int(row['create_time']),'item_path':video['path'],'media':media}
        if any(data.get(key) != value for key, value in expected.items()):
            raise mirror.CliError('resource_invalid: 转发视频资源身份不匹配')
        for resource in data.get('resources', []):
            for local_path in resource.get('local_paths', []):
                source = Path(local_path)
                if not matches_video(source, media):
                    continue
                directory.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'wb') as target, source.open('rb') as origin:
                    os.fchmod(target.fileno(), 0o600)
                    remaining = int(media['size']) + 1
                    while remaining:
                        chunk = origin.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        target.write(chunk)
                        remaining -= len(chunk)
                if matches_video(path, media):
                    return str(path)
                path.unlink(missing_ok=True)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise mirror.CliError('resource_missing: 转发视频读取或暂存失败') from None
    raise mirror.CliError('resource_missing: 本机未找到完整转发视频；请在微信内下载后重试')


def deliver_media(mirror, config, store, message, row, plan, text_index):
    items = [(item,'video') for item in plan.get('videos',[])] + [(item,'image') for item in plan.get('images',[])]
    for item, kind in sorted(items,key=lambda value:value[0]['record_number']):
        if item['text_part'] != text_index:
            continue
        ref = 'forward-' + kind + ':' + item['path']
        with store.connect() as db:
            job = db.execute('SELECT * FROM media_jobs WHERE message_id=? AND source_ref=?', (message['id'], ref)).fetchone()
        if config.get('_retry_media_job_id') and (not job or job['id'] != config['_retry_media_job_id']):
            continue
        part = 'forward-' + kind + '-' + item['path']
        receipt = store.delivery(message['id'], part)
        if receipt and receipt['state'] == 'sent':
            if not job or job['state'] != 'uploaded':
                store.add_media_job(message['id'], kind, ref, None, 'uploaded')
            if job and job['staging_path']:
                mirror.cleanup_staged(job['staging_path'])
                mirror.cleanup_staged(str(Path(job['staging_path']).with_suffix('.cover.jpg')))
                if kind=='image':mirror.cleanup_staged(str(Path(job['staging_path']).with_suffix('.json')))
            continue
        parent = store.delivery(message['id'], f'text-{text_index}')
        target = ['+messages-reply', '--message-id', parent['feishu_message_id']]
        media_label = '视频' if kind=='video' else '图片'
        label = f"{item['name']}：（转发记录第 {item['record_number']} 条{media_label}）"
        send = lambda suffix, args: mirror.send_part(config, store, message, part+suffix, target+args)
        # Text replies have receipts too. In particular, a lost fallback
        # receipt must not be skipped just because the job is a placeholder.
        for suffix in ('-name', '-fallback', '-quality-thumbnail', '-quality-original'):
            pending = store.delivery(message['id'], part+suffix)
            if pending and pending['state'] != 'sent':
                mirror.send_part(config,store,message,part+suffix,json.loads(pending['payload_json']))
        if (job and job['state'] == 'placeholder' and not config.get('_retry_media')
                and not (receipt and receipt['state'] in {'sending','uncertain'})):
            send('-fallback', ['--text', label + f'[{media_label}资源不可用，待人工排查]'])
            continue
        if not job:
            store.add_media_job(message['id'], kind, ref, None, 'pending')
        path = None
        try:
            if kind=='video':
                path, preview = stage_video(mirror, config, row, item),False
            else:
                from agents.forward_images import stage_image, freeze_attempt
                refresh = bool(receipt and receipt['state']=='pending' and receipt['attempted_at'] is None)
                frozen=None
                if receipt and receipt['state'] in {'sending','uncertain'}:
                    frozen=store.setting(f'forward-image-attempt:{message["id"]}:{part}')
                    if not frozen:raise mirror.DeliveryUncertain('缺少原图片发送快照，请先核对飞书回执')
                path, preview = stage_image(mirror,config,row,item,refresh=refresh,frozen=frozen)
                freeze_attempt(mirror,store,message['id'],part,path,preview,receipt)
            with store.connect() as db:
                db.execute("UPDATE media_jobs SET staging_path=? WHERE message_id=? AND source_ref=?", (path,message['id'],ref))
            send('-name', ['--text', label + ('（原图未同步，已发送缩略图）' if preview else '')])
            cover = None
            args = ['--image',path]
            if kind=='video':
                cover = Path(path).with_suffix('.cover.jpg')
                if cover.is_symlink():
                    raise mirror.CliError('resource_invalid: 视频封面暂存路径异常')
                if not cover.is_file():
                    cover = mirror.video_cover(Path(path))
                args = ['--video', path, '--video-cover', str(cover)] if cover else ['--file', path]
            else:
                # Only a definite rejection permits different upload bytes or
                # format. Never change an unknown delivery's saved payload.
                with store.connect() as db:
                    db.execute("UPDATE deliveries SET payload_json=? WHERE message_id=? AND part=? AND state='pending' AND attempted_at IS NULL",
                               (json.dumps(target+args,ensure_ascii=False),message['id'],part))
                name_receipt=store.delivery(message['id'],part+'-name')
                named_preview='已发送缩略图' in json.loads(name_receipt['payload_json'])[-1]
                if named_preview!=preview:
                    quality='thumbnail' if preview else 'original'
                    note='原图未同步，本次补发缩略图' if preview else '已获取原图，本次补发原图'
                    send('-quality-'+quality,['--text',label+'（'+note+'）'])
            send('', args)
            store.add_media_job(message['id'], kind, ref, path, 'uploaded')
            mirror.cleanup_staged(path)
            if cover:
                mirror.cleanup_staged(str(cover))
            if kind=='image':mirror.cleanup_staged(str(Path(path).with_suffix('.json')))
        except (mirror.CliError, mirror.DeliveryUncertain) as exc:
            if isinstance(exc, mirror.DeliveryUncertain):
                store.add_media_job(message['id'], kind, ref, path, 'manual', '媒体发送回执不确定，请先核对飞书消息')
                raise
            if exc.uncertain:
                store.add_media_job(message['id'], kind, ref, path, 'pending', '媒体发送回执暂未返回')
                raise
            if receipt and receipt['state'] in {'sending','uncertain'}:
                store.add_media_job(message['id'], kind, ref, path, 'manual', '媒体发送回执不确定，请先核对飞书消息')
                raise mirror.DeliveryUncertain('转发媒体发送回执不确定，请核对后重试') from None
            store.add_media_job(message['id'], kind, ref, path, 'placeholder', mirror.safe_error(exc))
            send('-fallback', ['--text', label + f'[{media_label}资源不可用，待人工排查]'])


def has_media_failures(store, message_id):
    with store.connect() as db:
        return bool(db.execute("SELECT 1 FROM media_jobs WHERE message_id=? AND (source_ref LIKE 'forward-video:%' OR source_ref LIKE 'forward-image:%') AND state!='uploaded' LIMIT 1", (message_id,)).fetchone())


def queue_video_repair(mirror, store, config, message_id):
    return queue_media_repair(mirror,store,config,message_id,'video')


def queue_media_repair(mirror, store, config, message_id, kind):
    """Explicit repair of one historical record; never rerender/resend its text."""
    with store.connect() as db:
        value = db.execute('SELECT * FROM messages WHERE id=? AND binding_id=?', (message_id, config['binding_id'])).fetchone()
    if not value or value['state'] not in {'sent','placeholder'}:
        raise ValueError('请选择已同步的转发记录')
    message = dict(value)
    row = json.loads(message['payload_json'])
    if not mirror.is_forward_row(row):
        raise ValueError('该消息不是合并转发')
    plan = store.setting(f'render:{message_id}')
    if not plan:
        raise ValueError('历史渲染记录不存在，需要人工核对')
    if kind not in {'image','video'}:raise ValueError('不支持的转发媒体类型')
    section = 'images' if kind=='image' else 'videos'
    if section not in plan:
        # Old stored forward_items predate resource descriptors; reparse local
        # source XML, but require its rendered text to match the saved plan.
        row['forward_items'] = parse_record(str(row.get('text') or row.get('content') or ''))
        records = mirror.forward_record_texts(row, config, store)
        from integrations.forward_layout import split_forward_records
        parts, record_parts = split_forward_records(records, message['sender_name'], with_record_parts=True)
        if parts != plan['parts']:
            raise ValueError('历史姓名或正文已有变化，需要人工核对，未重新发送')
        plan[section] = _item_plan(row, records, record_parts, kind)
    if not plan[section]:
        raise ValueError('该转发记录没有可识别的指定媒体子项')
    for index in range(len(plan['parts'])):
        receipt = store.delivery(message_id, f'text-{index}')
        if not receipt or receipt['state'] != 'sent':
            raise ValueError('转发正文回执不完整，需要先核对')
    store.save_setting(f'render:{message_id}', plan)
    for item in plan[section]:
        ref = 'forward-' + kind + ':' + item['path']
        with store.connect() as db:
            exists = db.execute('SELECT id,state,attempts FROM media_jobs WHERE message_id=? AND source_ref=?', (message_id, ref)).fetchone()
            if exists and exists['state'] in {'placeholder','failed','manual'}:
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (f"media_retry_base:{exists['id']}",json.dumps(exists['attempts'])))
                db.execute("UPDATE media_jobs SET state='pending',updated_at=0 WHERE id=?",(exists['id'],))
        if not exists:
            store.add_media_job(message_id, kind, ref, None, 'pending')
    return {'queued':True, 'message':'转发媒体已排队；正文保留，已有成功回执不会重复发送'}
