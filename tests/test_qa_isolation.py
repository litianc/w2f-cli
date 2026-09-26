"""Exercise guards in disposable child interpreters, never in the test runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qa.isolation import ProcessTree, _first_argument, child_env


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='w2f-isolation-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {**os.environ, **child_env(self.root)}

    def run_child(self, code):
        result = subprocess.run([sys.executable, '-c', code], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_environment_preserves_home_and_moves_all_fixture_roots(self):
        self.assertEqual(self.env.get('HOME'), os.environ.get('HOME'))
        self.assertEqual(self.env.get('CODEX_HOME'), os.environ.get('CODEX_HOME'))
        for key in ('TMPDIR', 'TMP', 'TEMP', 'W2F_WDA_CACHE', 'WECHAT_CACHE_DIRS', 'W2F_DATA_DIR'):
            self.assertTrue(Path(self.env[key]).is_relative_to(self.root.resolve()))
        self.assertEqual(self.env['WDA_BASE'], 'http://127.0.0.1:0')
        result = self.run_child('import json,os,tempfile; print(json.dumps([os.environ.get("W2F_QA_GUARD_ACTIVE"),tempfile.gettempdir()]))')
        self.assertEqual(result, ['1', str(self.root.resolve() / 'tmp')])

    def test_guard_rejects_remote_network_cli_and_real_state(self):
        result = self.run_child('''
import json,os,socket,subprocess,sys
from pathlib import Path
rejected=[]
actions=[lambda:socket.getaddrinfo('example.test',443),
         lambda:subprocess.run(['lark-cli','--version']),
         lambda:Path.home().joinpath('.local/share/w2f-cli/state.db').read_bytes()]
for action in actions:
    try: action()
    except PermissionError: rejected.append(True)
    else: rejected.append(False)
server=socket.socket();server.bind(('127.0.0.1',0));server.close()
child=subprocess.run([sys.executable,'-c','import os; print(os.environ.get("W2F_QA_GUARD_ACTIVE"))'],capture_output=True,text=True)
print(json.dumps([rejected,child.returncode,child.stdout.strip()]))
''')
        self.assertEqual(result, [[True, True, True], 0, '1'])

    def test_fixture_cli_inside_qa_root_is_allowed(self):
        executable = self.root / ('fake-cli.cmd' if os.name == 'nt' else 'fake-cli')
        if os.name == 'nt':
            executable.write_bytes(b'@echo off\r\necho fixture-ok\r\n')
        else:
            executable.write_text('#!/bin/sh\nprintf fixture-ok', encoding='utf-8')
            executable.chmod(0o755)
        self.env['QA_FIXTURE_EXECUTABLE'] = str(executable)
        result = self.run_child('import json,os,subprocess; from pathlib import Path; '
                                'p=subprocess.run([os.environ["QA_FIXTURE_EXECUTABLE"]],capture_output=True,text=True); '
                                'print(json.dumps([p.returncode,p.stdout.strip()]))')
        self.assertEqual(result, [0, 'fixture-ok'])

    def test_windows_executable_parse_ignores_python_code_quotes(self):
        # Exercise Windows audit-event strings on every CI platform. The code
        # argument deliberately contains quotes that are not shell-balanced.
        code = "import json\nvalue = \"can't parse this as shell\"\nprint(value)"
        for executable in (r'C:\Python\python.exe', r'C:\Program Files\Python\python.exe',
                           r'C:\QA fixtures\fake-cli.cmd'):
            command = subprocess.list2cmdline([executable, '-c', code])
            self.assertEqual(_first_argument(command, windows=True), executable)
        self.assertEqual(_first_argument('python.exe -c "unterminated trailing code', windows=True), 'python.exe')

    def test_windows_executable_parse_rejects_ambiguous_path(self):
        for command in ('', '"unterminated path', '"" -c test', '"C:\\python.exe"suffix -c test',
                        'C:\\bad"path.exe -c test'):
            with self.assertRaises(PermissionError):
                _first_argument(command, windows=True)

    def test_cleanup_finds_detached_python_after_original_parent_exits(self):
        import psutil
        # The detached child registers itself through inherited sitecustomize;
        # its original parent exits too quickly for reliable tree polling.
        code = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)'
        process = subprocess.Popen([sys.executable, '-c', code], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tracker = ProcessTree(process.pid, self.root, interval=.2)
        try:
            process.wait(timeout=10)
            import time
            deadline = time.monotonic() + 10
            records = []
            while time.monotonic() < deadline:
                records = [json.loads(p.read_text()) for p in (self.root / '.processes').glob('*.json')]
                if len(records) >= 2:
                    break
                time.sleep(.02)
            self.assertGreaterEqual(len(records), 2)
            children = [r for r in records if r['pid'] != process.pid]
            self.assertTrue(children)
            tracker.cleanup()
            for child in children:
                try:
                    live = psutil.Process(child['pid'])
                    self.assertTrue(live.create_time() != child['created'] or live.status() == psutil.STATUS_ZOMBIE)
                except psutil.NoSuchProcess:
                    pass
        finally:
            tracker.cleanup()

    def test_cleanup_rejects_stale_pid_record(self):
        import psutil
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            entry = {'pid': unrelated.pid, 'created': psutil.Process(unrelated.pid).create_time() - 100}
            (self.root / '.processes' / 'stale.json').write_text(json.dumps(entry), encoding='utf-8')
            # A nonexistent PID cannot accidentally add the unrelated process.
            tracker = ProcessTree(2_000_000_000, self.root)
            tracker.cleanup()
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
