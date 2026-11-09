"""Enrollment used by Install-DshGuard.ps1; never edits a DSH profile or takes over a listener."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


def read_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enroll(source, base, home, runtime, node, port=3080, console_port=3081):
    if os.name != 'nt':
        raise RuntimeError('Enrollment must run on native Windows with Windows Python and Node')
    if sys.version_info < (3, 11):
        raise RuntimeError('Python 3.11 or later is required')
    if not (1 <= port <= 65535 and 1 <= console_port <= 65535 and port != console_port):
        raise ValueError('Choose two different valid loopback ports')
    source, base, home, runtime, node = (Path(p).resolve() for p in (source, base, home, runtime, node))
    if base == source or source in base.parents:
        raise ValueError('Install outside the source directory')
    if (base / 'state/config.json').exists() or (base / 'launcher.py').exists():
        raise ValueError('Existing installation: refusing to overwrite its configuration or bootstrap')
    win = read_module('enrollment_windows', source / 'windows_platform.py')
    launcher = read_module('enrollment_launcher', source / 'launcher.py')
    entry = (runtime / 'node_modules/@deepseek-ai/dsh/lib/bin.js').resolve()
    package = entry.parent.parent / 'package.json'
    profile = home / 'profiles/web'
    # Validate all executable/config paths before creating the installed application.
    for path in (node, entry, package, profile / 'package.json', profile / 'cordis.patch.yml'):
        with os.fdopen(win.open_regular(path, os.O_RDONLY), 'rb'):
            pass
    if node.suffix.lower() != '.exe':
        raise ValueError('Node must be an absolute native .exe; command wrappers are unsupported')
    version_text = subprocess.run([str(node), '--version'], capture_output=True, text=True,
                                  timeout=10, check=True, env=win.environment()).stdout.strip()
    if not version_text.startswith('v') or tuple(int(p) for p in version_text[1:].split('.')[:3]) < (24, 9, 0):
        raise ValueError('DSH 0.2.2-rc.1 requires native Node.js 24.9.0 or later')
    with package.open(encoding='utf-8') as stream:
        dsh = json.load(stream)
    if dsh.get('name') != '@deepseek-ai/dsh' or dsh.get('version') != '0.2.2-rc.1':
        raise ValueError('This preview targets DSH 0.2.2-rc.1; unknown data migrations are unsupported')
    python = Path(sys.executable).resolve()
    pythonw = python.with_name('pythonw.exe')
    if not pythonw.is_file():
        raise ValueError('A normal Windows Python installation with pythonw.exe is required')
    base.mkdir(parents=True, exist_ok=True)
    state = base / 'state'
    state.mkdir()
    version = launcher.install(source, base / 'app')
    for name in ('launcher.py', 'windows_platform.py', 'windows_command.py'):
        with os.fdopen(win.open_regular(source / name, os.O_RDONLY), 'rb') as src, (base / name).open('xb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
    rid = 'windows-live-' + uuid.uuid4().hex[:12]
    release = base / 'releases' / rid
    release.mkdir(parents=True)
    manifest = {'id': rid, 'platform': 'win32', 'launch_only': True, 'validated': False,
                'node': str(node), 'node_hash': launcher.digest(node),
                'entry': str(entry), 'entry_hash': launcher.digest(entry),
                'dsh_version': dsh['version'], 'artifacts': {}, 'files': {}}
    launcher.atomic(release / 'release.json', manifest)
    launcher.atomic(state / 'config.json', {
        'platform_mode': 'windows-supervision-preview', 'home': str(home),
        'releases': str(base / 'releases'), 'initial_release': rid,
        'port': port, 'console_port': console_port, 'repair_enabled': False,
        'development_mode': False, 'interval': 10, 'startup_grace': 90,
        'max_restarts': 2, 'stop_timeout': 60,
        'repair_blocked_reason': 'Windows candidate containment is not implemented',
    })
    launcher.atomic(state / 'state.json', {'mode': 'observe', 'health': 'unknown', 'release': rid})
    startup = {'python': str(python), 'pythonw': str(pythonw),
               'working_directory': str(base), 'state': str(state), 'version': version,
               'arguments': subprocess.list2cmdline(['-I', str(base / 'launcher.py'), 'run',
                                                    '--store', str(base / 'app'), '--state', str(state)]),
               'bootstrap_sha256': {n: launcher.digest(base / n) for n in
                                    ('launcher.py', 'windows_platform.py', 'windows_command.py')},
               'native_windows_acceptance': 'not-run', 'recovery_enabled': False}
    launcher.atomic(base / 'installation.json', startup)
    # A PowerShell entry preserves Unicode and spaces without cmd.exe interpolation.
    executable = str(python).replace("'", "''")
    (base / 'dsh-guard.ps1').write_text(
        "$ErrorActionPreference = 'Stop'\n& '" + executable +
        "' -I (Join-Path $PSScriptRoot 'windows_command.py') @args\nexit $LASTEXITCODE\n",
        encoding='utf-8-sig')
    return {'base': str(base), 'version': version, 'mode': 'observe',
            'native_windows_acceptance': 'not-run', 'recovery_enabled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'base', 'home', 'runtime', 'node'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--port', type=int, default=3080)
    parser.add_argument('--console-port', type=int, default=3081)
    args = parser.parse_args()
    result = enroll(args.source, args.base, args.home, args.runtime, args.node, args.port, args.console_port)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit('dsh-guard enrollment: ' + str(error))
