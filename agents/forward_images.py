"""Decode and verify forward image candidates without trusting their filenames."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from PIL import Image
from agents.media_images import decode_dat, verified_image, ImageResourceError


def _write_private(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.forward-image-', dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(data)
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage_image(mirror, config, row, item, *, refresh=False, frozen=None):
    media = item['media']
    if not re.fullmatch(r'\d+(?:\.\d+)*', item['path']):
        raise mirror.CliError('resource_invalid: 转发子项身份无效')
    descriptors = [media,media.get('thumbnail') or {}]
    if not any(re.fullmatch(r'[a-f0-9]{32}',x.get('md5','')) and 0 < int(x.get('size') or 0) <= 100*1024*1024 for x in descriptors):
        raise mirror.CliError('resource_missing: 转发图片缺少可靠资源描述')
    directory=Path(config.get('media_dir') or mirror.MEDIA_DIR).expanduser()
    namespace=hashlib.sha256(str(row['talker']).encode()).hexdigest()[:16]
    base=directory/f"{config['binding_id']}-{namespace}-{int(row['local_id'])}-forward-image-{item['path']}"
    manifest=Path(str(base)+'.json')
    # A private integrity manifest pins the converted bytes (WXGF may become
    # PNG), so a lost upload receipt can be retried even after cache eviction.
    cached=None
    try:
        if not manifest.is_symlink() and manifest.is_file():
            saved=json.loads(manifest.read_text())
            if saved['media']==media and saved['extension'] in {'.jpg','.png','.gif','.webp'}:
                path=Path(str(base)+saved['extension'])
                if not path.is_symlink() and path.stat().st_size<=100*1024*1024:
                    data=path.read_bytes()
                    if hashlib.sha256(data).hexdigest()==saved['sha256'] and verified_image(data):
                        matches_frozen=(not frozen or (saved['sha256']==frozen['sha256']
                                        and saved['thumbnail_only']==frozen['thumbnail_only'] and str(path)==frozen['path']))
                        if matches_frozen:
                            cached=(str(path),saved['thumbnail_only'])
                            if not refresh or not saved['thumbnail_only']:
                                return cached
    except (OSError,ValueError,KeyError):
        pass
    command=[str(config.get('wechat_reader') or mirror.WECHAT_READER),'forward-resource',
             '--talker',row['talker'],'--local-id',str(row['local_id']),
             '--server-id',str(row.get('server_id') or 0),'--create-time',str(row['create_time']),
             '--item-path',item['path']]
    try:
        response=mirror.subprocess.run(command,cwd=mirror.ROOT,capture_output=True,text=True,encoding='utf-8',timeout=60)
        if response.returncode:
            if cached:return cached
            raise mirror.CliError('resource_missing: 转发图片读取失败，请检查读取器扩展及本机下载状态')
        value=json.loads(response.stdout).get('data',{})
        expected={'contract':'forward_resource_v1','talker':row['talker'],'local_id':int(row['local_id']),
                  'server_id':int(row.get('server_id') or 0),'create_time':int(row['create_time']),
                  'item_path':item['path'],'media':media}
        if any(value.get(key)!=item for key,item in expected.items()):
            raise mirror.CliError('resource_invalid: 转发图片资源身份不匹配')
        resources=sorted(value.get('resources',[]),key=lambda r:bool(r.get('thumbnail_only')))
        last_error=None
        for resource in resources:
            preview=resource.get('thumbnail_only') is True
            if frozen and preview!=frozen['thumbnail_only']:
                continue
            descriptor=(media.get('thumbnail') or {}) if preview else media
            if not re.fullmatch(r'[a-f0-9]{32}',descriptor.get('md5','')) or not 0 < int(descriptor.get('size') or 0)<=100*1024*1024:
                continue
            for source_value in resource.get('local_paths',[]):
                source=Path(source_value)
                if source.is_symlink() or not source.is_file():
                    continue
                try:
                    data=decode_dat(source,allow_macos_derivation=config.get('macos_image_key_derivation') is True,
                                    expected_md5=descriptor['md5'],expected_size=descriptor['size'])
                except (ImageResourceError,OSError) as exc:
                    last_error=exc
                    continue
                with Image.open(BytesIO(data)) as image:
                    extension={'JPEG':'.jpg','PNG':'.png','GIF':'.gif','WEBP':'.webp'}.get(image.format)
                    if not extension:
                        output=BytesIO();image.save(output,format='PNG');data=output.getvalue();extension='.png'
                path=Path(str(base)+extension)
                if frozen and (hashlib.sha256(data).hexdigest()!=frozen['sha256'] or str(path)!=frozen['path']):
                    continue
                directory.mkdir(parents=True,exist_ok=True,mode=0o700)
                _write_private(path,data)
                _write_private(manifest,json.dumps({'media':media,'extension':extension,
                               'sha256':hashlib.sha256(data).hexdigest(),'thumbnail_only':preview}).encode())
                return str(path),preview
        if cached:return cached
        if last_error:
            raise mirror.CliError('decode_failed: 转发图片未通过解码或源内容校验')
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise mirror.CliError('resource_missing: 转发图片读取或暂存失败') from None
    raise mirror.CliError('resource_missing: 本机未找到转发图片；请在微信内下载后重试')


def freeze_attempt(mirror,store,message_id,part,path,preview,receipt):
    """Unknown sends must retry the very same bytes, even if disk cache changes."""
    key=f'forward-image-attempt:{message_id}:{part}'
    current={'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
             'thumbnail_only':preview,'path':path}
    saved=store.setting(key)
    if receipt and receipt['state'] in {'sending','uncertain'}:
        if saved!=current:
            raise mirror.DeliveryUncertain('图片发送结果未知，原发送文件无法重建；请先核对飞书回执')
    else:
        store.save_setting(key,current)
