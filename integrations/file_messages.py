"""Structured metadata for ordinary WeChat file shares (including type 74)."""
import hashlib
import re
import xml.etree.ElementTree as ET

MAX_FILE_BYTES = 100 * 1024 * 1024


def file_share(row, *, with_type=False):
    for field in ('content', 'text'):
        raw = str(row.get(field) or '').strip()
        if not raw.startswith('<') or '<!DOCTYPE' in raw.upper():
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        app = root if root.tag == 'appmsg' else root.find('appmsg')
        if app is None or (app.findtext('type') or '').strip() not in {'6', '74'}:
            continue
        title = (app.findtext('title') or '').strip()
        size = (app.findtext('appattach/totallen') or '').strip()
        digest = (app.findtext('md5') or app.findtext('appattach/filemd5') or '').strip().lower()
        result = {'name': title, 'size': int(size) if re.fullmatch(r'[0-9]{1,15}', size) else 0,
                  'md5': digest if re.fullmatch(r'[a-f0-9]{32}', digest) else ''}
        if with_type:
            result['app_type'] = int(app.findtext('type'))
        return result
    return None


def safe_filename(name):
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', name).strip(' .')
    if not name:
        return 'attachment.bin'
    # Keep the extension and leave room for filesystem UTF-8 byte limits.
    stem, dot, suffix = name.rpartition('.')
    if not dot or len(suffix) > 16:
        stem, dot, suffix = name, '', ''
    while len((stem + dot + suffix).encode()) > 220:
        stem = stem[:-1]
    return stem + dot + suffix


def matches_file(path, metadata):
    try:
        if path.is_symlink() or not path.is_file() or not 0 < metadata['size'] <= MAX_FILE_BYTES:
            return False
        if path.stat().st_size != metadata['size']:
            return False
        if metadata['md5']:
            digest = hashlib.md5()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            return digest.hexdigest() == metadata['md5']
        return True
    except OSError:
        return False
