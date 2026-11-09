#!/usr/bin/env python3
"""External DSH supervision with optional native development observation; Python stdlib only."""
import argparse
import contextlib
import functools
import threading
import hashlib
import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
WINDOWS = os.name == 'nt'
if WINDOWS:
    import windows_platform as win
else:
    import fcntl
def xdg_root(key, fallback):
    value = Path(os.environ.get(key) or fallback).expanduser()
    return value if value.is_absolute() else Path(fallback)


STATE = (Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'dsh-guard/state'
         if WINDOWS else xdg_root('XDG_STATE_HOME', Path.home() / '.local/state') / 'dsh-guard')
RELEASES = (STATE.parent / 'releases' if WINDOWS else
            xdg_root('XDG_DATA_HOME', Path.home() / '.local/share') / 'dsh-guard/releases')


def enrollment_home(value=None, config=None):
    """Environment is a first-enrollment default; existing enrollment takes precedence."""
    return Path(value or (config or {}).get('home') or os.environ.get('DSH_HOME', '').strip() or
                Path.home() / '.dsh').expanduser().resolve()
WINDOWS_LIMIT = 'Windows 预览版仅支持进程守护；隔离验证、恢复点、救援与 Claude 修复尚未启用。'


def open_regular(path, flags, mode=0o600):
    return win.open_regular(path, flags, mode) if WINDOWS else os.open(
        path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)


