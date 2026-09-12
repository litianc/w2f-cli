"""Check current publishable files, without printing matched private values.

Git history is intentionally not rewritten or claimed clean by this script.
"""
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PATTERNS=[re.compile(r'(?<![A-Za-z0-9_])(?:cli_[a-f0-9]{16}|(?:oc|ou)_[a-f0-9]{24,}|\d{8,}@chatroom)(?![A-Za-z0-9_])'),
          re.compile(r'/Users/[A-Za-z0-9_.-]+/'),
          re.compile(r'(?i)(?:app_secret|access_token|refresh_token)["\x27]?\s*[:=]\s*["\x27][A-Za-z0-9_-]{24,}["\x27]')]


def main():
    files=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT).decode().split('\0')
    failures=[]
    for name in set(files):
        path=ROOT/name
        if not name or not path.is_file():continue
        if path.suffix.lower() in {'.sqlite3','.db','.log','.bak','.png','.jpg','.gif','.mp4'}:
            failures.append((name,'runtime artifact'));continue
        content=path.read_text(errors='replace')
        for line,text in enumerate(content.splitlines(),1):
            if any(pattern.search(text) for pattern in PATTERNS):failures.append((name,f'private identifier at line {line}'))
    for name,reason in failures:print(f'{name}: {reason}')
    print('Current-tree privacy check: '+('FAIL' if failures else 'PASS'))
    return bool(failures)


if __name__=='__main__':raise SystemExit(main())
