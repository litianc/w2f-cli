"""Article metadata from explicit reader fields or WeChat type-5 XML.

No page scraping, URL shortening, query rewriting, or network access.
"""
from __future__ import annotations
import re
import urllib.parse
import xml.etree.ElementTree as ET


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _url(value):
    value = _text(value)
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme in {'http', 'https'} and parsed.hostname and not any(ord(c) < 32 for c in value):
            return value
    except ValueError:
        pass
    return ''


def reader_article(message):
    """Preserve fields already decoded by WDA without decoding URL escapes again."""
    return {
        'title': _text(message.get('title')),
        'url': _url(message.get('url')),
        'description': _text(message.get('description') or message.get('des')),
        'cover_url': _url(message.get('cover_url') or message.get('coverUrl') or message.get('thumbUrl') or message.get('thumburl')),
    }


def article_share(row):
    supplied = row.get('article')
    if isinstance(supplied, dict):
        return reader_article(supplied)
    for field in ('content', 'text'):
        raw = _text(row.get(field))
        start = raw.find('<')
        if start < 0:
            continue
        try:
            root = ET.fromstring(raw[start:])
        except ET.ParseError:
            continue
        app = root if root.tag == 'appmsg' else root.find('.//appmsg')
        if app is None or _text(app.findtext('type')) != '5':
            continue
        # ElementTree decodes entities and unwraps CDATA. A second unescape
        # would change intentionally encoded query parameter values.
        return reader_article({'title': app.findtext('title'), 'url': app.findtext('url'),
                               'description': app.findtext('des'), 'thumburl': app.findtext('thumburl')})
    return None


def article_body(article):
    parts = [article.get('title'), article.get('description'), article.get('url')]
    return '\n'.join(dict.fromkeys(part for part in parts if part)) or '[文章链接暂不可用]'


def normalize_pasted_article_url(raw):
    """Repair known HTML-escaped delimiters in a standalone WeChat URL only.

    Structured XML/reader fields have already been decoded and never use this.
    Keep percent encodings, parameter values, order and fragment unchanged.
    """
    value = raw.strip()
    if not _url(value) or any(c.isspace() for c in value):
        return raw
    parsed = urllib.parse.urlsplit(value)
    keys = urllib.parse.parse_qs(parsed.query)
    if (parsed.hostname != 'mp.weixin.qq.com' or parsed.path != '/s'
            or '__biz' not in keys or 'amp;mid' not in keys or 'mid' in keys):
        return raw
    prefix, _, rest = value.partition('?')
    query, fragment_mark, fragment = rest.partition('#')
    query = re.sub(r'&amp;(?=(?:mid|idx|sn|chksm|mpshare|scene|srcid|sharer_shareinfo(?:_first)?)=)', '&', query)
    return raw.replace(value, prefix + '?' + query + fragment_mark + fragment, 1)
