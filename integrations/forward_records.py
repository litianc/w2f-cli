"""Structured merged-forward decoding; shared by the reader and mirror."""
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET


def stable_sender_id(value):
    value = str(value or '').strip()
    return value if (re.fullmatch(r'[A-Za-z0-9_.@-]+', value)
                     and not value.endswith('@chatroom')
                     and not value.startswith('unidentified:')) else ''


def _http_url(value):
    value = str(value or '').strip()
    if not value or any(c.isspace() or ord(c) < 32 or c in '\\<>"\x7f' for c in value):
        return ''
    try:
        page = urllib.parse.urlsplit(value)
        if (page.scheme not in {'http', 'https'} or not page.hostname
                or page.username is not None or page.password is not None
                or (page.port is not None and not 0 < page.port < 65536)):
            return ''
    except ValueError:
        return ''
    return value


_BARE_AMPERSAND = re.compile(r'&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,9}|#\d{1,5}|#x[0-9a-fA-F]{1,6});)')


def _rescue_bare_ampersands(text):
    """WeChat occasionally emits recordinfo whose article URLs carry raw '&'.

    ElementTree rejects the whole document for one bare ampersand. Escape only
    those (never real entities), so the URL text survives byte-for-byte.
    """
    return _BARE_AMPERSAND.sub('&amp;', text)


def parse_record(raw, depth=0):
    if depth > 4:
        return [{'sender': '未知发言人', 'sender_id': '', 'kind': 'placeholder', 'body': '[嵌套转发层级过深]'}]
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, TypeError):
        try:
            root = ET.fromstring(html.unescape(raw))
        except (ET.ParseError, TypeError):
            try:
                root = ET.fromstring(_rescue_bare_ampersands(html.unescape(raw)))
            except (ET.ParseError, TypeError):
                return []
    def field(node, *tags):
        for tag in tags:
            value = node.findtext(tag)
            if value:
                return value
        return ''
    records = [root] if root.tag in {'datalist', 'recordinfo', 'recorditem'} else root.findall('.//recorditem')
    result = []
    for record in records:
        if record.tag == 'recorditem' and record.text and record.text.strip():
            result.extend(parse_record(record.text.strip(), depth + 1))
            continue
        items = record.findall('./dataitem') if record.tag == 'datalist' else record.findall('./datalist/dataitem')
        for item in items:
            sender = field(item, 'sourcename', 'displayname', 'sendername') or '未知发言人'
            sender_id = next((valid for tag in ('dataitemsource/realchatname', 'dataitemsource/fromusr', 'fromusr')
                              if (valid := stable_sender_id(item.findtext(tag)))), '')
            body = field(item, 'datadesc', 'content', 'datatitle', 'title', 'des')
            kind = {'1': 'text','2': 'image','3': 'voice','4': 'video','5': 'link','8': 'file','17': 'forward'}.get(item.get('datatype'), 'text')
            if not item.get('datatype'):
                kind = {'3':'image','43':'video','34':'voice','6':'file','47':'sticker'}.get(field(item,'type'), 'text')
            nested = field(item, 'recorditem') or body
            if kind == 'forward' or '<recorditem' in nested or '<datalist' in nested:
                children = parse_record(nested, depth + 1)
            else:
                children = []
            if kind == 'link':
                # Native merged forwards store article fields in weburlitem,
                # not datatitle/datadesc. Read only those explicit fields so
                # avatars and signed cover resources never become article URLs.
                title = field(item, 'weburlitem/title', 'datatitle', 'title')
                description = field(item, 'weburlitem/desc')
                if not description:
                    description = field(item, 'datadesc', 'content', 'des')
                    # Legacy content may hold an entire app-message envelope.
                    # Keep its raw XML out without discarding explicit titles.
                    if description.lstrip().startswith('<'):
                        description = ''
                body = '\n'.join(dict.fromkeys(value for value in (title, description) if value))
                url = next((valid for tag in ('weburlitem/url', 'weburl', 'url', 'link')
                            if (valid := _http_url(item.findtext(tag)))), '')
            else:
                url = field(item, 'weburl', 'url')
            if url.lower().startswith(('http://','https://')) and url not in body:
                body += ('\n' if body else '') + url
            if not body:
                body = {'image':'[图片]','video':'[视频]','voice':'[语音]','file':'[文件]','sticker':'[表情]'}.get(kind,'[原消息内容不可用]')
            if kind != 'link' and body.lstrip().startswith('<'):
                body = '[原消息内容不可用]' if not children else ''
            entry = {'sender':sender, 'sender_id':sender_id, 'body':body, 'kind':kind, 'children':children}
            if kind in {'video', 'image'}:
                # CDN tickets/keys are not public URLs. Keep only the exact
                # content identity needed for a local, verified file lookup.
                digest = field(item, 'fullmd5').lower()
                def number(tag):
                    value = field(item, tag)
                    return int(value) if re.fullmatch(r'\d{1,12}', value) else 0
                entry['media'] = {'md5': digest if re.fullmatch(r'[a-f0-9]{32}', digest) else '',
                                  'size': number('datasize')}
                extension = field(item, 'datafmt').lower().lstrip('.')
                if kind == 'video':
                    entry['media'].update(duration=number('duration'), extension='.mp4' if extension == 'mp4' else '')
                else:
                    entry['media']['extension'] = '.' + extension if extension in {'jpg','jpeg','png','gif','webp','bmp','wxgf'} else ''
                    thumb = field(item, 'thumbfullmd5').lower()
                    if re.fullmatch(r'[a-f0-9]{32}', thumb) and number('thumbsize'):
                        entry['media']['thumbnail'] = {'md5':thumb, 'size':number('thumbsize')}
            result.append(entry)
    return result


def walk_records(items, prefix=''):
    """Stable positional identity even when two children share the same bytes."""
    for index, item in enumerate(items):
        path = prefix + str(index)
        yield path, item
        yield from walk_records(item.get('children') or [], path + '.')
