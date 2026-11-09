"""Only temporary stores/state and real, tiny fixture guards; no live DSH access.

Run: python3 -m unittest discover -s /path/to/dsh-guard -p test_launcher.py -v
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).with_name('launcher.py')
spec = importlib.util.spec_from_file_location('guard_launcher', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

BOOTSTRAP = '''import importlib.util,sys
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location('launcher',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.STARTUP_GRACE=.4;m.STALE_SECONDS=.3;m.POLL_SECONDS=.015
m.TERM_SECONDS=.1;m.KILL_SECONDS=.4;m.BACKOFF_SECONDS=.25;m.MAX_BACKOFF=.6
try: sys.exit(m.main(['run','--store',sys.argv[2],'--state',sys.argv[3]]))
except (m.LauncherError,OSError,ValueError) as e:
 print(str(e),file=sys.stderr);sys.exit(1)
'''

# The fake DSH intentionally shares its guard's process group. Killing a group
# instead of the owned guard PID stops its independent progress and fails a test.
FAKE_DSH = '''import json,os,pathlib,sys,time
p=pathlib.Path(sys.argv[1])/'fixture-dsh.json'
while True:
 t=p.with_suffix('.tmp');t.write_text(json.dumps({'pid':os.getpid(),'at':time.time()}));t.replace(p)
 time.sleep(.02)
'''

GUARD = '''from development import LABEL
import json,os,pathlib,signal,subprocess,sys,time
state=pathlib.Path(sys.argv[sys.argv.index('--state')+1])
mode=MODE
signal.signal(signal.SIGTERM,signal.SIG_IGN if mode=='operation-sliding' else lambda *_:sys.exit(0))
with (state/'fixture-starts.jsonl').open('a') as f:
 f.write(json.dumps({'pid':os.getpid(),'at':time.time(),'label':LABEL,'argv':sys.argv[1:],'cwd':os.getcwd(),
                     'env_keys':sorted(os.environ),'sid':os.getsid(0)})+'\\n')
if mode=='exit': sys.exit(9)
if mode=='operation-sliding' and not (state/'fixture-dsh.json').exists():
 subprocess.Popen([sys.executable,'-c',CHILD,str(state)],stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
initial=time.time()
while True:
 if mode!='missing':
  value={'pid':os.getpid(),'updated_at':initial if mode=='frozen-tick' else time.time(),
         'phase':'repair' if mode=='operation-sliding' else 'tick'}
  if mode=='operation-sliding': value['deadline']=time.time()+.18
  p=state/'heartbeat.json';t=p.with_suffix('.tmp');t.write_text(json.dumps(value));t.replace(p)
 time.sleep(.02)
'''


@unittest.skipIf(os.name == 'nt', 'POSIX fixture sessions/signals; use test_windows.py on Windows')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='launcher-test-')
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.store = self.root / 'store'
        self.state = self.root / 'state'
        self.launchers = []
        self.fixture_children = set()

    def tearDown(self):
        for child in self.launchers:
            self.stop(child)
        marker = self.state / 'fixture-dsh.json'
        if marker.exists():
            self.fixture_children.add(json.loads(marker.read_text())['pid'])
        for pid in self.fixture_children:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.temp.cleanup()

    def application(self, label='good', mode='healthy'):
        self.source.mkdir(exist_ok=True)
        for name in mod.FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}' if name.endswith('package.json') else 'fixture\n')
        (self.source / 'development.py').write_text('LABEL = ' + repr(label) + '\n')
        (self.source / 'dsh_guard.py').write_text('MODE = ' + repr(mode) + '\nCHILD = ' + repr(FAKE_DSH) + '\n' + GUARD)
        return mod.install(self.source, self.store)

    def launch(self):
        child = subprocess.Popen([sys.executable, '-c', BOOTSTRAP, str(SOURCE),
                                  str(self.store), str(self.state)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={**os.environ, 'ANTHROPIC_API_KEY': 'fixture-secret-never-forward',
                                      'PYTHONPATH': str(self.source)})
        self.launchers.append(child)
        return child

    def stop(self, child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        return child.communicate()

    def read(self, name='launcher.json'):
        try:
            return json.loads((self.state / name).read_text())
        except FileNotFoundError:
            return {}

    def wait_for(self, predicate, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(.015)
        self.fail('Timed out; launcher status: ' + repr(self.read()))

    def ready(self, version):
        return self.wait_for(lambda: self.read().get('version') == version and
                            self.read().get('phase') == 'tick' and self.read().get('status') == 'running')

    def starts(self):
        path = self.state / 'fixture-starts.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def corrupt(self, path, text):
        path.chmod(0o600)
        path.write_text(text)

    def test_explicit_install_snapshots_allowlist_and_is_idempotent(self):
        good = self.application()
        first = mod.pointer(self.store)
        version = mod.validate(self.store, good)
        manifest = json.loads((version / 'manifest.json').read_text())
        self.assertEqual(set(manifest['files']), set(mod.FILES))
        self.assertEqual(good, hashlib.sha256(mod.encoded(manifest)).hexdigest())
        before = (version / 'manifest.json').stat().st_mtime_ns
        (self.source / 'credentials.json').write_text('never-copy-this')
        self.assertEqual(mod.install(self.source, self.store), good)
        self.assertEqual(mod.pointer(self.store), first)
        self.assertEqual((version / 'manifest.json').stat().st_mtime_ns, before)
        second = self.application('second')
        self.assertNotEqual(second, good)
        self.assertEqual(mod.pointer(self.store)['previous'], good)
        self.assertFalse((mod.validate(self.store, second) / 'credentials.json').exists())
        self.assertEqual(mod.install(self.source, self.store), second)
        self.assertEqual(mod.pointer(self.store)['previous'], good)
        self.assertEqual((version / 'manifest.json').stat().st_mtime_ns, before)

    def test_incomplete_or_linked_source_cannot_replace_current(self):
        good = self.application()
        path = self.source / 'console.py'
        path.unlink()
        with self.assertRaisesRegex(mod.LauncherError, 'Missing'):
            mod.install(self.source, self.store)
        path.symlink_to(self.source / 'development.py')
        with self.assertRaisesRegex(mod.LauncherError, 'Symlink'):
            mod.install(self.source, self.store)
        path.unlink()
        path.write_text('fixture\n')
        shutil.rmtree(self.source / 'ui')
        (self.source / 'ui').symlink_to(self.store, target_is_directory=True)
        with self.assertRaisesRegex(mod.LauncherError, 'Symlink'):
            mod.install(self.source, self.store)
        self.assertEqual(mod.pointer(self.store)['current'], good)
        self.assertEqual(list((self.store / 'versions').glob('.stage-*')), [])

    def test_optional_development_module_and_external_store(self):
        self.application()
        (self.source / 'development.py').unlink()
        (self.source / 'LICENSE').unlink()  # Older versions also lack this optional notice.
        (self.source / 'dsh_guard.py').write_text('# Core implementation has no development import.\n')
        version = mod.install(self.source, self.store)
        root = mod.validate(self.store, version)
        self.assertFalse((root / 'development.py').exists())
        self.assertEqual(set(json.loads((root / 'manifest.json').read_text())['files']), mod.REQUIRED_FILES)
        with self.assertRaisesRegex(mod.LauncherError, 'outside'):
            mod.install(self.source, self.source / 'store')

    def test_existing_immutable_release_is_not_repaired_in_place(self):
        good = self.application()
        file = self.store / 'versions' / good / 'console.py'
        self.corrupt(file, 'tampered')
        with self.assertRaisesRegex(mod.LauncherError, 'hash mismatch'):
            mod.install(self.source, self.store)
        self.assertEqual(file.read_text(), 'tampered')
        self.assertEqual(mod.pointer(self.store)['current'], good)

    def test_source_deletion_safe_environment_and_manual_stop_preserved(self):
        good = self.application()
        shutil.rmtree(self.source)
        self.state.mkdir()
        (self.state / 'manual-stop').write_text('manual intent')
        (self.state / 'state.json').write_text('{"mode":"stopped"}')
        child = self.launch()
        self.ready(good)
        started = self.starts()[0]
        self.assertEqual(started['argv'], ['--state', str(self.state), 'run'])
        self.assertEqual(started['cwd'], str(self.store / 'versions' / good))
        self.assertEqual(started['sid'], started['pid'])
        self.assertNotIn('ANTHROPIC_API_KEY', started['env_keys'])
        self.assertNotIn('PYTHONPATH', started['env_keys'])
        self.stop(child)
        self.assertEqual(child.returncode, 0)
        self.assertEqual((self.state / 'manual-stop').read_text(), 'manual intent')
        self.assertEqual(self.read('state.json'), {'mode': 'stopped'})
        self.assertEqual(self.read()['status'], 'stopped')
        self.assertFalse(list((self.store / 'versions' / good).rglob('__pycache__')))
        self.assertNotIn('fixture-secret-never-forward', (self.state / 'launcher.events.jsonl').read_text())
        self.assertEqual(mod.validate(self.store, good), self.store / 'versions' / good)

    def test_tampered_active_file_falls_back_without_executing_it(self):
        good = self.application()
        bad = self.application('new')
        file = self.store / 'versions' / bad / 'dsh_guard.py'
        self.corrupt(file, 'raise RuntimeError("corrupt code must never run")\n')
        self.launch()
        self.ready(good)
        self.assertEqual([s['label'] for s in self.starts()], ['good'])
        self.assertEqual(self.read()['attempts'], 1)
        self.assertIn('version-invalid', (self.state / 'launcher.events.jsonl').read_text())

    def test_tampered_manifest_cannot_bless_modified_code(self):
        good = self.application()
        bad = self.application('new')
        root = self.store / 'versions' / bad
        self.corrupt(root / 'console.py', 'changed')
        manifest = json.loads((root / 'manifest.json').read_text())
        manifest['files']['console.py'] = mod.digest(root / 'console.py')
        self.corrupt(root / 'manifest.json', mod.encoded(manifest).decode())
        self.launch()
        self.ready(good)
        self.assertEqual([s['label'] for s in self.starts()], ['good'])

    def test_both_invalid_versions_block_without_launch(self):
        good = self.application()
        bad = self.application('new')
        for version in (good, bad):
            self.corrupt(self.store / 'versions' / version / 'console.py', 'tampered')
        child = self.launch()
        child.wait(timeout=3)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(self.read()['status'], 'blocked')
        self.assertEqual(self.read()['attempts'], 0)
        self.assertEqual(self.starts(), [])

    def test_failure_budget_falls_back_and_persists_across_launcher_restart(self):
        good = self.application()
        bad = self.application('bad', 'exit')
        first = self.launch()
        self.ready(good)
        self.assertEqual([s['label'] for s in self.starts()], ['bad'] * 3 + ['good'])
        self.assertEqual(mod.pointer(self.store)['current'], bad)
        self.stop(first)
        second = self.launch()
        self.wait_for(lambda: self.read().get('attempts') == 5)
        self.ready(good)
        self.assertEqual([s['label'] for s in self.starts()], ['bad'] * 3 + ['good', 'good'])
        self.stop(second)

    def test_retry_accounting_and_backoff_survive_launcher_restart(self):
        bad = self.application('bad', 'exit')
        first = self.launch()
        self.wait_for(lambda: self.read().get('status') == 'backoff' and self.read().get('attempts') == 1)
        self.stop(first)
        accounting = self.read()
        self.assertEqual(len(accounting['failures']), 1)
        self.assertGreater(accounting['next_start'], accounting['failures'][0]['at'])
        second = self.launch()
        second.wait(timeout=5)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(self.read()['status'], 'blocked')
        self.assertEqual(self.read()['attempts'], 3)
        self.assertEqual(self.read()['version'], bad)
        self.assertEqual(len(self.starts()), 3)
        self.assertGreaterEqual(self.starts()[1]['at'], accounting['next_start'])

    def test_interrupted_attempt_counts_without_trusting_saved_pid(self):
        good = self.application()
        bad = self.application('bad', 'exit')
        sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.launchers.append(sentinel)
        self.state.mkdir()
        mod.atomic(self.state / 'launcher.json', {
            'schema': 1, 'current': bad, 'previous': good, 'version': bad, 'attempts': 2,
            'failures': [{'at': time.time(), 'version': bad}] * 2, 'next_start': 0,
            'inflight': True, 'guard_pid': sentinel.pid})
        self.launch()
        self.ready(good)
        self.assertIsNone(sentinel.poll())
        self.assertEqual(self.read()['attempts'], 3)
        self.assertEqual([s['label'] for s in self.starts()], ['good'])
        self.assertIn('launcher-interrupted', (self.state / 'launcher.events.jsonl').read_text())

    def test_failed_fallback_blocks_without_bouncing_to_current(self):
        previous = self.application('old-bad', 'exit')
        self.application('new-bad', 'exit')
        child = self.launch()
        child.wait(timeout=5)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(self.read()['status'], 'blocked')
        self.assertEqual(self.read()['version'], previous)
        self.assertEqual(self.read()['attempts'], 6)
        self.assertEqual([s['label'] for s in self.starts()], ['new-bad'] * 3 + ['old-bad'] * 3)

    def test_missing_and_thread_refreshed_frozen_tick_are_not_healthy(self):
        for mode, reason in [('missing', 'heartbeat-missing'), ('frozen-tick', 'heartbeat-stale')]:
            with self.subTest(mode=mode):
                good = self.application('good-' + mode)
                self.application('bad-' + mode, mode)
                child = self.launch()
                self.ready(good)
                self.assertEqual([s['label'] for s in self.starts()][-4:], ['bad-' + mode] * 3 + ['good-' + mode])
                self.assertIn(reason, (self.state / 'launcher.events.jsonl').read_text())
                self.stop(child)

    def test_sliding_operation_deadline_sigkill_preserves_guard_child(self):
        good = self.application()
        self.application('hung', 'operation-sliding')
        child = self.launch()
        marker = self.wait_for(lambda: self.read('fixture-dsh.json'))
        self.fixture_children.add(marker['pid'])
        self.ready(good)
        before = self.read('fixture-dsh.json')
        self.wait_for(lambda: self.read('fixture-dsh.json').get('at', 0) > before['at'])
        self.assertEqual(self.read('fixture-dsh.json')['pid'], marker['pid'])
        for started in self.starts()[:3]:
            with self.assertRaises(ProcessLookupError):
                os.kill(started['pid'], 0)
        self.assertEqual([s['label'] for s in self.starts()], ['hung'] * 3 + ['good'])
        self.assertIn('operation-deadline', (self.state / 'launcher.events.jsonl').read_text())
        self.stop(child)
        before = self.read('fixture-dsh.json')['at']
        self.wait_for(lambda: self.read('fixture-dsh.json').get('at', 0) > before)

    def test_runner_lock_rejects_duplicate_without_touching_owner_status(self):
        good = self.application()
        first = self.launch()
        self.ready(good)
        second = self.launch()
        second.wait(timeout=3)
        _, err = self.stop(second)
        self.assertEqual(second.returncode, 1)
        self.assertIn('lock', err)
        self.assertIsNone(first.poll())
        self.assertEqual(self.read()['pid'], first.pid)
        self.assertEqual(self.read()['attempts'], 1)
        self.assertEqual(len(self.starts()), 1)

    def test_unlisted_or_linked_release_file_is_rejected(self):
        good = self.application()
        root = self.store / 'versions' / good
        extra = root / 'json.py'
        extra.write_text('raise RuntimeError("must not import")')
        with self.assertRaisesRegex(mod.LauncherError, 'Unexpected'):
            mod.validate(self.store, good)
        extra.unlink()
        target = root / 'console.py'
        target.unlink()
        target.symlink_to(self.source / 'console.py')
        with self.assertRaisesRegex(mod.LauncherError, 'Symlink'):
            mod.validate(self.store, good)

    def test_cli_install_requires_complete_application(self):
        self.application()
        other = self.root / 'cli-store'
        done = subprocess.run([sys.executable, str(SOURCE), 'install', '--source', str(self.source),
                               '--store', str(other)], capture_output=True, text=True, timeout=3)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), mod.pointer(other)['current'])


class HeartbeatTests(unittest.TestCase):
    def check(self, watcher, value, now, monotonic):
        with patch.object(mod, 'read_json', return_value=(value, now)):
            return watcher.check(Path('/unused-fixture-heartbeat'), now, monotonic)

    def test_bounded_deadline_is_pinned_until_main_tick(self):
        watcher = mod.Heartbeat(42, 100, 0)
        value = {'pid': 42, 'updated_at': 100, 'phase': 'repair', 'deadline': 1300}
        self.assertIsNone(self.check(watcher, value, 100, 0))
        for now in (200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200):
            value.update(updated_at=now, deadline=now + 1200)
            self.assertIsNone(self.check(watcher, value, now, now - 100))
        value.update(updated_at=1300, deadline=2500)
        self.assertEqual(self.check(watcher, value, 1300, 1200), 'operation-deadline')
        value = {'pid': 42, 'updated_at': 1301, 'phase': 'tick'}
        self.assertIsNone(self.check(watcher, value, 1301, 1201))
        self.assertIsNone(watcher.operation_until)

    def test_invalid_or_foreign_heartbeat_does_not_extend_startup(self):
        invalid = [None, [], {'pid': 999, 'updated_at': 100, 'phase': 'tick'},
                   {'pid': 42, 'updated_at': 99, 'phase': 'tick'},
                   {'pid': 42, 'updated_at': float('nan'), 'phase': 'tick'},
                   {'pid': 42, 'updated_at': 100, 'phase': 'repair'},
                   {'pid': 42, 'updated_at': 100, 'phase': 'repair', 'deadline': 1301},
                   {'pid': 42, 'updated_at': 100, 'phase': 'repair', 'deadline': True},
                   {'pid': 42, 'updated_at': 100, 'phase': 'tick?token=secret'}]
        for value in invalid:
            with self.subTest(value=value):
                watcher = mod.Heartbeat(42, 100, 0)
                self.assertIsNone(self.check(watcher, value, 100, 0))
                self.assertEqual(self.check(watcher, value, 220, 120), 'heartbeat-missing')

    def test_refreshing_file_without_main_progress_expires(self):
        watcher = mod.Heartbeat(42, 100, 0)
        value = {'pid': 42, 'updated_at': 100, 'phase': 'tick'}
        self.assertIsNone(self.check(watcher, value, 100, 0))
        self.assertIsNone(self.check(watcher, value, 279, 179))
        self.assertEqual(self.check(watcher, value, 280, 180), 'heartbeat-stale')


if __name__ == '__main__':
    unittest.main()
