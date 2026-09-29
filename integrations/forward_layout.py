"""Lay out merged-forward records without losing their boundaries or text."""
import re


def _header(sender, index, total, continued):
    prefix = sender if index == 1 else ''
    continuation = '，续上一条' if continued else ''
    return f'{prefix}转发聊天记录（第 {index}/{total} 段{continuation}）\n'


def _split_record(text, budget_at_piece):
    current = b''
    index = 0
    for token in re.split(r'(https?://[^\s<>]+)', text, flags=re.I):
        data = token.encode('utf-8')
        if re.match(r'https?://', token, re.I):
            if current and len(current) + len(data) > budget_at_piece(index):
                yield current.decode('utf-8')
                current = b''
                index += 1
            if len(data) > budget_at_piece(index):
                raise ValueError('转发记录中的单个链接超过消息字节预算，需要人工处理')
            current += data
            continue
        while data:
            capacity = budget_at_piece(index) - len(current)
            piece = data[:capacity].decode('utf-8', errors='ignore').encode('utf-8')
            if not piece:
                yield current.decode('utf-8')
                current = b''
                index += 1
                continue
            current += piece
            data = data[len(piece):]
            if data:
                yield current.decode('utf-8')
                current = b''
                index += 1
    if current:
        yield current.decode('utf-8')


def split_forward_records(records, sender, byte_limit=12000, *, with_record_parts=False):
    """Use each part's actual header budget; show the outer name once.

    A normal part boundary separates records, just like an inter-record
    newline. Continuation parts join without a separator. Repack only when
    the final part count requires another digit in every header.
    """
    digits = 1
    while True:
        total_hint = int('9' * digits)

        def budget(index, continued):
            available = byte_limit - len(_header(sender, index, total_hint, continued).encode('utf-8'))
            if available < 4:
                raise ValueError('转发消息标题过长，无法在字节预算内保留正文')
            return available

        chunks, record_parts = [], []
        current, size, continued = '', 0, False
        for record in records:
            record_size = len(record.encode('utf-8'))
            if current and size + 1 + record_size <= budget(len(chunks) + 1, continued):
                record_parts.append(len(chunks))
                current += '\n' + record
                size += 1 + record_size
                continue
            if current:
                chunks.append((current, continued))
            record_parts.append(len(chunks))
            current, size, continued = '', 0, False
            start = len(chunks) + 1
            if record_size <= budget(start, False):
                current, size = record, record_size
            else:
                pieces = list(_split_record(record, lambda i: budget(start + i, i > 0)))
                chunks.extend((piece, i > 0) for i, piece in enumerate(pieces[:-1]))
                current = pieces[-1]
                size, continued = len(current.encode('utf-8')), len(pieces) > 1
        if current:
            chunks.append((current, continued))
        total = len(chunks)
        if len(str(total)) > digits:
            digits = len(str(total))
            continue
        parts = [_header(sender, index, total, continuation) + text
                 for index, (text, continuation) in enumerate(chunks, 1)]
        return (parts, record_parts) if with_record_parts else parts
