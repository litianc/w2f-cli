"""Local image compatibility. No secrets are persisted or returned in diagnostics.

V1/V2 framing reference (independent implementation):
https://github.com/jackwener/wx-cli-again/blob/main/src/attachment/decoder/v2.rs
macOS filename derivation is opt-in and scoped to the source file's account.
"""
from __future__ import annotations

import hashlib
import re
import struct
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

V1 = b'\x07\x08V1\x08\x07'
V2 = b'\x07\x08V2\x08\x07'
MAX_BYTES = 100 * 1024 * 1024


class ImageResourceError(ValueError):
    pass


def verified_image(data: bytes) -> bool:
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
        return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def _macos_candidates(source: Path):
    """Read only known kvcomm filename layouts; never scan processes or keys files."""
    if sys.platform != 'darwin':
        return
    source = source.resolve()
    account = next((p for p in source.parents if p.parent.name == 'xwechat_files'), None)
    if account is None:
        return
    documents = account.parent.parent
    names = [account.name]
    normalized = re.sub(r'_[0-9a-fA-F]{4}$', '', account.name)
    if normalized != account.name:
        names.append(normalized)
    roots = [documents/'app_data'/family/'kvcomm' for family in ('net', 'ilink')]
    roots += [documents/'xwechat/net/kvcomm',
              documents.parent/'Library/Application Support/com.tencent.xinWeChat/xwechat/net/kvcomm',
              documents.parent/'Library/Application Support/com.tencent.xinWeChat/net/kvcomm']
    seen = set()
    for root in roots:
        try:
            files = list(root.glob('key_*.statistic'))[:256]
        except OSError:
            continue
        for file in files:
            match = re.fullmatch(r'key_(\d+)_.+\.statistic', file.name)
            if not match:
                continue
            code = int(match.group(1))
            for name in names:
                key = hashlib.md5(f'{code}{name}'.encode()).hexdigest()[:16].encode('ascii')
                candidate = (key, code & 255)
                if candidate not in seen:
                    seen.add(candidate)
                    yield candidate


def decode_dat(source: Path, *, keys=(), allow_macos_derivation=False) -> bytes:
    if source.stat().st_size > MAX_BYTES:
        raise ImageResourceError('resource_too_large: 图片超过 100 MB')
    data = source.read_bytes()
    if verified_image(data):
        return data
    if data[:6] in (V1, V2):
        if len(data) < 15:
            raise ImageResourceError('invalid_image_header: 图片文件头不完整')
        head, tail = struct.unpack('<II', data[6:14])
        end = 15 + head + (16 - head % 16)
        raw_end = len(data) - tail
        if end > raw_end or end > len(data) or head == 0:
            raise ImageResourceError('invalid_image_header: 图片分段长度无效')
        candidates = list(keys)
        if data[:6] == V1:
            candidates.append((b'cfcd208495d565ef', 0x88))
        elif allow_macos_derivation:
            candidates.extend(_macos_candidates(source))
        if not candidates:
            raise ImageResourceError('image_key_missing: V2 图片需要已授权的本机解码参数')
        for key, xor in candidates:
            if len(key) != 16 or not 0 <= xor <= 255:
                continue
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            first = decryptor.update(data[15:end]) + decryptor.finalize()
            pad = 16 - head % 16
            if first[head:] != bytes([pad]) * pad:
                continue
            decoded = first[:head] + data[end:raw_end] + bytes(x ^ xor for x in data[raw_end:])
            if verified_image(decoded):
                return decoded
        raise ImageResourceError('decode_failed: V2 图片解码或完整性校验失败')
    signatures = (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF8', b'RIFF')
    for signature in signatures:
        if not data:
            break
        key = data[0] ^ signature[0]
        if bytes(x ^ key for x in data[:len(signature)]) != signature:
            continue
        decoded = bytes(x ^ key for x in data)
        if verified_image(decoded):
            return decoded
    raise ImageResourceError('decode_failed: 不支持或损坏的本机图片资源')
