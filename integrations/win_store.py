"""Windows 自研读取层：以 WDA 保存的密钥直读微信 4.x 加密库。

链路（全部离线证据齐备，见 issue #35）：
- db_key 是主密钥：真实页密钥 = PBKDF2-HMAC-SHA512(master, 页1盐, 256000)，
  以 raw 形式交给 SQLCipher（mac_key 派生 iter=2，SQLCipher 4 标准行为）。
- 消息列（message_content/source/compress_content）为 zstd 压缩，需解压。
- 表情 XML 不总带 cdnurl（自己发送的副本没有）；同一表情在别的会话收到的
  副本通常带。跨会话扫描取第一个带 cdnurl 的副本，下载后按 XML 声明的
  md5/len 做严格校验——与转发表情的身份契约一致。
"""
import hashlib
import hmac
import os
import re
import urllib.request

from integrations.win_keys import WinKeysError, load_keys, keys_for

try:
    import sqlcipher3
except ImportError:  # 非 Windows 或未安装绑定：读取层整体不可用，调用方降级。
    sqlcipher3 = None

try:
    import zstandard
except ImportError:
    zstandard = None

ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'
EMOJI_MAX_BYTES = 20 * 1024 * 1024


class WinStoreError(RuntimeError):
    pass


def available() -> bool:
    return sqlcipher3 is not None and zstandard is not None and os.name == 'nt'


def default_account() -> str | None:
    """单账号机器的约定：取密钥文件第一个账号；多账号用 W2F_WX_ACCOUNT 指定。"""
    if override := os.environ.get('W2F_WX_ACCOUNT'):
        return override
    accounts = sorted(load_keys())
    return accounts[0] if accounts else None


def wxid_dir(account: str | None = None) -> str:
    """账号的 xwechat_files 目录（account_keys.json 里 WDA 已记录）。"""
    entry = _account_entry(account)
    wxid_dir_path = entry.get('db_key_source_wxid_dir')
    if not wxid_dir_path or not os.path.isdir(wxid_dir_path):
        raise WinStoreError(f'微信数据目录不可用（{wxid_dir_path or "未记录"}）')
    return wxid_dir_path


def _account_entry(account):
    account = account or default_account()
    if not account:
        raise WinStoreError('密钥文件中没有账号条目')
    accounts = load_keys()
    if account in accounts:
        return accounts[account]
    for name, entry in accounts.items():
        if name.startswith(account + '_'):
            return entry
    raise WinStoreError(f'密钥文件中没有账号 {account} 的条目')


def _page_key_for(master: bytes, page1: bytes) -> bytes:
    mac_salt = bytes((b ^ 0x3A) for b in page1[:16])
    mac_key = hashlib.pbkdf2_hmac('sha512', master, mac_salt, 2, dklen=32)
    payload = page1[16:4096 - 64] + (1).to_bytes(4, 'little')
    if hmac.compare_digest(hmac.new(mac_key, payload, hashlib.sha512).digest(), page1[4096 - 64:]):
        return master
    return hashlib.pbkdf2_hmac('sha512', master, page1[:16], 256000, dklen=32)


def connect_db(db_path: str, db_key: bytes):
    """打开一个 4.x 加密库；页密钥按该库自己的盐派生并缓存于调用方。"""
    if sqlcipher3 is None:
        raise WinStoreError('sqlcipher3 绑定未安装（pip install sqlcipher3-wheels）')
    page1 = open(db_path, 'rb').read(4096)
    if len(page1) < 4096:
        raise WinStoreError(f'库文件过小：{db_path}')
    page_key = _page_key_for(db_key, page1)
    con = sqlcipher3.connect(db_path)
    try:
        con.execute(f"PRAGMA key = \"x'{page_key.hex()}'\";")
        con.execute('SELECT count(*) FROM sqlite_master')  # 触发一次真实读，失败即刻暴露
    except Exception as exc:
        con.close()
        raise WinStoreError(f'数据库打开失败（密钥与该库不匹配）：{os.path.basename(db_path)}') from exc
    return con


def message_db_path(account: str | None = None) -> str:
    return os.path.join(wxid_dir(account), 'db_storage', 'message', 'message_0.db')


def _decompress(blob) -> str | None:
    if not isinstance(blob, bytes) or not blob:
        return None
    if blob[:4] == ZSTD_MAGIC:
        try:
            return zstandard.ZstdDecompressor().decompress(blob).decode('utf-8', 'replace')
        except Exception:
            return None
    return blob.decode('utf-8', 'replace')


_EMOJI_TAG = re.compile(r'<emoji [^>]*>')


def emoji_occurrences(con, emoji_md5: str):
    """跨会话扫描同一表情的所有出现；产出 (local_id, attrs)（attrs 含 cdnurl 时优先）。"""
    md5_lower = emoji_md5.lower()
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'").fetchall():
        try:
            rows = con.execute(f'SELECT local_id, message_content FROM "{table}"').fetchall()
        except Exception:
            continue
        for local_id, content in rows:
            xml = _decompress(content)
            if not xml or md5_lower not in xml:
                continue
            tag = _EMOJI_TAG.search(xml)
            if not tag:
                continue
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag.group(0)))
            if attrs.get('md5', '').lower() == md5_lower:
                yield int(local_id), attrs


def emoji_download(account: str | None, emoji_md5: str, db_key: bytes | None = None) -> bytes:
    """按消息 XML 声明的 cdnurl 下载表情并严格校验 md5 与声明长度。"""
    md5_lower = emoji_md5.lower()
    con = connect_db(message_db_path(account), db_key or keys_for(account)['db_key'])
    fallback = None
    try:
        for local_id, attrs in emoji_occurrences(con, emoji_md5):
            url = (attrs.get('cdnurl') or '').replace('&amp;', '&')
            if not url.startswith(('http://', 'https://')):
                fallback = fallback or attrs
                continue
            request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(EMOJI_MAX_BYTES + 1)
            if len(data) > EMOJI_MAX_BYTES:
                continue
            if hashlib.md5(data).hexdigest() != md5_lower:
                continue
            declared = int(attrs.get('len') or 0)
            if declared and len(data) != declared:
                continue
            return data
    finally:
        con.close()
    raise WinStoreError('emoji_unresolvable: 本地微信库中没有该表情的可下载副本')
