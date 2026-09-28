"""Accidental-I/O guards and owned-process cleanup for trusted QA code.

This is not an OS sandbox. Python audit hooks guard Python descendants loaded
through sitecustomize; native tools, browser engines, malicious code, Python -S,
and intentionally stripped environments require a container/OS policy. The
allowlisted Playwright driver is trusted to operate only on fixture pages.

Parent API: merge child_env(root) into the child's environment, then wrap its
Popen PID in ``with ProcessTree(pid, root): ...``. The context cleans up only
observed descendants or bootstrap-registered processes with matching creation
times, including Python workers that detach into another process session.
HOME and CODEX_HOME are neither overwritten nor repurposed.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
_INSTALLED = False


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _real_state_paths():
    home = Path.home()
    paths = [home / relative for relative in (
        '.local/share/w2f-cli', '.config/w2f-cli', '.config/lark-cli', '.lark-cli',
        '.config/lark', '.local/share/lark-cli', '.config/rion-wechat-cli',
        '.local/share/rion-wechat-cli', '.local/share/wechat-cli',
        'Library/Containers/com.tencent.xinWeChat',
        'Library/Application Support/com.tencent.xinWeChat',
        'Library/Application Support/WeChat', 'Documents/WeChat Files',
        'Documents/xwechat_files',
    )]
    # WeChat uses team-prefixed group-container names on macOS.
    groups = home / 'Library' / 'Group Containers'
    if groups.is_dir():
        paths.extend(p for p in groups.iterdir() if 'wechat' in p.name.lower())
    for variable in ('APPDATA', 'LOCALAPPDATA'):
        if os.environ.get(variable):
            base = Path(os.environ[variable])
            paths.extend((base / 'Tencent' / 'WeChat', base / 'Tencent' / 'xwechat', base / 'lark-cli'))
    for variable in ('W2F_DATA_DIR', 'W2F_CONFIG_DIR', 'CHZ_DATA_DIR', 'CHZ_CONFIG_DIR', 'W2F_WDA_CACHE'):
        if os.environ.get(variable):
            paths.append(Path(os.environ[variable]).expanduser())
    paths.extend(Path(value).expanduser() for value in os.environ.get('WECHAT_CACHE_DIRS', '').split(os.pathsep) if value)
    return [str(path.resolve()) for path in paths]


def child_env(root: Path) -> dict[str, str]:
    """Create bootstrap files and return environment overrides for one QA run."""
    root = root.resolve()
    bootstrap = root / 'bootstrap'
    for directory in ('bootstrap', 'tmp', 'data', 'config', 'media', 'wda-cache', 'wechat-cache', '.processes'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    bootstrap.joinpath('sitecustomize.py').write_text(
        "import os\n"
        "try:\n"
        "    from qa.isolation import install_child_guard\n"
        "    install_child_guard()\n"
        "except BaseException:\n"
        "    os.write(2, b'QA isolation bootstrap failed\\n')\n"
        "    os._exit(78)\n", encoding='utf-8')
    # Use the same interpreter installation for /usr/bin/env python3 fixtures.
    python_bin = str(Path(sys.executable).parent)
    result = {
        'W2F_QA_ROOT': str(root), 'W2F_QA_DENY_PATHS': json.dumps(_real_state_paths()),
        'W2F_QA_ISOLATED': '1', 'PYTHONPATH': os.pathsep.join((str(bootstrap), str(ROOT))),
        'PATH': python_bin + os.pathsep + os.environ.get('PATH', ''),
        'TMPDIR': str(root / 'tmp'), 'TEMP': str(root / 'tmp'), 'TMP': str(root / 'tmp'),
        'W2F_DATA_DIR': str(root / 'data'), 'CHZ_DATA_DIR': str(root / 'data'),
        'W2F_CONFIG_DIR': str(root / 'config'), 'CHZ_CONFIG_DIR': str(root / 'config'),
        'W2F_MEDIA_DIR': str(root / 'media'), 'CHZ_MEDIA_DIR': str(root / 'media'),
        'W2F_WDA_CACHE': str(root / 'wda-cache'), 'WECHAT_CACHE_DIRS': str(root / 'wechat-cache'),
        'WECHAT_READER': str(root / 'disabled-real-reader'),
        # Port zero cannot target the user's normal running WDA service.
        'WDA_BASE': 'http://127.0.0.1:0',
        'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
    }
    return result


def _allowed_executables():
    paths = {Path(sys.executable).resolve()}
    if getattr(sys, '_base_executable', None):
        paths.add(Path(sys._base_executable).resolve())
    spec = importlib.util.find_spec('playwright')
    if spec and spec.origin:
        driver = Path(spec.origin).parent / 'driver'
        paths.add((driver / ('node.exe' if os.name == 'nt' else 'node')).resolve())
    return paths


def _first_argument(command: str, *, windows: bool) -> str:
    """Read only argv[0], leaving quoted script/code arguments uninterpreted.

    Windows subprocess audit events contain the CreateProcess command line,
    even when Popen received a list. shlex is not a Windows command-line parser:
    parsing the entire string can reject valid Python -c code after argv[0].
    Windows executable filenames cannot contain a quote, so accept one fully
    quoted path or one unquoted word and fail closed on ambiguous forms.
    """
    command = command.lstrip()
    if not command:
        raise PermissionError('QA cannot identify the child executable')
    if windows:
        if command.startswith('"'):
            end = command.find('"', 1)
            if end <= 1 or (end + 1 < len(command) and not command[end + 1].isspace()):
                raise PermissionError('QA cannot identify the child executable')
            return command[1:end]
        first = command.split(None, 1)[0]
        if '"' in first:
            raise PermissionError('QA cannot identify the child executable')
        return first
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    try:
        return next(lexer)
    except (StopIteration, ValueError):
        raise PermissionError('QA cannot identify the child executable') from None


def _command_path(executable, args, cwd, env):
    if not executable:
        if isinstance(args, (list, tuple)) and args:
            executable = args[0]
        elif isinstance(args, (str, bytes)):
            executable = _first_argument(os.fsdecode(args), windows=os.name == 'nt')
    value = os.fsdecode(executable) if executable else ''
    if not value:
        raise PermissionError('QA cannot identify the child executable')
    candidate = Path(value)
    if not candidate.is_absolute() and candidate.parent == Path('.'):
        resolved = shutil.which(value, path=(env or os.environ).get('PATH', ''))
        if resolved:
            candidate = Path(resolved)
    if not candidate.is_absolute():
        candidate = Path(cwd or os.getcwd()) / candidate
    return candidate.resolve()


def install_child_guard() -> None:
    """Installed automatically in every inherited Python interpreter startup."""
    global _INSTALLED
    if _INSTALLED:
        return
    root = Path(os.environ['W2F_QA_ROOT']).resolve()
    protected = tuple(Path(value).resolve() for value in json.loads(os.environ['W2F_QA_DENY_PATHS']))
    executables = _allowed_executables()
    # Register before running any test/application code. Creation time prevents
    # cleanup from killing a later unrelated process that reuses the same PID.
    import psutil
    process = psutil.Process()
    record = {'pid': process.pid, 'created': process.create_time()}
    path = root / '.processes' / f'{process.pid}.json'
    path.write_text(json.dumps(record), encoding='utf-8')

    def check_path(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(value)).expanduser().resolve()
        if _inside(path, root):
            return
        if any(_inside(path, denied) for denied in protected):
            raise PermissionError('QA cannot access real application state')

    def check_address(address):
        if isinstance(address, tuple):
            host = address[0]
        elif address is None:
            return
        else:
            # Local IPC outside the fixture is not an authorized substitute
            # for network access to a user's running desktop application.
            if not _inside(Path(os.fsdecode(address)).resolve(), root):
                raise PermissionError('QA cannot access real application IPC')
            return
        if isinstance(host, bytes):
            host = host.decode('ascii', errors='strict')
        if host == 'localhost':
            return
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise PermissionError('QA allows only loopback networking')

    def audit(event, args):
        if event == 'subprocess.Popen':
            executable, arguments, cwd, env = args
            child = _command_path(executable, arguments, cwd, env)
            if child not in executables and not _inside(child, root):
                raise PermissionError('QA cannot execute a real external CLI')
        elif event in {'os.exec', 'os.posix_spawn'}:
            # subprocess can legitimately use posix_spawn as an optimization.
            executable, arguments, env = args
            child = _command_path(executable, arguments, None, env)
            if child not in executables and not _inside(child, root):
                raise PermissionError('QA cannot execute a real external CLI')
        elif event == 'os.system':
            raise PermissionError('QA requires audited subprocess execution')
        elif event in {'socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg'}:
            check_address(args[-1])
        elif event == 'socket.getaddrinfo':
            check_address((args[0], args[1]))
        elif event in {'open', 'sqlite3.connect', 'os.listdir', 'os.scandir', 'os.remove',
                       'os.rmdir', 'os.mkdir', 'os.chmod', 'os.truncate'}:
            if args:
                check_path(args[0])
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            check_path(args[0])
            check_path(args[1])

    sys.addaudithook(audit)
    _INSTALLED = True
    os.environ['W2F_QA_GUARD_ACTIVE'] = '1'


class ProcessTree:
    """Track one QA process tree and clean registered detached Python workers.

    Use from the parent runner. A matching PID *and* creation time is mandatory
    before signalling anything. Missing/inaccessible processes are ignored;
    there are no name-based or machine-wide process kills.
    """

    def __init__(self, pid: int, root: Path, interval: float = .03):
        import psutil
        self.psutil = psutil
        self.pid, self.root, self.interval = pid, Path(root), interval
        self.owned = {}
        self.tree_sampling_available = True
        self.stopped = threading.Event()
        self.thread = None
        self._remember(pid)

    def _remember(self, pid):
        if pid == os.getpid():
            return
        try:
            process = self.psutil.Process(pid)
            self.owned.setdefault(pid, process.create_time())
        except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
            pass

    def _sample(self):
        for path in (self.root / '.processes').glob('*.json'):
            try:
                row = json.loads(path.read_text(encoding='utf-8'))
                pid, created = int(row['pid']), float(row['created'])
                if pid != os.getpid():
                    process = self.psutil.Process(pid)
                    if process.create_time() == created:
                        self.owned.setdefault(pid, created)
            except (OSError, ValueError, TypeError, KeyError, self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                continue
        for process in self._live():
            try:
                for child in process.children(recursive=True):
                    self._remember(child.pid)
            except (self.psutil.NoSuchProcess, self.psutil.AccessDenied, OSError):
                # Some macOS host sandboxes forbid enumerating the process
                # table. Registered Python descendants remain cleanable.
                self.tree_sampling_available = False

    def _live(self):
        found = []
        for pid, created in list(self.owned.items()):
            try:
                process = self.psutil.Process(pid)
                if process.create_time() == created and process.is_running():
                    found.append(process)
            except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                pass
        return found

    def _watch(self):
        while not self.stopped.is_set():
            self._sample()
            self.stopped.wait(self.interval)

    def __enter__(self):
        self.thread = threading.Thread(target=self._watch, name='qa-process-watch', daemon=True)
        self.thread.start()
        return self

    def cleanup(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=2)
        self._sample()
        for _ in range(2):
            live = self._live()
            for process in live:
                try:
                    process.terminate()
                except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                    pass
            _, remaining = self.psutil.wait_procs(live, timeout=2)
            for process in remaining:
                try:
                    process.kill()
                except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                    pass
            self.psutil.wait_procs(remaining, timeout=2)
            self._sample()
            if not self._live():
                break

    def __exit__(self, exc_type, exc, traceback):
        self.cleanup()
