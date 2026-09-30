"""Windows 底层读取的密钥来源：WDA 捕获并按账号保存的解密密钥。

WDA（微信数据库解密工具）以临时微信窗口捕获密钥并校验后写入
`output/account_keys.json`：db_key（SQLCipher 32 字节，hex 字符串）、
image_aes_key（V2 图片 AES-128）、image_xor_key（dat 头 XOR 字节）。
本模块只做读取与归一化；密钥内容绝不写日志或异常文本。
"""
import json
import os
from pathlib import Path

DEFAULT_KEYS_PATH = Path(os.environ.get(
    'W2F_WDA_KEYS',
    str(Path.home() / 'AppData' / 'Roaming' / 'wechat-data-analysis-desktop' / 'output' / 'account_keys.json')))


class WinKeysError(RuntimeError):
    pass


def default_keys_path() -> Path:
    """当前生效的密钥文件路径（W2F_WDA_KEYS 可覆盖，供测试与沙箱）。"""
    if override := os.environ.get('W2F_WDA_KEYS'):
        return Path(override)
    return Path.home() / 'AppData' / 'Roaming' / 'wechat-data-analysis-desktop' / 'output' / 'account_keys.json'


def load_keys(path: Path | None = None) -> dict:
    path = path or default_keys_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise WinKeysError(f'密钥文件不存在：{path}（需在 WDA 中完成一次账号检测）') from None
    except ValueError:
        raise WinKeysError(f'密钥文件损坏（非 JSON）：{path}') from None
    if not isinstance(data, dict) or not data:
        raise WinKeysError(f'密钥文件为空：{path}')
    return data


def _match_account(accounts: dict, account: str) -> dict:
    if account in accounts:
        return accounts[account]
    for name, entry in accounts.items():
        # 微信 4.x 目录带 4 位十六进制后缀（wxid_x_290e）；按前缀归并同一账号。
        if name == account or name.startswith(account + '_'):
            return entry
    raise WinKeysError(f'密钥文件中没有账号 {account} 的条目（现有：{len(accounts)} 个账号）')


def keys_for(account: str, path: Path | None = None) -> dict:
    """归一化后的账号密钥：db_key(32B) / image_aes_key(str) / image_xor_key(int)。"""
    entry = _match_account(load_keys(path), str(account or '').strip())
    hex_key = str(entry.get('db_key') or '')
    try:
        db_key = bytes.fromhex(hex_key)
    except ValueError:
        raise WinKeysError(f'账号 {account} 的 db_key 不是合法 hex，无法用于 SQLCipher') from None
    if len(db_key) != 32:
        raise WinKeysError(f'账号 {account} 的 db_key 长度异常（{len(db_key)} 字节，应为 32）')
    if not entry.get('image_aes_key') or entry.get('image_xor_key') in (None, ''):
        raise WinKeysError(f'账号 {account} 缺少图片密钥（image_aes_key/image_xor_key），表情与图片解密不可用')
    try:
        xor_key = int(str(entry['image_xor_key']), 16)
    except ValueError:
        raise WinKeysError(f'账号 {account} 的 image_xor_key 不是合法十六进制') from None
    return {'db_key': db_key, 'image_aes_key': str(entry['image_aes_key']), 'image_xor_key': xor_key}
