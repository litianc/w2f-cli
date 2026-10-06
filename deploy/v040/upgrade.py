"""Install versioned release infrastructure while preserving the running image and data."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

try:
    from .release import ReleaseManager, atomic_write
except ImportError:
    from release import ReleaseManager, atomic_write


def install(root, source, entrypoint=Path('/usr/local/sbin/w2f-v040-release')):
    root,source=Path(root).resolve(),Path(source).resolve()
    entrypoint=Path(entrypoint).resolve()
    if not entrypoint.is_file():
        raise ValueError('Expected the installed restricted release entrypoint')
    manager=ReleaseManager(root)
    if not (root/'.w2f-managed').is_file():
        raise ValueError('Expected an existing managed v0.4.0 deployment')
    config=manager.config()
    if config.get('W2F_ENVIRONMENT') not in {'v040-prototype','v040-ci'} or not config.get('W2F_PROJECT','').startswith('w2f-v040-'):
        raise ValueError('Refusing unrelated environment')
    with (root/'.release.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current=json.loads((manager.state/'releases.json').read_text())['current']
        backup=manager.state/'topology-backups'/str(time.time_ns())
        backup.mkdir(parents=True,mode=0o700)
        backup.parent.chmod(0o700)
        names=('compose.yaml','release.py')
        for name in names:
            shutil.copy2(root/name,backup/name)
        shutil.copy2(entrypoint,backup/'restricted-release.py')
        for name in ('compose.env','releases.json'):
            shutil.copy2(manager.state/name,backup/name)
        manager.backup_database()
        secret=manager.state/'secrets/feishu_apps.json'
        if not secret.exists():
            with os.fdopen(os.open(secret,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o444),'w') as stream:
                stream.write('{}\n')
            secret.chmod(0o444)
        try:
            for name in names:
                atomic_write(root/name,(source/name).read_text())
            atomic_write(entrypoint,(source/'release.py').read_text())
            entrypoint.chmod(0o755)
            manager.compose('config','--quiet')
            manager.verify(current['revision'],current['id'])
        except Exception:
            for name in names:
                atomic_write(root/name,(backup/name).read_text())
            atomic_write(entrypoint,(backup/'restricted-release.py').read_text())
            entrypoint.chmod((backup/'restricted-release.py').stat().st_mode & 0o777)
            raise RuntimeError('Infrastructure upgrade failed; original configuration restored') from None
        result=dict(installed=True,backup=str(backup),running_release=current['revision'],
            files={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names})
        result['files']['restricted-release.py']=hashlib.sha256(entrypoint.read_bytes()).hexdigest()
        atomic_write(backup/'result.json',json.dumps(result)+'\n')
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='/opt/w2f-v040')
    parser.add_argument('--source',default=str(Path(__file__).resolve().parent))
    args=parser.parse_args()
    print(json.dumps(install(args.root,args.source)))
