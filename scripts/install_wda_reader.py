"""Generate the Windows shim that exposes integrations/wda_reader.py as a CLI.

w2f launches its reader as a plain executable (WECHAT_READER); Windows cannot
exec extensionless scripts, so the adapter is wrapped in a .cmd that calls the
current interpreter by absolute path. Run this once after creating the venv:

    python scripts/install_wda_reader.py

Then set WECHAT_READER, or add "wechat_reader" to the runtime config:

    "wechat_reader": "C:\\...\\integrations\\wda_reader.cmd"
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "integrations" / "wda_reader.cmd"
PAYLOAD = SHIM.with_suffix(".py")

if not PAYLOAD.exists():
    sys.exit(f"找不到适配器：{PAYLOAD}")
if sys.platform != "win32":
    sys.exit("此脚本仅用于 Windows；macOS 直接把 WECHAT_READER 指向读取器自身。")

python = Path(sys.executable).resolve()
SHIM.write_text(f'@"{python}" "%~dp0wda_reader.py" %*\r\n', encoding="ascii")
print(f"已生成 {SHIM}")
print('在运行配置中加 "wechat_reader": ' + json.dumps(str(SHIM)))
