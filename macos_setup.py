"""Generate an observation-only macOS install; never load launchd or alter DSH."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
LABEL = 'org.dsh-guard.supervisor'


def module(name):
    spec = importlib.util.spec_from_file_location('setup_' + name, ROOT / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def private_directory(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('Linked installation directory: ' + str(item))
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('Use a new or empty installation directory: ' + str(path))


def enroll(runtime, node=None, home=None, base=None, state=None, bin_dir=None, plist=None,
           development=False, port=3080, console_port=3081):
    if sys.platform != 'darwin' or sys.version_info < (3, 11):
        raise RuntimeError('Use macOS with Python 3.11 or later')
    if not (1 <= port <= 65535 and 1 <= console_port <= 65535 and port != console_port):
        raise ValueError('Choose two different valid loopback ports')
    guard, launcher = module('dsh_guard'), module('launcher')
    home = guard.enrollment_home(home)
    runtime = Path(runtime).expanduser().resolve(strict=True)
    node = Path(node or shutil.which('node') or '').expanduser().resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    if not node.is_file() or not os.access(node, os.X_OK):
        raise ValueError('Select an executable Node binary with --node')
    version_text = subprocess.run([str(node), '--version'], capture_output=True, text=True,
                                  timeout=10, check=True, env={'PATH': os.defpath}).stdout.strip()
    if not version_text.startswith('v') or not version_text[1:].split('.')[0].isdigit() or tuple(int(p) for p in version_text[1:].split('.')[:3]) < (24, 9, 0):
        raise ValueError('DSH 0.2.2-rc.1 requires native Node.js 24.9.0 or later')
    entry = runtime / 'node_modules/@deepseek-ai/dsh/lib/bin.js'
    package = guard.load(entry.parent.parent / 'package.json', {})
    if package.get('name') != '@deepseek-ai/dsh' or package.get('version') != '0.2.2-rc.1' or not entry.is_file():
        raise ValueError('This release supports a persistent DSH 0.2.2-rc.1 runtime')
    profile = home / 'profiles/web'
    for name in ('package.json', 'cordis.patch.yml', 'pnpm-lock.yaml'):
        guard.safe_file(home, 'profiles/web/' + name)
    if not (profile / 'node_modules').is_dir():
        raise ValueError('Prepare the native web profile dependencies first')
    if json.loads((profile / 'cordis.yml').read_text(encoding='utf-8')) != []:
        raise ValueError('Profile cordis.yml must contain []; edit cordis.patch.yml instead')
    if development and '@local/dsh-guard-bridge' not in guard.load(profile / 'package.json').get('dsh', {}).get('profile', {}).get('bundles', []):
        raise ValueError('Install the optional Host bridge with native plugin management before --development')
    base = Path(base or guard.RELEASES.parent).expanduser().absolute()
    state = Path(state or guard.STATE).expanduser().absolute()
    bin_dir = Path(bin_dir or Path.home() / '.local/bin').expanduser().absolute()
    plist = Path(plist or Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')).expanduser().absolute()
    for path in (base, state, bin_dir, plist):
        if '..' in path.parts:
            raise ValueError('Installation paths must not contain parent traversal')
        for protected in (ROOT, home, runtime):
            if path == protected or protected in path.parents or path in protected.parents:
                raise ValueError('Install outside the source, DSH home and runtime')
    if base == state or base in state.parents or state in base.parents:
        raise ValueError('Application and state directories must be separate')
    private_directory(base)
    private_directory(state)
    command = bin_dir / 'dsh-guard'
    for path in (command, plist):
        if path.exists() or path.is_symlink():
            raise ValueError('Refusing to replace an existing command or LaunchAgent: ' + str(path))
        for parent in path.parents:
            if parent.is_symlink():
                raise ValueError('Linked command or LaunchAgent parent')
    # No process launch, runtime copy or DSH profile write occurs during enrollment.
    for directory in (base, state, bin_dir, plist.parent):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (base, state):
        directory.chmod(0o700)
    version = launcher.install(ROOT, base / 'app')
    for name in ('launcher.py', 'command.py'):
        with (ROOT / name).open('rb') as src, (base / name).open('xb') as dst:
            shutil.copyfileobj(src, dst)
        (base / name).chmod(0o500)
    claude = shutil.which('claude')
    settings = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser().resolve() / 'settings.json'
    config = {'home': str(home), 'releases': str(base / 'releases'), 'port': port,
              'console_port': console_port, 'development_mode': development,
              'repair_enabled': False, 'optional_bundles': [], 'yaml_fields': {},
              'claude_settings': str(settings), 'validation_mcp_rows': []}
    if claude:
        config['claude'] = str(Path(claude).resolve())
    guard.atomic(state / 'config.json', config)
    guard.atomic(state / 'state.json', {'mode': 'observe', 'health': 'unknown'})
    launcher.atomic(base / 'installation.json', {'schema': 1, 'state': str(state),
                    'python': str(python), 'node': str(node), 'runtime': str(runtime),
                    'version': version, 'label': LABEL, 'plist': str(plist),
                    'command': str(command), 'mode': 'observe',
                    'bootstrap_sha256': {n: launcher.digest(base / n) for n in ('launcher.py', 'command.py')}})
    path_entries = [str(node.parent)]
    if claude:
        path_entries.append(str(Path(claude).absolute().parent))
    if os.environ.get('PNPM_HOME') and Path(os.environ['PNPM_HOME']).is_absolute():
        path_entries.append(os.environ['PNPM_HOME'])
    path_entries.extend(p for p in os.environ.get('PATH', os.defpath).split(os.pathsep) if Path(p).is_absolute())
    path_entries.extend(('/usr/bin', '/bin', '/usr/sbin', '/sbin'))
    job = {'Label': LABEL, 'ProgramArguments': [str(python), '-I', str(base / 'launcher.py'),
           'run', '--store', str(base / 'app'), '--state', str(state)],
           'WorkingDirectory': str(base), 'EnvironmentVariables': {'HOME': str(Path.home()),
           'PATH': os.pathsep.join(dict.fromkeys(path_entries)), 'LANG': 'en_US.UTF-8'},
           'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
           'ProcessType': 'Background', 'Umask': 0o077,
           'StandardOutPath': str(state / 'launchd.out.log'),
           'StandardErrorPath': str(state / 'launchd.err.log')}
    with plist.open('xb') as stream:
        stream.write(plistlib.dumps(job))
    plist.chmod(0o600)
    with command.open('x', encoding='utf-8') as stream:
        stream.write('#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' -I ' +
                     shlex.quote(str(base / 'command.py')) + ' "$@"\n')
    command.chmod(0o700)
    return {'base': str(base), 'state': str(state), 'command': str(command),
            'plist': str(plist), 'version': version, 'mode': 'observe',
            'launchd_loaded': False, 'release_prepared': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    for name in ('node', 'home', 'base', 'state', 'bin-dir', 'plist'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--development', action='store_true')
    parser.add_argument('--port', type=int, default=3080)
    parser.add_argument('--console-port', type=int, default=3081)
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(enroll(**vars(args)), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit('dsh-guard enrollment: ' + str(error))
