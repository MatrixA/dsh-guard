#!/usr/bin/env python3
"""Standalone stdlib launcher; never imports the development guard.

install --source DIR --store DIR explicitly approves a complete application copy
outside the source directory. development.py is included when present; the other
seven allowlisted files are required. run --store DIR --state DIR runs that copy,
retaining DSH across guard restarts. An install takes effect on the next launcher
start; fallback selection persists for that installed current version.

Heartbeat: atomically replace STATE/heartbeat.json with {pid, updated_at, phase}.
Times are Unix seconds. phase='tick' means main-loop progress: only the main
thread may advance updated_at for this phase. Every other phase needs a fixed
'deadline' (at most 1200 seconds away). Its heartbeat thread may refresh
updated_at, but cannot extend the deadline until a new main-loop tick. Heartbeat
freshness is 180 seconds; first heartbeat grace is 120 seconds. Missing/invalid
heartbeats do not renew either allowance. No heartbeat PID is ever signalled.
"""
import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

WINDOWS = os.name == 'nt'
if WINDOWS:
    # The standalone bootstrap loads its private installed helper, never the source tree.
    import importlib.util
    _spec = importlib.util.spec_from_file_location('guard_windows_platform', Path(__file__).with_name('windows_platform.py'))
    win = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(win)
else:
    import fcntl

FILES = ('dsh_guard.py', 'development.py', 'console.py', 'validate.mjs', 'LICENSE',
         'ui/index.html', 'bridge/package.json', 'bridge/index.mjs', 'bridge/cordis.patch.yml') + (
             ('windows_platform.py',) if WINDOWS else ())
# Older installed versions predate LICENSE; new installs include it when present.
REQUIRED_FILES = set(FILES) - {'development.py', 'LICENSE'}


def open_regular(path, flags, mode=0o600):
    return win.open_regular(path, flags, mode) if WINDOWS else os.open(
        path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)


def linked(path):
    return win.is_link(path) if WINDOWS else path.is_symlink()
STARTUP_GRACE = 120
STALE_SECONDS = 180
MAX_OPERATION_SECONDS = 1200
FAILURE_WINDOW = 300
FAILURE_LIMIT = 3
BACKOFF_SECONDS = 5
MAX_BACKOFF = 60
POLL_SECONDS = 2
TERM_SECONDS = 10
KILL_SECONDS = 5
MAX_JSON = 64 * 1024
VERSION = re.compile(r'[0-9a-f]{64}')
PHASE = re.compile(r'[a-z][a-z0-9_-]{0,63}')


class LauncherError(RuntimeError):
    pass


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def atomic(path, value):
    if path.is_symlink():
        raise LauncherError('Symlink metadata destination')
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        if WINDOWS:
            win.replace_file(name, path)
        else:
            os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_directory(path):
    if WINDOWS:
        return win.sync_directory(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path):
    fd = open_regular(path, os.O_RDONLY)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON:
            raise LauncherError('Invalid JSON file')
        data = stream.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise LauncherError('Oversized JSON file')
        return json.loads(data), info.st_mtime


def number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def version_id(value):
    return isinstance(value, str) and VERSION.fullmatch(value) is not None


@contextlib.contextmanager
def exclusive(path):
    if WINDOWS:
        try:
            with win.exclusive(path):
                yield
        except BlockingIOError:
            raise LauncherError('Another launcher or installer holds this lock') from None
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LauncherError('Another launcher or installer holds this lock') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def regular_file(root, name):
    path = root
    for part in ('', *Path(name).parts):
        path = path / part
        if linked(path):
            raise LauncherError('Symlink or reparse point in application: ' + name)
    if not path.is_file():
        raise LauncherError('Missing application file: ' + name)
    return path


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def pointer(store):
    value, _ = read_json(store / 'current.json')
    if (not isinstance(value, dict) or set(value) != {'schema', 'current', 'previous'} or
            value['schema'] != 1 or not version_id(value['current']) or
            (value['previous'] is not None and not version_id(value['previous'])) or
            value['previous'] == value['current']):
        raise LauncherError('Invalid version pointer')
    return value