def sync_directory(path):
    if WINDOWS:
        return win.sync_directory(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
MAX_FILE = 1024 * 1024
MAX_ATTEMPTS, MAX_SECONDS, MAX_TURNS, MAX_USD = 2, 1200, 30, 6.0
OPTIONAL_BUNDLES = ['@linxin666/dsh-client-ui-git-graph',
                    '@linxin666/dsh-client-ui-skill-explorer',
                    '@linxin666/dsh-client-ui-task-board', 'dsh-thoughtdag']


class Blocked(RuntimeError):
    pass


def load(path, default=None):
    try:
        if WINDOWS:
            with os.fdopen(open_regular(path, os.O_RDONLY), 'r', encoding='utf-8') as stream:
                return json.load(stream)
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def atomic(path, value, raw=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Blocked('Refusing symlink destination: ' + str(path))
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(value if raw else json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            f.flush()
            os.fsync(f.fileno())
        if WINDOWS:
            win.replace_file(temp, path)
        else:
            os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        Path(temp).unlink(missing_ok=True)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def tree_hash(path):
    """Hash file contents and link text, without following links."""
    path = Path(path)
    if not path.exists():
        raise Blocked('Missing artifact: ' + str(path))
    h = hashlib.sha256()
    for p in sorted(path.rglob('*')):
        if p.is_symlink():
            value = 'link:' + os.readlink(p)
        elif p.is_file():
            value = digest(p)
        else:
            continue
        h.update(str(p.relative_to(path)).encode() + b'\0' + value.encode() + b'\0')
    return h.hexdigest()


def safe_file(home, relative):
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise Blocked('Invalid enrolled path')
    if not (relative in ('profiles/web/package.json', 'profiles/web/cordis.patch.yml',
                         'profiles/web/pnpm-lock.yaml') or
            (relative.startswith('.agent-presets/') and p.name in ('agent.cordis.yml', 'preset.yml'))):
        raise Blocked('File outside enrolled configuration scope')
    target = Path(home) / p
    for parent in [target, *target.parents]:
        if parent == Path(home).parent:
            break
        if parent.is_symlink():
            raise Blocked('Symlink in enrolled path')
    if not target.is_file() or target.stat().st_size > MAX_FILE:
        raise Blocked('Configuration must be an existing bounded regular file')
    return target


def identity(pid):
    """PID plus start time, UID and complete argv; never trust a PID file alone."""
    if WINDOWS:
        return win.identity(pid)
    try:
        p = subprocess.run(['/bin/ps', '-ww', '-p', str(int(pid)), '-o', 'uid=,lstart=,command='],
                           env={**os.environ, 'TZ': 'UTC', 'LC_ALL': 'C'},
                           capture_output=True, text=True, timeout=3)
        value = p.stdout.strip()
        return {'pid': int(pid), 'signature': value} if p.returncode == 0 and value else None
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


def same_process(record):
    if not record:
        return False
    current = identity(record['pid'])
    if WINDOWS and current is None:
        try:
            if win.process_running(record['pid']):
                raise Blocked('Windows process identity is unavailable; refusing to assume it exited')
        except OSError as error:
            raise Blocked('Windows process inspection failed; no process action is permitted') from error
    return current == record


def listening(port):
    if WINDOWS:
        return bool(listener_pids(port))
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def listener_pids(port):
    if WINDOWS:
        try:
            return win.listener_pids(port)
        except OSError as error:
            raise Blocked('Windows listener ownership could not be verified') from error
    try:
        p = subprocess.run(['/usr/sbin/lsof', '-nP', '-ti', f'TCP:{port}', '-sTCP:LISTEN'],
                           capture_output=True, text=True, timeout=3)
        return sorted(set(int(x) for x in p.stdout.split() if x.isdigit()))
    except (OSError, subprocess.TimeoutExpired):
        return []


def redact(text):
    text = re.sub(r'(?i)([?&](?:token|key|secret|api_key)=)[^\s&\"<>]+', r'\1[REDACTED]', str(text))
    text = re.sub(r'(?i)((?:authorization|cookie|api[_-]?key|auth[_-]?token|secret|password)\s*[:=]\s*)[^\n]+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]+)\b', '[REDACTED]', text)
    return text


def child_env(home):
    # Do not inherit a parent agent's sandbox/session identifiers or model credentials.
    keys = ('HOME', 'USER', 'LOGNAME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'TZ', 'SHELL',
            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
            'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'PNPM_HOME',
            'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME',
            'DEEPSEEK_BASE_URL')
    env = {k: os.environ[k] for k in keys if k in os.environ}
    if WINDOWS:
        env.update(win.environment())
    env['DSH_HOME'] = str(home)
    return env


def claude_context(settings_path):
    """Reuse routing/auth from normal CLI settings without loading hooks or permissions."""
    keys = {'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
            'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
            'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
            'ANTHROPIC_SMALL_FAST_MODEL', 'ANTHROPIC_CUSTOM_HEADERS',
            'CLAUDE_CODE_MODEL_CAPABILITIES', 'CLAUDE_CODE_MAX_CONTEXT_TOKENS',
            'CLAUDE_CODE_MAX_OUTPUT_TOKENS', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
            'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    settings = load(settings_path, {})
    if not isinstance(settings, dict) or not isinstance(settings.get('env', {}), dict):
        raise Blocked('Invalid Claude settings structure')
    env = {k: v for k, v in child_env(Path.home()).items() if k != 'DSH_HOME'}
    env.update({k: v for k, v in os.environ.items() if k in keys})
    for key, value in settings.get('env', {}).items():
        if key in keys:
            if not isinstance(value, str) or '\0' in value:
                raise Blocked('Invalid Claude routing environment value for ' + key)
            env[key] = value
    # --restricted intentionally ignores the user file, so select its model explicitly.
    model = env.get('ANTHROPIC_MODEL') or settings.get('model')
    if model is not None and (not isinstance(model, str) or not model.strip() or '\0' in model):
        raise Blocked('Invalid configured Claude model')
    return env, model


def run_bounded(argv, cwd, env=None, input_text=None, timeout=60, limit=2 * MAX_FILE, cancel=None):
    """No shell, bounded capture, and terminate the whole owned process group on timeout."""
    if WINDOWS:
        raise Blocked('Windows temporary process-tree containment is not implemented')
    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        # Python 3.11 communicate(None) can stop resuming a partially written stdin after a timeout.
        # A private finite file also avoids pipe backpressure while we poll deadlines/cancellation.
        if input_text is not None:
            inp.write(input_text.encode())
        inp.seek(0)
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=inp,
                             stdout=out, stderr=err, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel and cancel():
                    raise Blocked('Manual stop requested')
                if out.tell() > limit or err.tell() > limit:
                    raise Blocked('Subprocess output exceeded limit')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Blocked('Subprocess deadline exceeded')
                try:
                    p.wait(timeout=min(.1, remaining))
                    if out.tell() > limit or err.tell() > limit:
                        raise Blocked('Subprocess output exceeded limit')
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
            raise
        out.seek(0)
        err.seek(0)
        return p.returncode, out.read().decode(errors='replace'), err.read().decode(errors='replace')


def clone(source, destination):
    """APFS clone when available, normal copy elsewhere; never overwrite a target."""
    source, destination = Path(source).resolve(), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise Blocked('Snapshot destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'darwin':
        subprocess.run(['/bin/cp', '-cR', str(source), str(destination)], check=True)
    else:
        shutil.copytree(source, destination, symlinks=True)


def sandbox_profile(work, readonly, port=None):
    q = lambda p: json.dumps(str(Path(p).resolve()))
    reads = [Path('/System'), Path('/usr'), Path('/bin'), Path('/sbin'), Path('/dev'),
             Path('/private/etc'), Path('/private/preboot'), Path('/private/var/db/dyld'),
             Path('/Library/Apple'), Path('/opt/homebrew'), *readonly, work]
    # Keep OS runtime operations, then deny data access outside explicit paths.
    # In particular, no user home, production data, keychain, external/loopback connects.
    readable = ' '.join(f'(subpath {q(p)})' for p in reads)
    executables = ' '.join(f'(literal {q(p)})' for p in readonly if Path(p).is_file())
    lines = ['(version 1)', '(allow default)',
             f'(deny file-read-data (require-all (subpath "/Users") (require-not (require-any {readable}))))',
             f'(deny file-write* (require-not (require-any (subpath {q(work)}) (literal "/dev/null"))))',
             f'(deny process-exec (require-not (require-any {executables})))',
             '(deny mach-lookup)', '(deny signal)', '(allow signal (target self))', '(deny network*)']
    if port:
        lines += [f'(allow network-inbound (local ip "localhost:{port}"))',
                  f'(allow network-bind (local ip "localhost:{port}"))']
    return '\n'.join(lines)


def bounded_operation(method):
    """A busy main loop gets a fixed watchdog deadline, never an unbounded heartbeat."""
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        if getattr(self, '_operation_active', False):
            return method(self, *args, **kwargs)
        self.remaining()
        if not getattr(self, '_supervising', False):
            return method(self, *args, **kwargs)
        stopped = threading.Event()
        started = time.time()
        deadline = started + self.remaining()
        self._operation_active = True
        def beat():
            atomic(self.directory / 'heartbeat.json', {'pid': os.getpid(), 'updated_at': time.time(),
                                                      'phase': method.__name__, 'deadline': deadline})
        def refresh():
            while not stopped.wait(20):
                try:
                    beat()
                except OSError:
                    return
        thread = threading.Thread(target=refresh, daemon=True)
        try:
            beat()
            thread.start()
            return method(self, *args, **kwargs)
        finally:
            stopped.set()
            if thread.ident is not None:
                thread.join(timeout=1)
            self._operation_active = False
            atomic(self.directory / 'heartbeat.json', {'pid': os.getpid(), 'updated_at': time.time(), 'phase': 'tick'})
    return call


class Guard:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.cfg = load(self.directory / 'config.json', {})
        if WINDOWS and self.cfg and self.cfg.get('platform_mode') != 'windows-supervision-preview':
            raise Blocked('Windows needs a freshly enrolled native supervision configuration')
        self.state = load(self.directory / 'state.json', {'mode': 'observe', 'health': 'unknown'})
        self.child = None
        self.opener = None
        self.auth_owner = None

    def require_recovery(self):
        if WINDOWS:
            raise Blocked(WINDOWS_LIMIT)

    def launch_allowed(self, manifest):
        if WINDOWS:
            return (self.cfg.get('platform_mode') == 'windows-supervision-preview' and
                    manifest.get('launch_only') is True and manifest.get('platform') == 'win32' and
                    manifest.get('validated') is False and not self.cfg.get('development_mode') and
                    not self.state.get('run_home'))
        return manifest.get('validated', False)

    def save(self):
        atomic(self.directory / 'state.json', self.state)

    def log(self, event, **details):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / 'events.jsonl'
        if path.exists() and path.stat().st_size > 2 * MAX_FILE:
            path.replace(self.directory / 'events.previous.jsonl')
        with os.fdopen(open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'time': time.time(), 'event': event,
                                **{k: redact(str(v)) for k, v in details.items()}}) + '\n')

    @contextlib.contextmanager
    def lock(self, name='operation.lock'):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / name
        if WINDOWS:
            try:
                with win.exclusive(path):
                    self.state = load(self.directory / 'state.json', self.state)
                    self.cfg = load(self.directory / 'config.json', self.cfg)
                    if self.cfg.get('platform_mode') != 'windows-supervision-preview':
                        raise Blocked('Invalid Windows supervision configuration')
                    yield
            except BlockingIOError:
                raise Blocked('Another guard operation holds the lock') from None
            return
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked('Another guard operation holds the lock') from None
            try:
                self.state = load(self.directory / 'state.json', self.state)
                self.cfg = load(self.directory / 'config.json', self.cfg)
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @property
    def port(self):
        return int(self.cfg.get('port', 3080))

    def release(self, rid=None, verify=False):
        rid = rid or self.state.get('release') or self.cfg.get('initial_release')
        if not rid or not re.fullmatch(r'[A-Za-z0-9_.-]+', rid) or rid in ('.', '..'):
            raise Blocked('No valid prepared release selected')
        p = Path(self.cfg['releases']) / rid
        if p.is_symlink():
            raise Blocked('Release must not be a symlink')
        manifest = load(p / 'release.json')
        if not manifest or manifest.get('id') != rid:
            raise Blocked('Release manifest missing')
        if WINDOWS and verify:
            if not self.launch_allowed(manifest):
                raise Blocked('Invalid Windows launch baseline; it is not a recovery snapshot')
            for key in ('node', 'entry'):
                candidate = Path(manifest[key])
                if not candidate.is_absolute() or digest(candidate) != manifest[key + '_hash']:
                    raise Blocked('Windows executable or entry changed; enroll it explicitly again')
                with os.fdopen(open_regular(candidate, os.O_RDONLY), 'rb'):
                    pass
        if verify:
            for name, expected in manifest['artifacts'].items():
                if tree_hash(p / name) != expected:
                    raise Blocked('Prepared artifact drift: ' + name)
        return p, manifest

    def source_hashes(self, manifest):
        home = Path(self.cfg['home'])
        values = {name: digest(safe_file(home, name)) for name in manifest['files']}
        shared = home / 'cordis.patch.yml'
        values['shared:cordis.patch.yml'] = digest(shared) if shared.is_file() else None
        presets = home / '.agent-presets'
        values['shared:presets'] = tree_hash(presets) if presets.is_dir() else None
        return values

    def drift(self):
        expected = self.state.get('installed_hashes')
        if not expected:
            return False
        _, m = self.release()
        return self.source_hashes(m) != expected

    @contextlib.contextmanager
    def profile_lock(self):
        """Use DSH's exclusive-create writer protocol; never steal a stale native lock."""
        profile = Path(self.cfg['home']) / 'profiles/web'
        path = profile / 'package.json.lock'
        if (profile / '.plugin-manager/run.json').exists():
            raise Blocked('Native package operation requires completion or native recovery')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise Blocked('Native profile writer is busy') from None
        try:
            os.write(fd, (str(os.getpid()) + '\n').encode())
            os.fsync(fd)
            yield
        finally:
            if path.exists() and path.stat().st_ino == os.fstat(fd).st_ino:
                path.unlink()
            os.close(fd)

    def bridge(self, now=None):
        now = time.time() if now is None else now
        try:
            path = self.directory / 'bridge.json'
            if path.is_symlink() or path.stat().st_size > MAX_FILE:
                return None
            value = load(path)
            record = self.state.get('child') or {}
            if (value.get('schema') != 1 or type(value.get('pid')) is not int or value['pid'] <= 0 or
                    not isinstance(value.get('epoch'), str) or not value['epoch'] or
                    value.get('pid') != record.get('pid') or value.get('epoch') != self.state.get('bridge_epoch') or
                    not 0 <= now - float(value['updated_at']) <= 15):
                return None
            return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def development_fingerprint(self, manifest=None):
        """Content and dependency generation, including declared local build artifacts."""
        if manifest is None:
            _, manifest = self.release()
        values = self.source_hashes(manifest)
        profile = Path(self.cfg['home']) / 'profiles/web'
        modules = profile / 'node_modules'
        h = hashlib.sha256()
        # ponytail: metadata scan of installed packages; content hash only local declared artifacts.
        # A native generation and independent scratch boot must also pass before acceptance.
        for p in sorted(modules.rglob('*')):
            if p.is_symlink():
                value = 'link:' + os.readlink(p)
            elif p.is_file():
                stat = p.stat()
                value = f'{stat.st_size}:{stat.st_mtime_ns}'
            else:
                continue
            h.update(str(p.relative_to(modules)).encode() + b'\0' + value.encode() + b'\0')
        pkg = load(profile / 'package.json')
        for name, spec in pkg.get('dependencies', {}).items():
            if not isinstance(spec, str) or not spec.startswith(('link:', 'file:')):
                continue
            target = (modules / name).resolve()
            package = load(target / 'package.json')
            if not isinstance(package, dict) or not isinstance(package.get('files'), list):
                raise Blocked('Local development package has no declared build artifacts: ' + name)
            h.update(name.encode() + digest(target / 'package.json').encode())
            for entry in package['files']:
                rel = Path(entry)
                if rel.is_absolute() or '..' in rel.parts or '*' in entry:
                    raise Blocked('Local package artifact scope cannot be frozen: ' + name)
                artifact = target / rel
                if not artifact.exists():
                    continue
                if not artifact.resolve().is_relative_to(target):
                    raise Blocked('Local artifact escapes its package')
                h.update(entry.encode() + (tree_hash(artifact) if artifact.is_dir() else digest(artifact)).encode())
        values['dependencies'] = h.hexdigest()
        return values

    def native_busy(self, bridge=None):
        profile = Path(self.cfg['home']) / 'profiles/web'
        return ((profile / 'package.json.lock').exists() or
                (profile / '.plugin-manager/run.json').exists() or
                bool(bridge and bridge.get('phase') == 'busy'))

    @bounded_operation
    def checkpoint(self):
        self.require_recovery()
        """Freeze and independently validate a completed development generation."""
        if not self.cfg.get('development_mode'):
            raise Blocked('Development collaboration is not enabled')
        bridge = self.bridge()
        if self.state.get('run_home'):
            raise Blocked('Return to development before creating a new recovery point')
        if not bridge or bridge.get('phase') != 'idle' or not same_process(self.state.get('child')):
            raise Blocked('Host development status is unavailable or not idle')
        capabilities = bridge.get('capabilities', [])
        if not capabilities or any(x.get('status') not in ('ready', 'disabled') for x in capabilities):
            raise Blocked('A plugin or capability is not ready; checkpoint deferred')
        before = self.development_fingerprint()
        p, m = self.release(verify=True)
        rid = 'dev-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:6]
        self.state['workflow_phase'] = 'checkpointing'
        self.save()
        with self.profile_lock():
            if self.development_fingerprint() != before:
                raise Blocked('Development changed before checkpoint capture')
            prepare(self, p / 'runtime', Path(m['node']), Path(self.cfg['home']), rid)
        self.validate(rid)
        fresh = self.bridge()
        with self.profile_lock():
            if (self.development_fingerprint() != before or not fresh or fresh.get('phase') != 'idle' or
                    fresh.get('generation') != bridge.get('generation') or
                    any(x.get('status') not in ('ready', 'disabled') for x in fresh.get('capabilities', []))):
                raise Blocked('Development changed during checkpoint validation; candidate retained')
            self.state.update(release=rid, known_good=rid, installed_hashes=self.source_hashes(self.release(rid)[1]),
                              development_accepted=before, checkpoint_at=time.time(), workflow_phase='ready',
                              checkpoint_error=None, checkpoint_attempt=None, checkpoint_failures=0, checkpoint_retry_at=0)
            self.save()
        self.log('checkpoint-created', release=rid)
        return rid

    def observe_development(self, now, bridge):
        if not self.cfg.get('development_mode') or self.state.get('run_home'):
            return
        try:
            fingerprint = self.development_fingerprint()
            if fingerprint == self.state.get('development_accepted'):
                self.state.update(pending_changes=False, workflow_phase='ready', checkpoint_error=None)
                return
            self.state['pending_changes'] = True
            if fingerprint != self.state.get('development_pending'):
                self.state.update(development_pending=fingerprint, change_seen_at=now,
                                  checkpoint_attempt=None, checkpoint_error=None, checkpoint_failures=0, checkpoint_retry_at=0)
            self.state['workflow_phase'] = 'changing'
            quiet = now - self.state.get('change_seen_at', now) >= max(self.cfg.get('checkpoint_quiet', 30), self.cfg.get('accept_window', 60))
            retry = (self.state.get('checkpoint_failures', 0) < 3 and now >= self.state.get('checkpoint_retry_at', 0))
            if (quiet and bridge and bridge.get('phase') == 'idle' and not self.native_busy(bridge) and
                    (self.state.get('checkpoint_attempt') != fingerprint or retry)):
                # Transient validation/concurrency failures get three attempts, five minutes apart.
                self.state.update(checkpoint_attempt=fingerprint, checkpoint_retry_at=now + 300,
                                  checkpoint_failures=self.state.get('checkpoint_failures', 0) + 1)
                self.save()
                self.checkpoint()
        except (Blocked, ValueError, OSError) as error:
            # A rejected development generation does not interrupt the still healthy Host.
            self.state.update(checkpoint_error=redact(error), workflow_phase='changing')
            self.log('checkpoint-deferred', reason=error)

    @bounded_operation
    def rescue_publish(self, rid):
        self.require_recovery()
        """Boot a compatible frozen profile without replacing the working profile."""
        self.remaining()
        p, m = self.release(rid, verify=True)
        _, previous = self.release()
        if not m.get('validated') or m['dsh_version'] != previous['dsh_version']:
            raise Blocked('Rescue requires a validated compatible runtime')
        if self.native_busy(self.bridge()):
            raise Blocked('Native package operation is still active; rescue deferred')
        current = self.source_hashes(previous)
        if any(current.get(k) != m['source_hashes'].get(k) for k in ('shared:cordis.patch.yml', 'shared:presets')):
            raise Blocked('Shared composition changed; rescue requires review')
        home = Path(self.cfg['home'])
        rescue = self.directory / 'rescue' / uuid.uuid4().hex
        profile = rescue / 'profiles/web'
        profile.parent.mkdir(parents=True)
        atomic(rescue / 'package.json', {'name': 'dsh-guard-rescue', 'private': True})
        clone(p / 'profile', profile)
        for original in (p / 'profile/node_modules').rglob('*'):
            if original.is_symlink():
                resolved = original.resolve()
                if not resolved.is_relative_to(p):
                    raise Blocked('External rescue artifact link')
                link = profile / original.relative_to(p / 'profile')
                link.unlink()
                link.symlink_to(resolved, target_is_directory=resolved.is_dir())
        atomic(profile / 'cordis.yml', '[]\n', raw=True)
        for item in home.iterdir():
            if item.name in ('profiles', 'settings.yaml', 'settings.yaml.imported', 'settings.yaml.imported.kept', '.credentials.yaml'):
                continue
            if item.name == 'cordis.patch.yml':
                shutil.copy2(item, rescue / item.name)
            elif item.is_dir() or item.name in ('.env', '.anonymous-user-id', 'AGENTS.md', 'CLAUDE.md'):
                (rescue / item.name).symlink_to(item.resolve(), target_is_directory=item.is_dir())
        # Native atomic credential writes must target the original regular file, never a symlink.
        atomic(rescue / 'guard-data.patch.yml',
               '- id: credentials\n  config:\n    path: ' + json.dumps(str(home / '.credentials.yaml')) + '\n', raw=True)
        evidence = self.directory / 'incidents' / self.incident()['id']
        evidence.mkdir(parents=True, exist_ok=True)
        for name in previous['files']:
            source = safe_file(home, name)
            dest = evidence / name
            if not dest.exists():
                atomic(dest, source.read_text(), raw=True)
        atomic(evidence / 'development.json', {'source_hashes': current, 'release': self.state.get('release')})
        old = {k: self.state.get(k) for k in ('release', 'run_home', 'rescue_hashes')}
        self.stop_child()
        self.state.update(release=rid, run_home=str(rescue), rescue_hashes={n: digest(profile / n) for n in ('package.json', 'cordis.patch.yml')},
                          workflow_phase='rescue', installed_hashes=current)
        try:
            self.state['rescue_observed'] = self.development_fingerprint()
        except (Blocked, ValueError, OSError):
            self.state['rescue_observed'] = current
        self.save()
        try:
            self.launch()
            self.await_ready()
            self.state.update(health='ready', known_good=rid, workflow_phase='rescue')
            self.save()
            self.log('rescue-started', release=rid, evidence=str(evidence))
        except BaseException:
            self.stop_child()
            self.state.update(old)
            self.save()
            raise

    def await_ready(self):
        deadline = time.monotonic() + self.cfg.get('startup_grace', 90) + self.cfg.get('accept_window', 60)
        ready = None
        while time.monotonic() < deadline:
            self.remaining()
            if self.child:
                self.child.poll()
            if not same_process(self.state.get('child')):
                raise Blocked('DSH exited during stability check')
            if self.probe()[0] == 'ready':
                ready = ready or time.monotonic()
                if time.monotonic() - ready >= self.cfg.get('accept_window', 60):
                    return
            else:
                ready = None
            time.sleep(.2)
        raise Blocked('DSH readiness deadline exceeded')

    @bounded_operation
    def observe_rescue(self, force=False):
        """Return only after a changed working tree passes independent native validation."""
        try:
            if not self.state.get('run_home'):
                raise Blocked('Already running the development profile')
            fingerprint = self.development_fingerprint()
            if self.native_busy():
                raise Blocked('Native package operation is still active')
            if not force and fingerprint == self.state.get('rescue_observed'):
                return
            self.state['rescue_observed'] = fingerprint
            self.save()
            p, m = self.release(verify=True)
            rid = 'return-' + uuid.uuid4().hex[:12]
            with self.profile_lock():
                prepare(self, p / 'runtime', Path(m['node']), Path(self.cfg['home']), rid)
            self.validate(rid)
            if self.development_fingerprint() != fingerprint:
                raise Blocked('Working tree changed during return validation')
            self.return_development()
            with self.profile_lock():
                if self.development_fingerprint() != fingerprint:
                    raise Blocked('Working tree changed during return stability window')
                self.state.update(release=rid, known_good=rid, development_accepted=fingerprint,
                                  installed_hashes=self.source_hashes(self.release(rid)[1]),
                                  pending_changes=False, checkpoint_at=time.time(), checkpoint_error=None)
                self.save()
            self.log('checkpoint-created', release=rid)
        except (Blocked, OSError, ValueError) as error:
            self.state['checkpoint_error'] = redact(error)
            self.log('development-return-deferred', reason=error)
            if force:
                raise

    @bounded_operation
    def return_development(self):
        self.require_recovery()
        rescue = self.state.get('run_home')
        if not rescue:
            raise Blocked('Already running the development profile')
        profile = Path(rescue) / 'profiles/web'
        if any(digest(profile / name) != expected for name, expected in self.state['rescue_hashes'].items()):
            raise Blocked('Rescue settings changed; preserve and reconcile them before return')
        if self.native_busy():
            raise Blocked('Native package operation is still active')
        self.stop_child()
        self.state.update(run_home=None, workflow_phase='changing', breaker=None)
        self.save()
        try:
            self.launch()
            self.await_ready()
            bridge = self.bridge()
            if not bridge or bridge.get('phase') != 'idle' or any(x.get('status') not in ('ready', 'disabled') for x in bridge.get('capabilities', [])):
                raise Blocked('Development plugin capabilities are not ready')
            self.state.update(incident=None, pending_changes=True, ready_since=time.time())
            self.save()
            self.log('development-restored')
        except BaseException:
            self.stop_child()
            self.state.update(run_home=rescue, workflow_phase='rescue')
            self.save()
            self.launch()
            raise

    def control(self, action):
        if action == 'stop':
            atomic(self.directory / 'manual-stop', {'requested': time.time()})
            self.state.update(mode='stopped', health='unknown', reason='Stopped by user')
            self.save()
            self.stop_child()
        elif action == 'resume':
            if listening(self.port) and not same_process(self.state.get('child')):
                raise Blocked('Unmanaged listener prevents resume')
            (self.directory / 'manual-stop').unlink(missing_ok=True)
            if self.state.get('incident'):
                self.state['last_incident'] = self.state['incident']
            self.state.update(mode='manage', breaker=None, incident=None, next_start=0)
            self.save()
        elif action == 'restart':
            if self.state.get('mode') != 'manage' or (self.directory / 'manual-stop').exists():
                raise Blocked('Resume management before requesting restart')
            if self.native_busy(self.bridge()):
                raise Blocked('Native package operation is still active')
            self.stop_child()
            self.launch()
        elif action == 'checkpoint':
            self.checkpoint()
        elif action == 'return-development':
            self.require_recovery()
            self.observe_rescue(force=True)
        else:
            raise Blocked('Unknown control action')
        self.log('control-completed', action=action)

    def breaker(self, reason):
        self.state.update(health='blocked', breaker=redact(reason), ready_since=None)
        self.save()
        self.log('circuit-open', reason=reason)

    def application_url(self):
        """Return the native login URL only for the currently owned listener."""
        record = self.state.get('child')
        if not same_process(record) or listener_pids(self.port) != [record['pid']]:
            return None
        try:
            fd = open_regular(self.state.get('child_log', '/nonexistent'), os.O_RDONLY)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    return None
                stream.seek(max(0, info.st_size - 256 * 1024))
                text = stream.read(256 * 1024).decode(errors='replace')
        except (OSError, ValueError, TypeError):
            return None
        tokens = re.findall(r'http://127\.0\.0\.1:' + str(self.port) + r'/\?token=([A-Za-z0-9_-]+)', text)
        if not tokens or not same_process(record) or listener_pids(self.port) != [record['pid']]:
            return None
        return f'http://127.0.0.1:{self.port}/?token={tokens[-1]}'

    def probe(self):
        base = f'http://127.0.0.1:{self.port}'
        owners = listener_pids(self.port)
        record = self.state.get('child')
        if record and same_process(record) and owners != [record['pid']]:
            return ('unknown' if not owners else 'blocked'), 'Listener ownership not proved'
        if not listening(self.port):
            return 'failed', 'Connection refused'
        if not record or not same_process(record):
            return 'live', 'Unmanaged listener; observation only'
        if self.auth_owner != record:
            self.opener = None
            self.auth_owner = record
        if self.opener is None:
            try:
                url = self.application_url()
                if url:
                    jar = http.cookiejar.CookieJar()
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
                    with opener.open(url, timeout=self.cfg.get('request_timeout', 10)) as r:
                        if r.status == 200 and list(jar):
                            self.opener = opener
            except (OSError, urllib.error.URLError):
                pass
        if self.opener is None:
            return 'unknown', 'Authenticated readiness unavailable (401/404 are not a crash)'
        envelope = {'type': 'client-request', 'rpcId': 'dsh-guard-readiness',
                    'method': 'session/list', 'payload': {'args': {'_request': {}}}}
        req = urllib.request.Request(base + '/api/session/list', data=json.dumps(envelope).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=self.cfg.get('request_timeout', 10)) as r:
                value = json.loads(r.read(2 * MAX_FILE))
            result = value.get('result', {})
            if value.get('rpcId') == envelope['rpcId'] and result.get('ok') is True and isinstance(result.get('value', {}).get('items'), list):
                bridge = self.bridge() if self.cfg.get('development_mode') else None
                if bridge:
                    self.state['bridge_seen'] = True
                    bad = [x for x in bridge.get('capabilities', []) if x.get('status') not in ('ready', 'disabled')]
                    if any(x.get('required') for x in bad) and not self.native_busy(bridge):
                        return 'failed', 'A required Host capability is unavailable'
                    if bad or bridge.get('phase') == 'error':
                        return 'degraded', 'An optional plugin or development operation failed'
                return 'ready', 'Authenticated session/list succeeded'
            return 'degraded', 'Session RPC contract or service unavailable'
        except urllib.error.HTTPError as e:
            return ('unknown' if e.code in (401, 403, 404) else 'degraded'), 'Readiness HTTP ' + str(e.code)
        except (OSError, ValueError, urllib.error.URLError):
            return 'failed', 'Authenticated readiness timed out or malformed'

    def stop_child(self):
        record = self.state.get('child')
        if not record:
            return
        if self.child and self.child.pid == record['pid'] and self.child.poll() is not None:
            self.state.pop('child', None)
            self.save()
            return
        if same_process(record):
            if listener_pids(self.port) not in ([], [record['pid']]):
                raise Blocked('Another process holds the port; refusing stop')
            if WINDOWS:
                if self.state.get('windows_console_owner') != record:
                    raise Blocked('Dedicated Windows console ownership is not recorded; refusing signal')
                win.graceful_stop(record)
            else:
                os.kill(record['pid'], signal.SIGINT)
            deadline = time.monotonic() + self.cfg.get('stop_timeout', 60)
            while same_process(record):
                if self.child:
                    self.child.poll()
                if time.monotonic() >= deadline:
                    raise Blocked('Graceful shutdown timed out; no SIGKILL sent')
                time.sleep(.1)
        if self.child and self.child.pid == record['pid']:
            try:
                self.child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise Blocked('Child identity changed before shutdown completed')
        self.state.pop('child', None)
        self.opener = None
        self.save()
        if listening(self.port):
            raise Blocked('Port still occupied after owned child shutdown')

    def launch(self):
        if self.state.get('mode') != 'manage' or self.state.get('breaker'):
            raise Blocked('Managed launch is paused or circuit is open')
        if same_process(self.state.get('child')):
            return
        if listening(self.port):
            raise Blocked('Unmanaged port owner; no duplicate launch')
        p, m = self.release(verify=True)
        if not self.launch_allowed(m):
            raise Blocked('Selected release has no current runtime validation')
        node = m['node']
        if digest(node) != m['node_hash']:
            raise Blocked('Node executable changed')
        logs = self.directory / 'web-logs'
        logs.mkdir(exist_ok=True)
        for old in sorted(logs.glob('*.log'), key=lambda x: x.stat().st_mtime)[:-9]:
            old.unlink()
        log = logs / (uuid.uuid4().hex + '.log')
        home = self.state.get('run_home') or self.cfg['home']
        command = [node, str(p / m['entry']), '--profile', 'web']
        if self.state.get('run_home'):
            command += ['--patch', str(Path(home) / 'guard-data.patch.yml')]
        # DSH forwards everything from the first app option onward to the inner CLI.
        command += ['--port', str(self.port), '--no-open']
        env = child_env(home)
        epoch = uuid.uuid4().hex
        if self.cfg.get('development_mode'):
            env.update(DSH_GUARD_BRIDGE_FILE=str(self.directory / 'bridge.json'), DSH_GUARD_EPOCH=epoch)
        with log.open('xb') as f:
            options = win.persistent_options() if WINDOWS else {'start_new_session': True}
            self.child = subprocess.Popen(command, cwd=Path(home) / 'profiles/web',
                                          env=env, stdin=subprocess.DEVNULL,
                                          stdout=f, stderr=f, **options)
        record = identity(self.child.pid)
        if not record:
            raise Blocked('Child exited before identity could be recorded')
        if WINDOWS:
            self.state['windows_console_owner'] = record
        self.state.update(child=record, child_log=str(log), started=time.time(), health='unknown', reason='Starting managed DSH',
                          bridge_epoch=epoch, busy_since=None, bridge_seen=False, workflow_phase='starting',
                          failures=0, ready_since=None)
        self.opener = None
        self.save()
        self.log('launched', pid=self.child.pid, release=m['id'])

    def remaining(self, ceiling=MAX_SECONDS):
        if getattr(self, 'shutting_down', False):
            raise Blocked('Guardian restart requested')
        if (self.directory / 'manual-stop').exists():
            self.state['mode'] = 'stopped'
            self.save()
            raise Blocked('Manual stop requested')
        incident = self.state.get('incident')
        remaining = MAX_SECONDS - (time.time() - incident['started']) if incident else ceiling
        if remaining <= 0:
            raise Blocked('Incident recovery deadline exceeded')
        return min(ceiling, remaining)

    def prove_sandbox(self, node):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='containment-', dir=self.directory) as outer:
            outer = Path(outer)
            work = outer / 'allowed'
            work.mkdir()
            sentinel = outer / 'private-sentinel'
            sentinel.write_text('guard fixture; no production secret')
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.close()
            code = '''const fs=require('node:fs'),cp=require('node:child_process'),net=require('node:net');
const [work,outside,port,parent]=process.argv.slice(1); const result={};
fs.writeFileSync(work+'/ok','ok'); result.writeInside=true;
try {fs.readFileSync(outside);result.readOutside=false;} catch(e){result.readOutside=e.code==='EPERM'||e.code==='EACCES';}
try {fs.writeFileSync(outside,'bad');result.writeOutside=false;} catch(e){result.writeOutside=e.code==='EPERM'||e.code==='EACCES';}
let child=cp.spawnSync('/usr/bin/true');result.execOutside=!!child.error && ['EPERM','EACCES'].includes(child.error.code);
try {process.kill(Number(parent),0);result.signalOutside=false;} catch(e){result.signalOutside=['EPERM','EACCES'].includes(e.code);}
function blocked(host,port){return new Promise(resolve=>{let s=net.connect({host,port});s.setTimeout(1500);s.on('connect',()=>{s.destroy();resolve(false)});s.on('error',e=>resolve(['EPERM','EACCES'].includes(e.code)));s.on('timeout',()=>{s.destroy();resolve(false)});});}
(async()=>{result.external=await blocked('1.1.1.1',443);result.productionLoopback=await blocked('127.0.0.1',3080);
const server=net.createServer();await new Promise((resolve,reject)=>{server.on('error',reject);server.listen(Number(port),'127.0.0.1',resolve)});result.listenInside=true;server.close();console.log(JSON.stringify(result));})().catch(e=>{console.error(e.code);process.exitCode=1});'''
            rc, out, err = run_bounded(['/usr/bin/sandbox-exec', '-p', sandbox_profile(work, [Path(node)], port),
                                        node, '-e', code, str(work), str(sentinel), str(port), str(os.getpid())], work,
                                       child_env(work), timeout=self.remaining(10))
            expected = {'writeInside', 'readOutside', 'writeOutside', 'execOutside', 'signalOutside', 'external',
                        'productionLoopback', 'listenInside'}
            result = json.loads(out) if rc == 0 else {}
            if set(result) != expected or not all(v is True for v in result.values()) or sentinel.read_text() != 'guard fixture; no production secret':
                raise Blocked('Native containment self-test failed: ' + redact(err or str(result)))
            self.log('containment-proved', checks=len(expected))

    def incident(self):
        value = self.state.get('incident')
        if value is None:
            value = {'id': uuid.uuid4().hex, 'started': time.time(), 'restarts': 0,
                     'attempts': 0, 'turns': 0, 'usd': 0.0, 'accounting_uncertain': False,
                     'rollback_done': False}
            self.state['incident'] = value
            self.save()
        return value

    def reserve_repair(self):
        i = self.incident()
        if (i['attempts'] >= MAX_ATTEMPTS or time.time() - i['started'] >= MAX_SECONDS or
                i['turns'] >= MAX_TURNS or i['usd'] >= MAX_USD or i['accounting_uncertain']):
            raise Blocked('Repair budget exhausted or accounting uncertain')
        i['attempts'] += 1
        turns = min(15, MAX_TURNS - i['turns'])
        i['turns'] += turns  # reserve worst-case, including timed-out invocations
        i['accounting_uncertain'] = True
        self.save()
        return turns, MAX_USD - i['usd'], max(1, MAX_SECONDS - (time.time() - i['started']))

    def diagnostic_codes(self):
        try:
            path = Path(self.state['child_log'])
            with path.open('rb') as f:
                f.seek(max(0, path.stat().st_size - 64 * 1024))
                text = f.read().decode(errors='replace')
        except (KeyError, OSError):
            return []
        # Only fixed error codes and enrolled package names leave this machine.
        codes = [code for code in ('EADDRINUSE', 'ECONNREFUSED', 'MODULE_NOT_FOUND',
                                  'ERR_MODULE_NOT_FOUND', 'YAMLParseError', 'YAMLException',
                                  'incompatible', 'required plugins did not activate', 'EACCES', 'EPERM') if code in text]
        for line in text.splitlines():
            if not re.search(r'cannot find|not found|(?:ERR_)?MODULE_NOT_FOUND', line, re.I):
                continue
            for bundle in self.cfg.get('optional_bundles', []):
                if re.search(r'(?<![\w@.-])' + re.escape(bundle) + r'(?![\w.-])', line):
                    code = 'optional-bundle-import:' + bundle
                    if code not in codes:
                        codes.append(code)
        return codes

    def repair(self):
        self.require_recovery()
        if not self.cfg.get('repair_enabled'):
            raise Blocked('Automatic Claude repair not enabled')
        turns, usd, seconds = self.reserve_repair()
        p, m = self.release(verify=True)
        operations = {'type': 'object', 'additionalProperties': False,
                      'properties': {'bundle': {'type': 'string', 'enum': self.cfg.get('optional_bundles', [])},
                                     'enabled': {'type': 'boolean'}}, 'required': ['bundle', 'enabled']}
        scalar = {'type': ['boolean', 'number', 'string', 'null']}
        field_schema = {'type': 'object', 'additionalProperties': False,
                        'properties': {'file': {'type': 'string', 'enum': list(self.cfg.get('yaml_fields', {})) or ['UNENROLLED']},
                                       'changes': {'type': 'array', 'maxItems': 32, 'items': {
                                           'type': 'object', 'additionalProperties': False,
                                           'properties': {'path': {'type': 'array', 'maxItems': 32, 'items': {'type': ['string', 'integer']}},
                                                          'expected': scalar, 'value': scalar},
                                           'required': ['path', 'expected', 'value']}}}, 'required': ['file', 'changes']}
        schema = {'type': 'object', 'additionalProperties': False,
                  'properties': {'diagnosis': {'type': 'string'},
                                 'bundles': {'type': 'array', 'items': operations, 'maxItems': 8},
                                 'fields': {'type': 'array', 'items': field_schema, 'maxItems': 8}},
                  'required': ['diagnosis', 'bundles', 'fields']}
        # No raw configuration, conversation content, credential values, or full logs sent.
        prompt = json.dumps({'task': 'Propose a minimal DSH startup repair. Only select listed optional bundles. Never invent a command.',
                             'incident': self.incident()['id'], 'reason': self.state.get('reason', 'startup failure'),
                             'dsh_version': m['dsh_version'], 'optional_bundles': self.cfg.get('optional_bundles', []),
                             'enabled_bundles': load(p / 'profile/package.json')['dsh']['profile']['bundles'],
                             'enrolled_scalars': self.cfg.get('yaml_fields', {}), 'diagnostic_codes': self.diagnostic_codes()})
        argv = [self.cfg['claude'], '--safe-mode', '--restricted', '--disable-slash-commands',
                '--no-chrome', '-p', '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--permission-mode', 'dontAsk', '--permission-prompts', 'none', '--no-session-persistence',
                '--max-turns', str(turns), '--max-budget-usd', str(usd), '--output-format', 'json',
                '--json-schema', json.dumps(schema)]
        env, model = claude_context(self.cfg.get('claude_settings', Path.home() / '.claude/settings.json'))
        if model:
            argv.extend(['--model', model])
        with tempfile.TemporaryDirectory(prefix='repair-', dir=self.directory) as work:
            code, out, _ = run_bounded(argv, work, env, prompt, timeout=min(seconds, self.remaining(), self.cfg.get('claude_call_timeout', MAX_SECONDS)), cancel=lambda: getattr(self, 'shutting_down', False) or (self.directory / 'manual-stop').exists())
        value = json.loads(out)
        cost = value.get('total_cost_usd')
        usage = value.get('usage')
        if (isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0 or
                not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('input_tokens', 'output_tokens'))):
            raise Blocked('Claude usage missing; budget remains reserved')
        i = self.incident()
        i['usd'] += cost
        i['accounting_uncertain'] = False
        self.save()
        if code != 0 or value.get('is_error') or value.get('subtype') != 'success' or i['usd'] > MAX_USD:
            detail = redact(str(value.get('errors') or value.get('result') or 'No diagnostic supplied')[:1000])
            self.log('claude-rejected', subtype=value.get('subtype'), exit=code, errors=detail)
            raise Blocked('Claude invocation unsuccessful (' + str(value.get('subtype')) + '): ' + detail)
        proposal = value.get('structured_output')
        if not isinstance(proposal, dict) or set(proposal) != {'diagnosis', 'bundles', 'fields'}:
            raise Blocked('Missing or invalid structured proposal')
        return self.candidate(proposal['bundles'], proposal['fields'])

    def check_claude(self):
        self.require_recovery()
        """Exercise a synthetic repair and contained validation; never publish it."""
        check = Guard(self.directory / 'checks' / uuid.uuid4().hex)
        check.cfg = {**self.cfg, 'repair_enabled': True, 'claude_call_timeout': 120}
        check.state = {'mode': 'observe', 'release': self.state.get('release'),
                       'reason': 'Synthetic check only: git-graph optional bundle cannot import. Disable only @linxin666/dsh-client-ui-git-graph; leave fields empty.'}
        check.save()
        report = {'time': time.time(), 'check_directory': str(check.directory), 'passed': False}
        try:
            rid = check.repair()
            check.validate(rid)
            report.update(passed=True, candidate=rid, published=False,
                          usage=check.state['incident'],
                          settings=str(self.cfg.get('claude_settings', Path.home() / '.claude/settings.json')))
            return report
        finally:
            atomic(self.directory / 'claude-check.json', report)

    def candidate(self, changes, fields=None):
        self.require_recovery()
        fields = [] if fields is None else fields
        if (not isinstance(changes, list) or not isinstance(fields, list) or
                not (changes or fields) or len(changes) > 8 or len(fields) > 8):
            raise Blocked('Empty or oversized repair proposal')
        self.remaining()
        p, m = self.release(verify=True)
        rid = 'repair-' + uuid.uuid4().hex[:12]
        dest = Path(self.cfg['releases']) / rid
        clone(p, dest)
        try:
            pkg = load(dest / 'profile/package.json')
            bundles = pkg['dsh']['profile']['bundles']
            seen = set()
            for change in changes:
                if not isinstance(change, dict) or set(change) != {'bundle', 'enabled'}:
                    raise Blocked('Only bundle enable/disable proposals are accepted')
                name, enabled = change['bundle'], change['enabled']
                if name not in self.cfg.get('optional_bundles', []) or name in seen or type(enabled) is not bool:
                    raise Blocked('Unenrolled or repeated bundle operation')
                seen.add(name)
                original = m['baseline_bundles']
                if name not in original:
                    raise Blocked('Bundle was not enrolled in baseline')
                if not enabled:
                    bundles = [b for b in bundles if b != name]
                elif name not in bundles:
                    bundles = [b for b in original if b == name or b in bundles]
            pkg['dsh']['profile']['bundles'] = bundles
            atomic(dest / 'profile/package.json', pkg)
            changed_files = set()
            for field in fields:
                if not isinstance(field, dict) or set(field) != {'file', 'changes'}:
                    raise Blocked('Invalid scalar proposal')
                name = field['file']
                allowed = self.cfg.get('yaml_fields', {}).get(name)
                if not allowed or name not in m['files'] or name in changed_files:
                    raise Blocked('Unenrolled or repeated configuration file')
                safe_file(self.cfg['home'], name)
                source = dest / 'profile' / Path(name).relative_to('profiles/web') if name.startswith('profiles/web/') else dest / 'files' / name
                request = {'action': 'apply', 'yamlPackage': str(dest / 'runtime/node_modules/yaml/package.json'),
                           'text': source.read_text(), 'changes': field['changes'], 'allowed': allowed}
                code, out, err = run_bounded([m['node'], str(ROOT / 'validate.mjs')], ROOT,
                                             child_env(self.directory), json.dumps(request), timeout=self.remaining(10))
                result = json.loads(out)
                if code or result.get('ok') is not True:
                    raise Blocked('Scalar proposal rejected: ' + redact(result.get('error', err)))
                atomic(source, result['text'], raw=True)
                m['files'][name] = digest(source)
                changed_files.add(name)
            m.update(id=rid, parent=p.name, validated=False, accepted=False)
            m['files']['profiles/web/package.json'] = digest(dest / 'profile/package.json')
            m['artifacts']['profile'] = tree_hash(dest / 'profile')
            atomic(dest / 'release.json', m)
            return rid
        except BaseException:
            shutil.rmtree(dest)
            raise

    @bounded_operation
    def recover(self):
        if (WINDOWS or self.cfg.get('development_mode')) and self.native_busy(self.bridge()):
            raise Blocked('Native operation is active; automatic recovery will not interrupt it')
        i = self.incident()
        if time.time() - i['started'] >= MAX_SECONDS:
            raise Blocked('Incident recovery deadline exceeded')
        self.state['workflow_phase'] = 'recovering'
        self.save()
        self.stop_child()
        if i['restarts'] < self.cfg.get('max_restarts', 2):
            i['restarts'] += 1
            self.state['next_start'] = time.time() + min(60, 5 * 2 ** (i['restarts'] - 1))
            self.save()
            return
        if WINDOWS:
            raise Blocked('Windows supervision restart limit reached; recovery isolation is unavailable')
        good = self.state.get('known_good')
        if good and self.cfg.get('development_mode') and not i['rollback_done']:
            i['rollback_done'] = True
            self.save()
            self.rescue_publish(good)
            return
        if good and good != self.state.get('release') and not i['rollback_done']:
            i['rollback_done'] = True
            self.save()
            self.publish(good, rollback=True)
            return
        # A baseline can repair self-edits even when the selected runtime ID has not changed.
        if good and self.drift() and not i['rollback_done']:
            i['rollback_done'] = True
            self.save()
            self.publish(good, rollback=True)
            return
        if not self.cfg.get('repair_enabled'):
            raise Blocked('Automatic repair disabled; restart limit reached')
        while i['attempts'] < MAX_ATTEMPTS:
            before_attempts = i['attempts']
            try:
                rid = self.repair()
                self.validate(rid)
                self.publish(rid)
                return
            except (Blocked, ValueError, OSError) as e:
                self.log('repair-rejected', reason=e)
                if i['attempts'] == before_attempts or i['accounting_uncertain'] or time.time() - i['started'] >= MAX_SECONDS:
                    break
        raise Blocked('Recovery exhausted; inspect events and repair ledger')

    def step(self, now=None):
        now = time.time() if now is None else now
        command = self.directory / 'control.json'
        if command.exists():
            request = {}
            try:
                request = load(command)
                action = request['action']
                stop = load(self.directory / 'manual-stop', {})
                if stop.get('request_id') and request.get('queued_at', 0) <= stop['requested']:
                    raise Blocked('Cancelled by manual stop')
                self.control(action)
                self.state['last_control'] = {'action': action, 'request_id': request.get('request_id'), 'ok': True, 'time': now}
            except (Blocked, ValueError, OSError, KeyError) as error:
                self.state['last_control'] = {'request_id': request.get('request_id'), 'ok': False, 'error': redact(error), 'time': now}
                self.log('control-rejected', reason=error)
            finally:
                command.unlink(missing_ok=True)
                self.save()
        if (self.directory / 'manual-stop').exists():
            stop = load(self.directory / 'manual-stop', {})
            self.state.update(mode='stopped', health='unknown', reason='Stopped by user')
            self.save()
            try:
                self.stop_child()
            except (Blocked, OSError) as error:
                if stop.get('request_id'):
                    self.state['last_control'] = {'action': 'stop', 'request_id': stop['request_id'], 'ok': False, 'error': redact(error), 'time': now}
                    self.save()
                raise
            if stop.get('request_id'):
                self.state['last_control'] = {'action': 'stop', 'request_id': stop['request_id'], 'ok': True, 'time': now}
        last = self.state.get('last_tick', now)
        self.state['last_tick'] = now
        if now - last > max(90, self.cfg.get('interval', 10) * 3):
            self.state.update(failures=0, wake_grace_until=now + self.cfg.get('startup_grace', 90))
        if self.state.get('mode') != 'manage':
            if self.state.get('mode') != 'stopped':
                self.state.update(health='live' if listening(self.port) else 'unknown', reason='Observation only; no process changes')
            self.save()
            return
        if self.state.get('breaker'):
            return
        if self.child:
            self.child.poll()
        bridge = self.bridge(now) if self.cfg.get('development_mode') else None
        if (WINDOWS or self.cfg.get('development_mode')) and self.native_busy(bridge):
            self.state['busy_since'] = self.state.get('busy_since') or now
            if now - self.state['busy_since'] > self.cfg.get('operation_grace', 600):
                raise Blocked('Native development operation exceeded its deadline; inspect it before recovery')
            self.state.update(workflow_phase='busy', reason='Native installation or hot reload is in progress', failures=0)
            self.save()
            return
        self.state['busy_since'] = None
        record = self.state.get('child')
        if record and not same_process(record):
            intent = (bridge or {}).get('restart_intent') or {}
            if intent.get('kind') in ('restart', 'stop') and now <= intent.get('expires_at', 0):
                self.state.pop('child', None)
                self.save()
                if intent['kind'] == 'stop':
                    self.control('stop')
                else:
                    self.launch()
                return
            self.state.update(health='failed', reason='Owned DSH process exited')
            self.save()
            self.recover()
            return
        if not record:
            if now >= self.state.get('next_start', 0):
                self.launch()
            return
        health, reason = self.probe()
        self.state.update(health=health, reason=reason)
        if health == 'ready':
            self.state['failures'] = 0
            if not self.state.get('ready_since'):
                self.state['ready_since'] = now
            if now - self.state['ready_since'] >= self.cfg.get('accept_window', 60):
                if WINDOWS:
                    self.state.update(workflow_phase='ready', known_good=None, checkpoint_error=WINDOWS_LIMIT)
                elif self.cfg.get('development_mode'):
                    if self.state.get('run_home'):
                        self.observe_rescue()
                    else:
                        self.observe_development(now, bridge)
                elif not self.drift():
                    self.state['known_good'] = self.state['release']
                incident = self.state.get('incident')
                if incident and now - self.state['ready_since'] >= self.cfg.get('incident_reset_after', 600):
                    self.state['last_incident'] = self.state.pop('incident')
        else:
            self.state['ready_since'] = None
            if health != 'failed':
                self.state['failures'] = 0
            grace = max(self.state.get('started', now) + self.cfg.get('startup_grace', 90), self.state.get('wake_grace_until', 0))
            if health == 'failed' and now > grace:
                self.state['failures'] = self.state.get('failures', 0) + 1
                if self.state['failures'] >= self.cfg.get('failure_threshold', 3):
                    self.save()
                    self.recover()
                    return
        self.save()

    def module_state(self):
        path = Path(self.cfg['home']) / 'profiles/web/node_modules'
        if path.is_symlink():
            target = path.resolve()
            if not (target.is_relative_to(Path(self.cfg['releases']).resolve()) or
                    target.is_relative_to((self.directory / 'installed').resolve())):
                raise Blocked('Unowned profile dependency symlink')
            return {'kind': 'link', 'target': os.readlink(path)}
        if path.is_dir():
            return {'kind': 'directory', 'backup': '.dsh-guard-modules-' + uuid.uuid4().hex}
        raise Blocked('Profile dependency directory missing')

    def switch_modules(self, target, previous):
        target = Path(target)
        installed = self.directory / 'installed' / uuid.uuid4().hex / 'node_modules'
        clone(target, installed)
        # Live package-manager writes must never modify a recovery artifact.
        for original in target.rglob('*'):
            if original.is_symlink() and not original.resolve().is_relative_to(target.resolve()):
                resolved = original.resolve()
                if not resolved.is_relative_to(Path(self.cfg['releases']).resolve()):
                    raise Blocked('External dependency link during installation')
                link = installed / original.relative_to(target)
                link.unlink()
                link.symlink_to(resolved, target_is_directory=resolved.is_dir())
        path = Path(self.cfg['home']) / 'profiles/web/node_modules'
        if previous['kind'] == 'directory':
            path.rename(path.parent / previous['backup'])
        temp = path.with_name('.dsh-guard-link-' + uuid.uuid4().hex)
        temp.symlink_to(installed, target_is_directory=True)
        os.replace(temp, path)

    def restore_journal(self):
        self.require_recovery()
        journal = load(self.directory / 'publication.json')
        if not journal:
            return
        if listening(self.port) and not same_process(self.state.get('child')):
            raise Blocked('Unmanaged listener prevents transaction recovery')
        self.stop_child()
        for name, text in journal['backups'].items():
            atomic(safe_file(self.cfg['home'], name), text, raw=True)
        path = Path(self.cfg['home']) / 'profiles/web/node_modules'
        old = journal['modules']
        if old['kind'] == 'link':
            temp = path.with_name('.dsh-guard-restore-' + uuid.uuid4().hex)
            temp.symlink_to(old['target'], target_is_directory=True)
            os.replace(temp, path)
        else:
            backup = path.parent / old['backup']
            if backup.is_dir():
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    raise Blocked('Unexpected dependency directory during rollback')
                backup.rename(path)
            elif not path.is_dir() or path.is_symlink():
                raise Blocked('Dependency rollback backup missing')
        self.state.update(release=journal['previous_release'], installed_hashes=journal['previous_hashes'],
                          known_good=journal.get('previous_known_good'))
        self.save()
        (self.directory / 'publication.json').unlink()
        self.log('transaction-restored')
        if self.state.get('mode') == 'manage' and not self.state.get('breaker'):
            self.launch()

    @bounded_operation
    def publish(self, rid, rollback=False):
        self.require_recovery()
        if self.cfg.get('development_mode'):
            return self.rescue_publish(rid)
        self.remaining()
        p, m = self.release(rid, verify=True)
        if not m.get('validated'):
            raise Blocked('Candidate has no contained runtime validation')
        _, previous = self.release()
        if previous['dsh_version'] != m['dsh_version']:
            raise Blocked('Runtime changes require a separate data migration procedure')
        expected = self.state.get('installed_hashes') or previous['source_hashes']
        current = self.source_hashes(previous)
        if not rollback and current != expected:
            raise Blocked('Concurrent production edit; publication cancelled')
        if any(current.get(key) != m['source_hashes'].get(key) for key in ('shared:cordis.patch.yml', 'shared:presets')):
            raise Blocked('Shared composition/preset drift; no cross-profile rollback')
        if self.state.get('mode') != 'manage':
            raise Blocked('Observe mode cannot publish or stop an unmanaged DSH')
        if listening(self.port) and not same_process(self.state.get('child')):
            raise Blocked('Unmanaged listener prevents publication')
        modules = self.module_state()
        backups = {name: safe_file(self.cfg['home'], name).read_text() for name in m['files']}
        # Journal before stopping/replacing, so crash recovery always has the old set.
        atomic(self.directory / 'publication.json', {'previous_release': self.state.get('release'),
                                                    'previous_hashes': current, 'backups': backups,
                                                    'previous_known_good': self.state.get('known_good'),
                                                    'modules': modules})
        try:
            self.stop_child()
            for name in m['files']:
                source = p / 'profile' / Path(name).relative_to('profiles/web') if name.startswith('profiles/web/') else p / 'files' / name
                atomic(safe_file(self.cfg['home'], name), source.read_text(), raw=True)
            # Dependency replacement is part of the same recoverable journal.
            self.switch_modules(p / 'profile/node_modules', modules)
            self.state.update(release=rid, installed_hashes=self.source_hashes(m))
            self.save()
            self.launch()
            deadline = time.monotonic() + self.cfg.get('startup_grace', 90) + self.cfg.get('accept_window', 60)
            ready_since = None
            while time.monotonic() < deadline:
                self.remaining()
                if self.child:
                    self.child.poll()
                if not same_process(self.state.get('child')):
                    raise Blocked('Published DSH exited')
                if self.probe()[0] == 'ready':
                    ready_since = ready_since or time.monotonic()
                    if time.monotonic() - ready_since >= self.cfg.get('accept_window', 60):
                        if self.drift():
                            raise Blocked('Configuration changed during stability window')
                        self.state.update(known_good=rid, health='ready', ready_since=time.time())
                        self.save()
                        (self.directory / 'publication.json').unlink()
                        self.log('published', release=rid)
                        return
                else:
                    ready_since = None
                time.sleep(.1)
            raise Blocked('Published DSH readiness deadline exceeded')
        except BaseException:
            self.restore_journal()
            raise

    @bounded_operation
    def validate(self, rid):
        self.require_recovery()
        p, m = self.release(rid, verify=True)
        sandbox = Path('/usr/bin/sandbox-exec')
        if not sandbox.exists():
            raise Blocked('Required native containment unavailable')
        m['validated'] = False
        atomic(p / 'release.json', m)
        self.prove_sandbox(m['node'])
        self.remaining()
        with tempfile.TemporaryDirectory(prefix='validation-', dir=self.directory) as tmp:
            work = Path(tmp)
            home = work / 'home'
            profile = home / 'profiles/web'
            profile.parent.mkdir(parents=True)
            # DSH's importer lookup must stop within the scratch tree, before the denied user home.
            atomic(home / 'package.json', {'name': 'dsh-guard-validation', 'private': True})
            clone(p / 'profile', profile)
            # Rebase artifact links when moving the profile away from its sibling runtime.
            for original in (p / 'profile/node_modules').rglob('*'):
                if original.is_symlink():
                    resolved = original.resolve()
                    if not resolved.is_relative_to(p):
                        raise Blocked('Candidate has external dependency link')
                    link = profile / original.relative_to(p / 'profile')
                    link.unlink()
                    link.symlink_to(resolved, target_is_directory=resolved.is_dir())
            atomic(profile / 'cordis.yml', '[]\n', raw=True)
            shared = Path(self.cfg['home']) / 'cordis.patch.yml'
            if shared.exists():
                shutil.copy2(shared, home / 'cordis.patch.yml')
            for name in m['files']:
                if name.startswith('.agent-presets/'):
                    target = home / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p / 'files' / name, target)
            policy = sandbox_profile(work, [p, Path(m['node'])])
            env = child_env(home)
            env.update(HOME=str(home), TMPDIR=str(work))
            bridge_required = '@local/dsh-guard-bridge' in load(profile / 'package.json')['dsh']['profile']['bundles']
            validation_epoch = uuid.uuid4().hex
            if bridge_required:
                (work / 'probe').mkdir()
                env.update(DSH_GUARD_BRIDGE_FILE=str(work / 'probe/bridge.json'), DSH_GUARD_EPOCH=validation_epoch)
            code, out, err = run_bounded([str(sandbox), '-p', policy, m['node'], str(p / m['entry']),
                                          '--profile', 'web', '--dump-config'], work, env, timeout=self.remaining(60))
            if code or not out.strip() or re.search(r'skipp(?:ed|ing)|unmatched|not found', err, re.I):
                raise Blocked('Scratch composition failed: ' + redact(err[-1000:]))
            atomic(p / 'composition.sha256', hashlib.sha256(out.encode()).hexdigest(), raw=True)
            # Source watching is exercised against the real development workspace separately.
            # In a candidate boot, remap only literal watch roots into an empty scratch directory.
            # Keep all other HMR config (including !!js) and reject nonliteral root expressions.
            watch_root = work / 'watch-only'
            watch_root.mkdir()
            watch_code = r'''const fs=require('node:fs');const r=JSON.parse(fs.readFileSync(0,'utf8'));
const Y=require(r.yaml);const options={schema:'core',resolveKnownTags:false,customTags:[{tag:'tag:yaml.org,2002:js',resolve:s=>s}]};
const d=Y.parseDocument(r.text,options);if(d.errors.length)throw new Error('Invalid composed YAML');
const rows=[];Y.visit(d,{Map(_k,n){if(n.get('name')==='@deepseek-ai/dsh-hmr')rows.push(n)}});
if(rows.length>1)throw new Error('Ambiguous HMR instance');const out=Y.parseDocument('[]\n',options);
if(rows.length){const c=rows[0].get('config',true);if(c!==undefined&&!Y.isMap(c))throw new Error('Nonliteral HMR configuration');
if(c){const roots=c.get('root',true);if(roots!==undefined){if(!Y.isSeq(roots)||roots.items.some(x=>!Y.isScalar(x)||typeof x.value!=='string'||x.tag))throw new Error('Cannot contain nonliteral HMR roots');
const cfg=c.clone();cfg.set('root',d.createNode(roots.items.length?[r.root]:[]));const row=d.createNode({id:'hmr'});row.set('config',cfg);out.contents.add(row);}}}
if(!Array.isArray(r.excluded)||r.excluded.length>16||r.excluded.some(id=>typeof id!=='string'||!/^mcp-[a-z0-9][a-z0-9-]{0,99}$/.test(id)))throw Error('Invalid external MCP validation scope');
for(const id of new Set(r.excluded)){const found=[];Y.visit(d,{Map(_k,n){if(n.get('id')===id)found.push(n)}});
if(found.length!==1||found[0].get('name')!=='@deepseek-ai/dsh-mcp-client')throw Error('Excluded row must be one native external MCP client: '+id);
out.contents.add(d.createNode({id,disabled:true}));}
console.log(JSON.stringify({patch:out.toString()}));'''
            rc, watch_out, watch_err = run_bounded([m['node'], '-e', watch_code], work, env,
                json.dumps({'yaml': str(p / 'runtime/node_modules/yaml'), 'text': out, 'root': str(watch_root),
                            'excluded': self.cfg.get('validation_mcp_rows', [])}),
                timeout=self.remaining(10))
            if rc:
                raise Blocked('Cannot safely contain source watching: ' + redact(watch_err[-500:]))
            watch_patch = work / 'validation-watch.patch.yml'
            atomic(watch_patch, json.loads(watch_out)['patch'], raw=True)
            # Full startup uses same containment, no outside network, no production state.
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.close()
            log = work / 'web.log'
            with log.open('wb') as f:
                proc = subprocess.Popen([str(sandbox), '-p', sandbox_profile(work, [p, Path(m['node'])], port),
                                         m['node'], str(p / m['entry']), '--profile', 'web', '--patch', str(watch_patch),
                                         '--port', str(port), '--no-open'],
                                        cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=f,
                                        start_new_session=True)
            try:
                verifier = Guard(work / 'probe')
                verifier.cfg = {**self.cfg, 'port': port, 'development_mode': bridge_required}
                verifier.state = {'child': identity(proc.pid), 'child_log': str(log), 'bridge_epoch': validation_epoch}
                deadline = time.monotonic() + self.remaining(self.cfg.get('validation_timeout', 90))
                while time.monotonic() < deadline and proc.poll() is None:
                    self.remaining()
                    verifier.state['child'] = identity(proc.pid)
                    bridge = verifier.bridge() if bridge_required else None
                    capabilities_ready = not bridge_required or (bridge and bridge.get('phase') == 'idle' and
                        bridge.get('capabilities') and all(x.get('status') in ('ready', 'disabled') for x in bridge['capabilities']))
                    if bridge and bridge.get('phase') == 'error':
                        break  # Immutable candidate has a settled plugin/configuration failure.
                    if verifier.probe()[0] == 'ready' and capabilities_ready:
                        m['validated'] = True
                        m['validated_at'] = time.time()
                        m['validation_excluded_rows'] = self.cfg.get('validation_mcp_rows', [])
                        m['validation'] = ('contained authenticated session/list and scoped Host capability audit' if bridge_required
                                           else 'contained authenticated session/list') + '; external integrations not exercised'
                        atomic(p / 'release.json', m)
                        self.log('validated', release=rid)
                        return
                    time.sleep(1)
                if bridge_required:
                    atomic(self.directory / 'validation-capabilities.json', verifier.bridge() or {'status': 'missing'})
                full = log.read_text()
                detail = redact(full[:64 * 1024] + ('\n...\n' + full[-16 * 1024:] if len(full) > 64 * 1024 else ''))
                atomic(self.directory / 'validation.last.log', detail, raw=True)
                raise Blocked('Composition passed; contained runtime did not become ready; see ' + str(self.directory / 'validation.last.log'))
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGINT)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()

    def status(self):
        record = self.state.get('child')
        alive = same_process(record)
        mode = 'stopped' if (self.directory / 'manual-stop').exists() else self.state.get('mode', 'observe')
        phase = ('stopping' if alive else 'stopped') if mode == 'stopped' else 'blocked' if self.state.get('breaker') else self.state.get('workflow_phase', 'observing')
        bridge = self.bridge()
        events = []
        try:
            path = self.directory / 'events.jsonl'
            with path.open('rb') as f:
                f.seek(max(0, path.stat().st_size - 32768))
                for line in f.read().decode(errors='replace').splitlines()[-40:]:
                    try:
                        events.append(json.loads(redact(line)))
                    except ValueError:
                        continue
        except OSError:
            pass
        return {'mode': mode, 'health': self.state.get('health', 'unknown'),
                'reason': self.state.get('reason'), 'port': self.port, 'listeners': listener_pids(self.port),
                'owned_child_alive': alive, 'release': self.state.get('release'),
                'known_good': self.state.get('known_good'), 'breaker': self.state.get('breaker'),
                'incident': self.state.get('incident'), 'repair_enabled': not WINDOWS and self.cfg.get('repair_enabled', False),
                'repair_blocked_reason': WINDOWS_LIMIT if WINDOWS else self.cfg.get('repair_blocked_reason'),
                'platform': {'name': sys.platform, 'tier': 'supervision-preview' if WINDOWS else 'development',
                             'contained_recovery': not WINDOWS, 'native_windows_accepted': False}, 'last_control': self.state.get('last_control'),
                'control_pending': (self.directory / 'control.json').exists(),
                'workflow': {'mode': 'supervision' if WINDOWS else 'rescue' if self.state.get('run_home') else 'development' if self.cfg.get('development_mode') else 'fixed',
                             'phase': phase,
                             'message': WINDOWS_LIMIT if WINDOWS else self.state.get('checkpoint_error') or self.state.get('reason'),
                             'checkpoint_at': self.state.get('checkpoint_at'), 'pending_changes': self.state.get('pending_changes', False),
                             'bridge': 'connected' if bridge else 'stale' if self.state.get('bridge_seen') else 'missing',
                             'capabilities': (bridge or {}).get('capabilities', [])},
                'guard': {'heartbeat_at': load(self.directory / 'heartbeat.json', {}).get('updated_at'), 'version': '0.3.0'},
                'events': events}


