"""Check current publishable files, without printing matched private values.

This is a current-tree check; the Git history is cleaned separately before a
public push.
"""
import hashlib
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
# User-selected README media, reviewed as rendered output. A replacement must
# be reviewed again; never exempt an entire directory or media extension.
REVIEWED_MEDIA={
    'docs/media/w2f-cli-preview.gif': '627fe57baba84a3fad1d49e32006abc0bb2ef51a0b5225bf2332a8206b36ff42',
    'docs/media/w2f-cli-preview.mp4': '97024b59a55f425fe85619c4dd82fa8d3697ca909a835cdd67bef71b14052bc6',
    'docs/media/wechat-sanitized.png': '5d035418d1c91abc09524f75bade2f2659ff4437a59b35995f64b9c310a3fa92',
    'docs/media/feishu-sanitized.png': '4bb5800ec1190046d58c8c7812a7da47f49db3f35d5a22495a980e80973d3067',
}
PATTERNS=[re.compile(r'(?<![A-Za-z0-9_])(?:cli_[a-f0-9]{16}|(?:oc|ou)_[a-f0-9]{24,}|\d{8,}@chatroom)(?![A-Za-z0-9_])'),
          re.compile(r'/Users/[A-Za-z0-9_.-]+/'),
          re.compile(r'(?i)(?:app_secret|access_token|refresh_token)["\x27]?\s*[:=]\s*["\x27][A-Za-z0-9_-]{24,}["\x27]')]


def main():
    files=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT).decode().split('\0')
    failures=[]
    for name in set(files):
        path=ROOT/name
        if not name or not path.is_file():continue
        if name in REVIEWED_MEDIA:
            if hashlib.sha256(path.read_bytes()).hexdigest()!=REVIEWED_MEDIA[name]:
                failures.append((name,'documentation media changed; review required'))
            continue
        if path.suffix.lower() in {'.sqlite3','.db','.log','.bak','.png','.jpg','.gif','.mp4'}:
            failures.append((name,'runtime artifact'));continue
        content=path.read_text(errors='replace')
        for line,text in enumerate(content.splitlines(),1):
            if any(pattern.search(text) for pattern in PATTERNS):failures.append((name,f'private identifier at line {line}'))
    for name,reason in failures:print(f'{name}: {reason}')
    print('Current-tree privacy check: '+('FAIL' if failures else 'PASS'))
    return bool(failures)


if __name__=='__main__':raise SystemExit(main())