def validate(store, version):
    if not version_id(version):
        raise LauncherError('Invalid version ID')
    root = store / 'versions' / version
    if (store / 'versions').is_symlink() or root.is_symlink() or not root.is_dir():
        raise LauncherError('Missing or linked version')
    manifest, _ = read_json(root / 'manifest.json')
    if (not isinstance(manifest, dict) or set(manifest) != {'schema', 'files'} or
            manifest['schema'] != 1 or not isinstance(manifest['files'], dict) or
            not REQUIRED_FILES <= set(manifest['files']) <= set(FILES) or
            any(not version_id(h) for h in manifest['files'].values()) or
            hashlib.sha256(encoded(manifest)).hexdigest() != version):
        raise LauncherError('Invalid version manifest')
    expected = set(manifest['files']) | {'manifest.json', 'ui', 'bridge'}
    contents = set()
    def inventory(directory):
        for item in directory.iterdir():
            if linked(item):
                raise LauncherError('Symlink or reparse point in application contents')
            contents.add(item.relative_to(root).as_posix())
            if item.is_dir():
                inventory(item)
    inventory(root)
    if contents != expected:
        raise LauncherError('Unexpected version contents')
    for name in manifest['files']:
        if digest(regular_file(root, name)) != manifest['files'][name]:
            raise LauncherError('Version hash mismatch: ' + name)
    return root


def install(source, store):
    source, store = Path(source).resolve(), Path(store).resolve()
    if store == source or source in store.parents:
        raise LauncherError('Version store must be outside the development source')
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive(store / 'install.lock'):
        versions = store / 'versions'
        if versions.is_symlink():
            raise LauncherError('Linked version store')
        versions.mkdir(exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=versions))
        try:
            hashes = {}
            for name in FILES:
                if name not in REQUIRED_FILES and not (source / name).exists() and not linked(source / name):
                    continue
                original = regular_file(source, name)
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = open_regular(original, os.O_RDONLY)
                with os.fdopen(fd, 'rb') as src, target.open('xb') as dst:
                    if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                        raise LauncherError('Application source is not regular: ' + name)
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                if not WINDOWS:
                    target.chmod(0o400)
                hashes[name] = digest(target)
            manifest = {'schema': 1, 'files': hashes}
            version = hashlib.sha256(encoded(manifest)).hexdigest()
            atomic(stage / 'manifest.json', manifest)
            if not WINDOWS:
                (stage / 'manifest.json').chmod(0o400)
            destination = versions / version
            if destination.exists() or destination.is_symlink():
                # An existing release is never repaired or overwritten in place.
                validate(store, version)
            else:
                sync_directory(stage / 'ui')
                sync_directory(stage / 'bridge')
                sync_directory(stage)
                os.rename(stage, destination)
                sync_directory(versions)
            try:
                old = pointer(store)
            except FileNotFoundError:
                old = None
            if old and old['current'] == version:
                return version
            previous = None
            for candidate in ([old['current'], old['previous']] if old else []):
                if candidate and candidate != version:
                    try:
                        validate(store, candidate)
                        previous = candidate
                        break
                    except (LauncherError, OSError, ValueError):
                        continue
            atomic(store / 'current.json', {'schema': 1, 'current': version, 'previous': previous})
            return version
        finally:
            if stage.exists():
                shutil.rmtree(stage)


class Heartbeat:
    def __init__(self, pid, started, monotonic):
        self.pid = pid
        self.started = started
        self.startup_until = monotonic + STARTUP_GRACE
        self.fresh_until = None
        self.operation_until = None
        self.updated_at = None
        self.phase = None

    def check(self, path, now, monotonic):
        try:
            value, mtime = read_json(path)
            if (not isinstance(value, dict) or type(value.get('pid')) is not int or
                    value['pid'] != self.pid or not number(value.get('updated_at')) or
                    not isinstance(value.get('phase'), str) or not PHASE.fullmatch(value['phase']) or
                    not self.started <= value['updated_at'] <= now + 5 or mtime > now + 5):
                raise ValueError('Invalid heartbeat')
            stamp, phase = value['updated_at'], value['phase']
            deadline = value.get('deadline')
            if phase != 'tick' and (not number(deadline) or deadline > now + MAX_OPERATION_SECONDS or
                                    deadline > stamp + MAX_OPERATION_SECONDS):
                raise ValueError('Invalid operation deadline')
            if self.updated_at is None or stamp > self.updated_at:
                self.fresh_until = monotonic + max(0, STALE_SECONDS - max(0, now - min(stamp, mtime)))
                self.updated_at, self.phase = stamp, phase
                if phase == 'tick':
                    self.operation_until = None
                else:
                    end = monotonic + max(0, deadline - now)
                    # A heartbeat thread cannot slide a stuck operation's lease.
                    self.operation_until = min(self.operation_until, end) if self.operation_until is not None else end
        except (LauncherError, OSError, ValueError, TypeError):
            pass
        if self.operation_until is not None and monotonic >= self.operation_until:
            return 'operation-deadline'
        if self.fresh_until is not None and monotonic >= self.fresh_until:
            return 'heartbeat-stale'
        if self.fresh_until is None and monotonic >= self.startup_until:
            return 'heartbeat-missing'
        return None