def prepare(g, runtime, node, home, rid):
    """Prepare separate artifacts, with no changes to the live DSH home."""
    g.require_recovery()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', rid):
        raise Blocked('Invalid release ID')
    if g.cfg and Path(g.cfg['home']).resolve() != home.resolve():
        raise Blocked('Preparation cannot change the managed home')
    initial = not g.cfg
    first_release = (not g.state.get('installed_hashes') and not g.state.get('child') and
                     g.state.get('mode', 'observe') == 'observe')
    capture_scope = {'files': [f'profiles/web/{name}' for name in
                              ('package.json', 'cordis.patch.yml', 'pnpm-lock.yaml')]}
    development_before = g.development_fingerprint(capture_scope) if g.cfg.get('development_mode') else None
    target = Path(g.cfg.get('releases', RELEASES)) / rid
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise Blocked('Release already exists')
    target.mkdir()
    # A pnpm tree can link the same package hundreds of times; hash each source once per capture.
    @functools.cache
    def artifact_hash(path):
        return tree_hash(path)
    try:
        clone(Path(runtime) / 'node_modules', target / 'runtime/node_modules')
        entry = 'runtime/node_modules/@deepseek-ai/dsh/lib/bin.js'
        dsh = load(target / 'runtime/node_modules/@deepseek-ai/dsh/package.json')
        if not dsh:
            raise Blocked('Runtime has no DSH package')
        (target / 'profile').mkdir()
        profile = home / 'profiles/web'
        for name in ('package.json', 'cordis.patch.yml', 'pnpm-lock.yaml'):
            shutil.copy2(safe_file(home, 'profiles/web/' + name), target / 'profile' / name)
        clone(profile / 'node_modules', target / 'profile/node_modules')
        # Materialize external local package artifacts without .git, build scripts or their own mutable node_modules.
        modules = target / 'profile/node_modules'
        for link in list(modules.rglob('*')):
            if not link.is_symlink():
                continue
            source = link.resolve()
            if source.is_relative_to(modules):
                continue
            # Relative external links are rooted at the ORIGINAL profile, not the clone.
            source = (profile / 'node_modules' / link.relative_to(modules)).resolve()
            runtime_modules = (Path(runtime) / 'node_modules').resolve()
            if source.is_relative_to(runtime_modules):
                # A previously frozen dependency is already an exact trusted artifact.
                frozen = target / 'runtime/node_modules' / source.relative_to(runtime_modules)
                if not frozen.exists():
                    raise Blocked('Frozen runtime dependency missing')
                link.unlink()
                link.symlink_to(os.path.relpath(frozen, link.parent), target_is_directory=frozen.is_dir())
                continue
            if not (source / 'package.json').is_file():
                raise Blocked('Unknown external link: ' + str(link))
            pkg = load(source / 'package.json')
            package_name = pkg.get('name', '')
            if isinstance(package_name, str) and re.fullmatch(r'(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+', package_name):
                frozen = target / 'runtime/node_modules' / package_name
                if frozen.is_dir() and artifact_hash(source) == artifact_hash(frozen):
                    link.unlink()
                    link.symlink_to(os.path.relpath(frozen, link.parent), target_is_directory=True)
                    continue
            files = pkg.get('files')
            if not isinstance(files, list) or any(not isinstance(f, str) or '*' in f or Path(f).is_absolute() or '..' in Path(f).parts for f in files):
                raise Blocked('External package needs explicit artifact files: ' + str(source))
            link.unlink()
            link.mkdir()
            shutil.copy2(source / 'package.json', link / 'package.json')
            for name in files:
                s = source / name
                if not s.exists():
                    continue
                if not s.resolve().is_relative_to(source) or (s.is_dir() and any(x.is_symlink() and not x.resolve().is_relative_to(source) for x in s.rglob('*'))):
                    raise Blocked('External link inside declared package artifacts')
                d = link / name
                d.parent.mkdir(parents=True, exist_ok=True)
                if s.is_dir():
                    shutil.copytree(s, d, symlinks=False)
                else:
                    shutil.copy2(s, d)
            if pkg.get('dependencies'):
                (link / 'node_modules').mkdir()
                for dep in pkg['dependencies']:
                    candidates = [source / 'node_modules' / dep, profile / 'node_modules' / dep,
                                  target / 'runtime/node_modules' / dep]
                    actual = next((x.resolve() for x in candidates if x.is_dir()), None)
                    if actual is None:
                        raise Blocked('Local artifact dependency missing: ' + dep)
                    # Use the prepared runtime's version, require exact match with installed artifact dependency.
                    frozen = target / 'runtime/node_modules' / dep
                    if not frozen.is_dir() or load(frozen / 'package.json')['version'] != load(actual / 'package.json')['version']:
                        raise Blocked('Local dependency needs a prepared exact artifact: ' + dep)
                    d = link / 'node_modules' / dep
                    d.parent.mkdir(parents=True, exist_ok=True)
                    d.symlink_to(os.path.relpath(frozen, d.parent))
        pkg = load(target / 'profile/package.json')
        for name, spec in pkg.get('dependencies', {}).items():
            installed = load(modules / name / 'package.json')
            if spec.startswith(('link:', 'file:')):
                pkg['dependencies'][name] = 'link:node_modules/' + name
            elif installed:
                pkg['dependencies'][name] = installed['version']
        atomic(target / 'profile/package.json', pkg)
        files = {f'profiles/web/{n}': digest(target / 'profile' / n)
                 for n in ('package.json', 'cordis.patch.yml', 'pnpm-lock.yaml')}
        # Home presets may be shared with 3082: record drift only, do not auto-enroll them.
        manifest = {'id': rid, 'node': str(node.resolve()), 'node_hash': digest(node), 'entry': entry,
                    'dsh_version': dsh['version'], 'files': files, 'validated': False,
                    'baseline_bundles': pkg['dsh']['profile']['bundles'],
                    'artifacts': {'runtime': tree_hash(target / 'runtime'), 'profile': tree_hash(target / 'profile')}}
        if initial:
            g.cfg.update(home=str(home), releases=str(target.parent), port=3080,
                         initial_release=rid, claude=str(Path(shutil.which('claude') or 'claude').absolute()),
                         repair_enabled=False, optional_bundles=OPTIONAL_BUNDLES, yaml_fields={})
        manifest['source_hashes'] = g.source_hashes(manifest)
        if development_before is not None:
            if g.development_fingerprint(manifest) != development_before:
                raise Blocked('Development changed during capture')
            manifest['development_source'] = development_before
        atomic(target / 'release.json', manifest)
        if initial or first_release:
            g.cfg['initial_release'] = rid
            atomic(g.directory / 'config.json', g.cfg)
            g.state.update(mode='observe', release=rid, health='unknown')
            g.save()
        return target
    except BaseException:
        shutil.rmtree(target)
        raise


