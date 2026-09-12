"""Runtime paths shared by the manager and mirror workers.

The repository contains only examples and code.  Mutable state is kept in a
user data directory so a checkout can be replaced or published safely.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("CHZ_DATA_DIR", "~/.local/share/chz-cli")).expanduser()
CONFIG_DIR = Path(os.environ.get("CHZ_CONFIG_DIR", "~/.config/chz-cli")).expanduser()
DEFAULT_CONFIG = CONFIG_DIR / "wechat_feishu_mirror.json"
EXAMPLE_CONFIG = ROOT / "config" / "wechat_feishu_mirror.json"


def config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG


def data_path(name: str) -> Path:
    return DATA_DIR / name


def runtime_paths(config: dict) -> dict:
    """Fill mutable paths without rewriting an explicit user configuration."""
    result = dict(config)
    result.setdefault("data_dir", str(DATA_DIR))
    data_dir = Path(result["data_dir"]).expanduser()
    for key, fallback in {
        "sqlite_file": data_dir / "wechat_feishu_mirror.sqlite3",
        "state_file": data_dir / "state" / "wechat_feishu_mirror.json",
        "media_dir": data_dir / "media",
        "log_dir": data_dir / "logs",
    }.items():
        value = result.get(key)
        # Relative paths from the v0.1/v0.2 repository config are mutable
        # state and must move out of the checkout.
        if value and not Path(str(value)).is_absolute():
            relative = Path(str(value))
            # v0.1/v0.2 stored the database under repository/state.  Keep the
            # state JSON namespaced, but place the SQLite file at the data root.
            if key == "sqlite_file" and relative.parts and relative.parts[0] == "state":
                relative = Path(relative.name)
            result[key] = str((data_dir / relative).resolve())
        else:
            result[key] = str(value or fallback)
    return result


def is_initialized(config: dict, db_path: Path) -> bool:
    required = ("wechat_talker", "feishu_chat_id", "lark_profile")
    return all(str(config.get(key) or "").strip() for key in required) and db_path.exists()
