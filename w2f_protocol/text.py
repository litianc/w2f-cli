"""Pure UTF-8 text layout shared by legacy mirroring and cloud delivery."""


def split_text(text, byte_limit=12000):
    parts, current, length = [], [], 0
    for char in text:
        size = len(char.encode('utf-8'))
        if length + size > byte_limit:
            parts.append(''.join(current)); current = []; length = 0
        current.append(char); length += size
    if current:
        parts.append(''.join(current))
    return parts or ['']


def attributed_text(name, body):
    return [f'{name}：{part}' for part in split_text(body)]