def main():
    if WINDOWS and os.environ.get('DSH_GUARD_JOB'):
        win.wait_guard_job(os.environ['DSH_GUARD_JOB'])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=STATE)
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('status')
    for command in ('restart', 'checkpoint', 'return-development'):
        subs.add_parser(command)
    check = subs.add_parser('check-claude', help='Synthetic proposal and contained candidate validation; no publication')
    check.add_argument('--enable-repair', action='store_true', help='Enable automatic repair after a successful check')
    run = subs.add_parser('run')
    run.add_argument('--once', action='store_true')
    run.add_argument('--observe', action='store_true')
    subs.add_parser('stop')
    resume = subs.add_parser('resume')
    resume.add_argument('--manage', action='store_true', help='Only when no unmanaged listener exists')
    prep = subs.add_parser('prepare')
    prep.add_argument('--runtime', type=Path, required=True)
    prep.add_argument('--node', type=Path, default=Path(shutil.which('node') or '/usr/bin/node'))
    prep.add_argument('--home', type=Path, help='First enrollment: DSH_HOME or ~/.dsh; later: enrolled home')
    prep.add_argument('--id', required=True)
    val = subs.add_parser('validate')
    val.add_argument('release')
    pub = subs.add_parser('publish')
    pub.add_argument('release')
    args = parser.parse_args()
    os.umask(0o077)
    g = Guard(args.state)
    if args.command == 'status':
        print(json.dumps(g.status(), ensure_ascii=False, indent=2))
        return
    if args.command == 'stop':
        # A busy model/validation operation must not swallow an intentional stop.
        atomic(g.directory / 'manual-stop', {'requested': time.time()})
    with g.lock('runner.lock' if args.command == 'run' else 'operation.lock'):
        if args.command in ('restart', 'checkpoint', 'return-development'):
            g.control(args.command)
        elif args.command == 'check-claude':
            report = g.check_claude()
            if args.enable_repair:
                g.remaining()
                g.cfg['repair_enabled'] = True
                g.cfg.pop('repair_blocked_reason', None)
                atomic(g.directory / 'config.json', g.cfg)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == 'prepare':
            print(prepare(g, args.runtime.resolve(), args.node.resolve(), enrollment_home(args.home, g.cfg), args.id))
        elif args.command == 'validate':
            g.validate(args.release)
        elif args.command == 'publish':
            g.publish(args.release)
        elif args.command == 'stop':
            g.state['mode'] = 'stopped'
            g.save()
            g.stop_child()
        elif args.command == 'resume':
            if args.manage and listening(g.port) and not same_process(g.state.get('child')):
                raise Blocked('Unmanaged listener present; handover must be explicit')
            if args.manage:
                _, m = g.release(verify=True)
                if not g.launch_allowed(m):
                    raise Blocked('Selected release has no contained runtime validation')
                if g.cfg.get('development_mode') and not g.state.get('installed_hashes'):
                    with g.profile_lock():
                        if not m.get('development_source') or g.development_fingerprint() != m['development_source']:
                            raise Blocked('Development changed since first validation; prepare a fresh baseline')
                        g.state['installed_hashes'] = g.source_hashes(m)
            (g.directory / 'manual-stop').unlink(missing_ok=True)
            if g.state.get('incident'):
                g.state['last_incident'] = g.state['incident']
            g.state.update(mode='manage' if args.manage else 'observe', breaker=None, incident=None)
            g.save()
            if args.manage and not WINDOWS and not g.state.get('installed_hashes'):
                g.publish(m['id'])
        else:
            if args.observe and g.state.get('mode') != 'stopped':
                g.state['mode'] = 'observe'
                g.save()
            g._supervising = True
            console = None
            if not args.once and g.cfg.get('console_port'):
                from console import start
                try:
                    console = start(g.directory, int(g.cfg['console_port']), Guard, atomic)
                except OSError as error:
                    g.log('console-unavailable', reason=error)
            stopping = False
            def halt(_sig, _frame):
                nonlocal stopping
                stopping = True
                g.shutting_down = True
            signal.signal(signal.SIGINT, halt)
            signal.signal(signal.SIGTERM, halt)
            while not stopping:
                try:
                    with g.lock():
                        atomic(g.directory / 'heartbeat.json', {'pid': os.getpid(), 'updated_at': time.time(), 'phase': 'tick'})
                        if load(g.directory / 'publication.json'):
                            g.restore_journal()
                        g.step()
                except Blocked as e:
                    # Lock contention is a transient control operation, not an application failure.
                    if not stopping and 'holds the lock' not in str(e):
                        g.breaker(str(e))
                atomic(g.directory / 'heartbeat.json', {'pid': os.getpid(), 'updated_at': time.time(), 'phase': 'tick'})
                if args.once:
                    break
                for _ in range(max(1, int(g.cfg.get('interval', 10) * 10))):
                    if stopping:
                        break
                    time.sleep(.1)


if __name__ == '__main__':
    try:
        main()
    except (Blocked, OSError, ValueError, subprocess.SubprocessError) as error:
        print('dsh-guard: ' + redact(error), file=sys.stderr)
        sys.exit(1)
