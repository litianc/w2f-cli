"""Install versioned read-only extensions with backups and rollback.

Usage: python scripts/install_reader_sync.py /path/to/rion_wechat_reader.py
Stop reader consumers before installation. No WeChat data is modified.
"""
import argparse
import os
import shutil
import tempfile
import time
from pathlib import Path


def _atomic_write(path, content):
    fd, name = tempfile.mkstemp(prefix='.w2f-install-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(content)
        if path.exists():shutil.copymode(path,name)
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def install(engine):
    engine = Path(engine).expanduser()
    text = engine.read_text(encoding='utf-8')
    if 'from reader_sync import read_page' not in text:
        replacements = {
            '    sub = parser.add_subparsers(dest="command", required=True)':
            '    sub = parser.add_subparsers(dest="command", required=True)\n    from reader_sync import register\n    register(sub)',
            '            if command == "status":':
            '            if command == "sync-page":\n                from reader_sync import read_page\n                emit(command, read_page(sys.modules[__name__], db, args), args.pretty)\n            elif command == "status":',
        }
        for old,new in replacements.items():
            if text.count(old)!=1:raise RuntimeError('Reader layout changed; review before installing')
            text=text.replace(old,new)
    extensions = {
        'from reader_voice import read_voice':('voice-resource',
            '                from reader_voice import read_voice\n'
            '                emit(command, read_voice(sys.modules[__name__], db, args), args.pretty)\n'),
        'from reader_sync import watch_changes':('watch-changes',
            '                from reader_sync import watch_changes\n'
            '                watch_changes(db,args)\n'),
    }
    for marker,(command,body) in extensions.items():
        if marker not in text:
            old='            elif command == "status":'
            if text.count(old)!=1:raise RuntimeError('Reader dispatch changed; review before installing')
            text=text.replace(old,f'            elif command == "{command}":\n'+body+old)
    compile(text,str(engine),'exec')
    sources=Path(__file__).resolve().parents[1]/'integrations'
    changes={engine.parent/name:(sources/name).read_bytes() for name in ('reader_sync.py','forward_records.py','reader_voice.py')}
    changes[engine]=text.encode('utf-8')
    changes={path:data for path,data in changes.items() if not path.exists() or path.read_bytes()!=data}
    if not changes:return
    for path,data in changes.items():compile(data,str(path),'exec')
    backup=engine.parent/'w2f-extension-backups'/str(time.time_ns());backup.mkdir(parents=True,mode=0o700)
    existed={path:path.exists() for path in changes}
    for path in changes:
        if existed[path]:shutil.copy2(path,backup/path.name)
    installed=[]
    try:
        for path,data in changes.items():
            _atomic_write(path,data);installed.append(path)
    except Exception:
        for path in reversed(installed):
            if existed[path]:shutil.copy2(backup/path.name,path)
            else:path.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('engine');install(p.parse_args().engine)