def guard_environment():
    keys = ('HOME', 'USER', 'LOGNAME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'TZ', 'SHELL',
            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
            'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'PNPM_HOME',
            'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME',
            'DEEPSEEK_BASE_URL')
    return {**{k: os.environ[k] for k in keys if k in os.environ},
            **(win.environment() if WINDOWS else {}),
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1'}


def terminate_owned(child):
    """Only an unreaped Popen child is authority. Never use stored/heartbeat PIDs."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=TERM_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=KILL_SECONDS)
    return child.returncode


class Runner:
    def __init__(self, store, state):
        self.store, self.state = Path(store).resolve(), Path(state).resolve()
        self.data = {}
        self.child = None

    def save(self, status, **details):
        self.data.update(status=status, pid=os.getpid(), updated_at=time.time(), **details)
        atomic(self.state / 'launcher.json', self.data)

    def event(self, event, **details):
        path = self.state / 'launcher.events.jsonl'
        if path.is_symlink():
            raise LauncherError('Linked launcher event log')
        if path.exists() and path.stat().st_size > 1024 * 1024:
            os.replace(path, self.state / 'launcher.events.previous.jsonl')
        fd = open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(encoded({'time': time.time(), 'event': event, **details}))

    def failures(self):
        cutoff = time.time() - FAILURE_WINDOW
        self.data['failures'] = [f for f in self.data['failures'] if f['at'] >= cutoff]
        return sum(f['version'] == self.data['version'] for f in self.data['failures'])

    def failed(self, reason):
        now = time.time()
        self.data['failures'].append({'at': now, 'version': self.data['version']})
        count = self.failures()
        self.save('backoff', reason=reason, guard_pid=None, inflight=False,
                  next_start=now + min(MAX_BACKOFF, BACKOFF_SECONDS * 2 ** min(count - 1, 6)))
        self.event('guard-failed', version=self.data['version'], reason=reason)

    def fallback(self, reason):
        previous = self.data['previous']
        if previous and self.data['version'] != previous:
            try:
                validate(self.store, previous)
                self.save('fallback', version=previous, reason=reason, guard_pid=None)
                self.event('fallback', version=previous, reason=reason)
                return True
            except (LauncherError, OSError, ValueError):
                pass
        self.save('blocked', reason=reason, guard_pid=None, inflight=False)
        self.event('blocked', reason=reason)
        return False

    def restore(self):
        selected = pointer(self.store)
        try:
            saved, _ = read_json(self.state / 'launcher.json')
        except FileNotFoundError:
            saved = {}
        if not isinstance(saved, dict) or (saved and (saved.get('schema') != 1 or
                      type(saved.get('attempts')) is not int or saved['attempts'] < 0 or
                      not isinstance(saved.get('failures'), list) or
                      not number(saved.get('next_start')) or type(saved.get('inflight')) is not bool or
                      any(not isinstance(f, dict) or not version_id(f.get('version')) or
                          not number(f.get('at')) for f in saved['failures']))):
            raise LauncherError('Invalid persisted launcher accounting')
        self.data = {**selected, 'version': selected['current'], 'attempts': saved.get('attempts', 0),
                     'failures': [], 'next_start': 0, 'guard_pid': None, 'inflight': False}
        if saved.get('current') == selected['current']:
            if saved.get('version') and saved['version'] in (selected['current'], selected['previous']):
                self.data['version'] = saved['version']
            self.data.update(failures=saved['failures'],
                             next_start=min(saved['next_start'], time.time() + MAX_BACKOFF))
            if saved['inflight']:
                # A previous launcher's PID is evidence of an attempt, never kill authority.
                self.failed('launcher-interrupted')
        self.save('starting')

    def run(self, stopping):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive(self.state / 'launcher.lock'):
            self.restore()
            self.job = None
            try:
                return self.supervise(stopping)
            finally:
                try:
                    if self.child is not None:
                        terminate_owned(self.child)
                        self.child = None
                    if self.data.get('status') != 'blocked':
                        self.save('stopped', guard_pid=None, inflight=False)
                        self.event('launcher-stopped')
                finally:
                    if self.job is not None:
                        self.job.close()

    def supervise(self, stopping):
        while not stopping.is_set():
            if self.failures() >= FAILURE_LIMIT:
                if not self.fallback('retry-limit'):
                    return 1
                continue
            remaining = self.data['next_start'] - time.time()
            if remaining > 0:
                self.save('backoff')
                stopping.wait(min(POLL_SECONDS, remaining))
                continue
            try:
                root = validate(self.store, self.data['version'])
            except (LauncherError, OSError, ValueError):
                if not self.fallback('version-invalid'):
                    return 1
                continue
            if stopping.is_set():
                break
            started, monotonic = time.time(), time.monotonic()
            self.save('starting', attempts=self.data['attempts'] + 1, inflight=True,
                      guard_pid=None, phase=None, heartbeat_at=None, reason=None)
            try:
                log = self.state / 'guard-process.log'
                if log.is_symlink():
                    raise OSError('Linked guard process log')
                if log.exists() and log.stat().st_size > 2 * 1024 * 1024:
                    os.replace(log, self.state / 'guard-process.previous.log')
                fd = open_regular(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                with os.fdopen(fd, 'ab') as stream:
                    if WINDOWS:
                        if self.job is not None:
                            self.job.close()
                        self.job = win.GuardJob()
                    self.child = subprocess.Popen([sys.executable, str(root / 'dsh_guard.py'),
                                                   '--state', str(self.state), 'run'], cwd=root,
                                                  env={**guard_environment(), **({'DSH_GUARD_JOB': self.job.name} if WINDOWS else {})}, stdin=subprocess.DEVNULL,
                                                  stdout=stream, stderr=stream,
                                                  **({'creationflags': subprocess.CREATE_NO_WINDOW} if WINDOWS else {'start_new_session': True}),
                                                  close_fds=True)
                if WINDOWS:
                    self.job.assign(self.child)
            except OSError:
                if self.child is not None:
                    terminate_owned(self.child)
                    self.child = None
                self.failed('spawn-failed')
                continue
            heartbeat = Heartbeat(self.child.pid, started, monotonic)
            self.save('running', guard_pid=self.child.pid)
            self.event('guard-started', version=self.data['version'], pid=self.child.pid)
            while not stopping.is_set():
                code = self.child.poll()
                if code is not None:
                    self.child = None
                    self.failed('guard-exited')
                    break
                reason = heartbeat.check(self.state / 'heartbeat.json', time.time(), time.monotonic())
                if reason:
                    try:
                        terminate_owned(self.child)
                    except subprocess.TimeoutExpired:
                        self.save('blocked', reason='guard-would-not-exit')
                        return 1
                    self.child = None
                    self.failed(reason)
                    break
                self.save('running', phase=heartbeat.phase, heartbeat_at=heartbeat.updated_at)
                stopping.wait(POLL_SECONDS)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    install_parser = sub.add_parser('install')
    install_parser.add_argument('--source', type=Path, required=True)
    install_parser.add_argument('--store', type=Path, required=True)
    run_parser = sub.add_parser('run')
    run_parser.add_argument('--store', type=Path, required=True)
    run_parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.command == 'install':
        print(install(args.source, args.store))
        return 0
    stopping = threading.Event()
    old_handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, lambda *_: stopping.set())
        return Runner(args.store, args.state).run(stopping)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (LauncherError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('dsh-guard launcher: ' + str(error), file=sys.stderr)
        sys.exit(1)
