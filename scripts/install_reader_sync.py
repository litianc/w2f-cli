"""Install the versioned read-only sync-page extension into an existing reader.

Usage: python scripts/install_reader_sync.py /path/to/rion_wechat_reader.py
The patch is narrow, idempotent and keeps a backup. No WeChat data is modified.
"""
import argparse
import shutil
from pathlib import Path


def install(engine):
    engine = Path(engine).expanduser()
    text = engine.read_text(encoding="utf-8")
    replacements = {
        '    sub = parser.add_subparsers(dest="command", required=True)':
        '    sub = parser.add_subparsers(dest="command", required=True)\n    from reader_sync import register\n    register(sub)',
        '            if command == "status":':
        '            if command == "sync-page":\n                from reader_sync import read_page\n                emit(command, read_page(sys.modules[__name__], db, args), args.pretty)\n            elif command == "status":',
    }
    if 'from reader_sync import read_page' not in text:
        for old, new in replacements.items():
            if text.count(old) != 1:
                raise RuntimeError('Reader layout changed; review the patch before installing')
            text = text.replace(old, new)
        compile(text, str(engine), 'exec')
        backup = engine.with_suffix('.py.before-sync')
        if not backup.exists():
            shutil.copy2(engine, backup)
        engine.write_text(text)
    for name in ('reader_sync.py', 'forward_records.py'):
        shutil.copy2(Path(__file__).resolve().parents[1] / 'integrations' / name, engine.parent / name)


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('engine'); install(p.parse_args().engine)
