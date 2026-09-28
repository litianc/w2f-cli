import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.install_reader_sync import install


class ReaderInstallTest(unittest.TestCase):
    def test_idempotent_upgrade_and_rollback_preserve_reader(self):
        import scripts.install_reader_sync as module
        original='''def main():
    sub = parser.add_subparsers(dest="command", required=True)
    with db:
        if True:
            if command == "status":
                pass
'''
        with tempfile.TemporaryDirectory() as directory:
            engine=Path(directory)/'engine.py';engine.write_text(original)
            writer=module._atomic_write
            def fail_engine(path,content):
                if path==engine:raise OSError('injected install failure')
                return writer(path,content)
            with patch.object(module,'_atomic_write',side_effect=fail_engine):
                with self.assertRaises(OSError):install(engine)
            self.assertEqual(engine.read_text(),original)
            self.assertFalse((engine.parent/'reader_sync.py').exists())
            install(engine)
            self.assertIn('from reader_voice import read_voice',engine.read_text())
            self.assertIn('from reader_sync import watch_changes',engine.read_text())
            before={p.name:p.read_bytes() for p in engine.parent.glob('*.py')}
            backups=list((engine.parent/'w2f-extension-backups').iterdir())
            install(engine)
            self.assertEqual(before,{p.name:p.read_bytes() for p in engine.parent.glob('*.py')})
            self.assertEqual(backups,list((engine.parent/'w2f-extension-backups').iterdir()))

if __name__=='__main__':unittest.main()
