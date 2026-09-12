"""Structured merged-forward decoding; shared by the reader and mirror."""
import html
import xml.etree.ElementTree as ET


def parse_record(raw, depth=0):
    if depth > 4:
        return [{'sender': '未知发言人', 'sender_id': '', 'kind': 'placeholder', 'body': '[嵌套转发层级过深]'}]
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, TypeError):
        try:
            root = ET.fromstring(html.unescape(raw))
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
            sender_id = field(item, 'dataitemsource/realchatname', 'dataitemsource/fromusr', 'fromusr')
            body = field(item, 'datadesc', 'content', 'datatitle', 'title', 'des')
            kind = {'1': 'text','2': 'image','3': 'voice','4': 'video','5': 'link','8': 'file','17': 'forward'}.get(item.get('datatype'), 'text')
            if not item.get('datatype'):
                kind = {'3':'image','43':'video','34':'voice','6':'file','47':'sticker'}.get(field(item,'type'), 'text')
            nested = field(item, 'recorditem') or body
            if kind == 'forward' or '<recorditem' in nested or '<datalist' in nested:
                children = parse_record(nested, depth + 1)
            else:
                children = []
            url = field(item, 'weburl', 'url')
            if url.startswith(('http://','https://')) and url not in body:
                body += ('\n' if body else '') + url
            if not body:
                body = {'image':'[图片]','video':'[视频]','voice':'[语音]','file':'[文件]','sticker':'[表情]'}.get(kind,'[原消息内容不可用]')
            if body.lstrip().startswith('<'):
                body = '[原消息内容不可用]' if not children else ''
            result.append({'sender':sender, 'sender_id':sender_id, 'body':body, 'kind':kind, 'children':children})
    return result
