"""Installed macOS CLI: use the verified app and the state pinned at enrollment."""
import importlib.util
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
base = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dsh_guard_launcher', base / 'launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
installation, _ = launcher.read_json(base / 'installation.json')
state = Path(installation['state'])
if not state.is_absolute():
    raise SystemExit('dsh-guard: installed state path must be absolute')
if any(arg == '--state' or arg.startswith('--state=') for arg in sys.argv[1:]):
    raise SystemExit('dsh-guard: installed CLI uses its enrolled state directory')
store = base / 'app'
selected = launcher.pointer(store)
try:
    running, _ = launcher.read_json(state / 'launcher.json')
    active = running.get('version')
except (OSError, ValueError):
    active = None
for version in dict.fromkeys([active, selected['current'], selected['previous']]):
    if version not in (selected['current'], selected['previous']) or version is None:
        continue
    try:
        application = launcher.validate(store, version)
    except (launcher.LauncherError, OSError, ValueError):
        continue
    os.execve(sys.executable, [sys.executable, '-B', str(application / 'dsh_guard.py'),
                              '--state', str(state), *sys.argv[1:]], launcher.guard_environment())
raise SystemExit('dsh-guard: no verified installed application is available')
