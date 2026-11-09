"""Private installed Windows command; selects a verified app outside its sources."""
import importlib.util
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
base = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('guard_bootstrap', base / 'launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
if any(arg == '--state' or arg.startswith('--state=') for arg in sys.argv[1:]):
    raise SystemExit('dsh-guard: installed CLI uses its enrolled state directory')
store = base / 'app'
selected = launcher.pointer(store)
try:
    running, _ = launcher.read_json(base / 'state/launcher.json')
    active = running.get('version')
except (OSError, ValueError):
    active = None
for version in dict.fromkeys([active, selected['current'], selected['previous']]):
    if version is None or version not in (selected['current'], selected['previous']):
        continue
    try:
        application = launcher.validate(store, version)
    except (launcher.LauncherError, OSError, ValueError):
        continue
    # Windows execv starts a replacement process; preserve its actual exit code.
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, '-B', str(application / 'dsh_guard.py'),
                                      '--state', str(base / 'state'), *sys.argv[1:]],
                                     cwd=application, env=launcher.guard_environment()))
raise SystemExit('dsh-guard: no verified installed application is available')
