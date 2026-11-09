"""Run: python3 -m unittest discover -s <this directory> -v"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).with_name('dsh_guard.py')
spec = importlib.util.spec_from_file_location('dsh_guard', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAKE_DSH = '''import http.server,json,signal,sys
port=int(sys.argv[sys.argv.index('--port')+1])
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  if self.path.startswith('/?token='):
   self.send_response(303); self.send_header('Set-Cookie','guard_test=ok; Path=/; HttpOnly'); self.send_header('Location','/'); self.end_headers()
  else:
   self.send_response(200 if self.headers.get('Cookie') else 401); self.end_headers(); self.wfile.write(b'fixture')
 def do_POST(self):
  value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  good=value['payload']=={'args':{'_request':{}}} and bool(self.headers.get('Cookie'))
  self.send_response(200); self.end_headers()
  self.wfile.write(json.dumps({'type':'server-response','rpcId':value['rpcId'],'result':{'ok':good,'value':{'items':[]}}}).encode())
server=http.server.HTTPServer(('127.0.0.1',port),Handler)
print('http://127.0.0.1:'+str(port)+'/?token=fixture_token',flush=True)
signal.signal(signal.SIGINT,lambda *_:sys.exit(0))
server.serve_forever()
'''


@unittest.skipIf(os.name == 'nt', 'POSIX fixture signals/shebangs; use test_windows.py on Windows')
class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='guard-test-')
        self.root = Path(self.temp.name)
        self.g = mod.Guard(self.root / 'state')
        self.home = self.root / 'home'
        self.profile = self.home / 'profiles/web'
        self.profile.mkdir(parents=True)
        for name, content in [('cordis.patch.yml', '[]\n'), ('pnpm-lock.yaml', 'lockfileVersion: 9\n')]:
            (self.profile / name).write_text(content)
        pkg = {'dependencies': {}, 'dsh': {'profile': {'bundles': ['core', 'optional']}}}
        mod.atomic(self.profile / 'package.json', pkg)
        (self.profile / 'node_modules').mkdir()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        sock.close()
        release = self.root / 'releases/good'
        release.mkdir(parents=True)
        shutil.copytree(self.profile, release / 'profile')
        (release / 'runtime').mkdir()
        (release / 'runtime/fake.py').write_text(FAKE_DSH)
        self.manifest = {'id': 'good', 'node': sys.executable, 'node_hash': mod.digest(sys.executable),
                         'entry': 'runtime/fake.py', 'dsh_version': 'fixture', 'validated': True,
                         'files': {f'profiles/web/{n}': mod.digest(self.profile / n) for n in ('package.json','cordis.patch.yml','pnpm-lock.yaml')},
                         'baseline_bundles': ['core', 'optional'],
                         'artifacts': {n: mod.tree_hash(release / n) for n in ('profile', 'runtime')}}
        self.g.cfg = {'home': str(self.home), 'releases': str(release.parent), 'port': self.port,
                      'initial_release': 'good', 'optional_bundles': ['optional'], 'stop_timeout': 2,
                      'startup_grace': .1, 'accept_window': .1, 'request_timeout': .3, 'repair_enabled': False,
                      'claude_settings': str(self.root / 'cli-settings.json')}
        self.manifest['source_hashes'] = self.g.source_hashes(self.manifest)
        mod.atomic(release / 'release.json', self.manifest)
        mod.atomic(self.g.directory / 'config.json', self.g.cfg)
        self.g.state.update(mode='manage', release='good', installed_hashes=self.manifest['source_hashes'])
        self.g.save()

    def tearDown(self):
        try:
            self.g.stop_child()
        finally:
            if self.g.child and self.g.child.poll() is None:
                self.g.child.kill()
                self.g.child.wait()
            self.temp.cleanup()

    def wait_ready(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.g.probe()[0] == 'ready':
                return
            time.sleep(.02)
        self.fail('fake DSH never ready: ' + str(self.g.probe()))

    def test_authenticated_child_restart_stop_and_guard_reload(self):
        self.g.launch()
        self.wait_ready()
        pid = self.g.child.pid
        self.assertEqual(self.g.application_url(), f'http://127.0.0.1:{self.port}/?token=fixture_token')
        with patch.object(mod, 'listener_pids', return_value=[pid + 1]):
            self.assertIsNone(self.g.application_url())
        with patch.object(mod, 'same_process', return_value=False):
            self.assertIsNone(self.g.application_url())
        original_log = self.g.state['child_log']
        linked_log = self.root / 'linked.log'
        linked_log.symlink_to(original_log)
        self.g.state['child_log'] = str(linked_log)
        self.assertIsNone(self.g.application_url())
        self.g.state['child_log'] = original_log
        reloaded = mod.Guard(self.g.directory)
        with patch.object(reloaded, 'launch') as launch:
            reloaded.step()
            launch.assert_not_called()
        self.assertEqual(reloaded.state['child']['pid'], pid)
        self.g.state['mode'] = 'stopped'
        self.g.save()
        self.assertEqual(self.g.status()['workflow']['phase'], 'stopping')
        self.g.stop_child()
        self.assertEqual(self.g.status()['workflow']['phase'], 'stopped')
        self.assertFalse(mod.listening(self.port))
        self.assertIsNone(self.g.application_url())
        restarted = mod.Guard(self.g.directory)
        with patch.object(restarted, 'launch') as launch:
            restarted.step()
            launch.assert_not_called()
        self.assertEqual(restarted.state['mode'], 'stopped')

    def test_status_does_not_create_state_directory(self):
        directory = self.root / 'absent'
        p = subprocess.run([sys.executable, str(SOURCE), '--state', str(directory), 'status'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(directory.exists())
        self.assertEqual(json.loads(p.stdout)['mode'], 'observe')

    def test_unmanaged_listener_blocks_launch_without_signals(self):
        sock = socket.socket()
        try:
            sock.bind(('127.0.0.1', self.port))
            sock.listen()
            with patch.object(mod.os, 'kill') as kill:
                with self.assertRaisesRegex(mod.Blocked, 'Unmanaged'):
                    self.g.launch()
                kill.assert_not_called()
        finally:
            sock.close()

    def test_pid_reuse_does_not_signal_other_process(self):
        self.g.state['child'] = {'pid': os.getpid(), 'signature': 'old-start-time'}
        with patch.object(mod.os, 'kill') as kill:
            self.g.stop_child()
            kill.assert_not_called()

    def test_manual_stop_is_recorded_even_when_operation_is_busy(self):
        with self.g.lock():
            result = subprocess.run([sys.executable, str(SOURCE), '--state', str(self.g.directory), 'stop'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.g.directory / 'manual-stop').exists())
        reloaded = mod.Guard(self.g.directory)
        reloaded.step()
        self.assertEqual(reloaded.state['mode'], 'stopped')
        self.assertEqual(reloaded.status()['mode'], 'stopped')

    def test_stale_local_popen_does_not_forget_the_current_owned_child(self):
        self.g.launch()
        self.wait_ready()
        child = self.g.child
        from unittest.mock import Mock
        self.g.child = Mock(pid=child.pid+100000, poll=lambda: 0)
        self.g.stop_child()
        child.wait(timeout=2)
        self.assertFalse(mod.listening(self.port))
        self.g.child = None

    def test_operation_lock_serializes_control(self):
        second = mod.Guard(self.g.directory)
        with self.g.lock():
            with self.assertRaisesRegex(mod.Blocked, 'lock'):
                with second.lock():
                    pass

    def test_wake_gap_does_not_restart_healthy_child(self):
        self.g.state.update(child={'pid': 1}, last_tick=1, started=1, failures=2)
        with patch.object(mod, 'same_process', return_value=True), patch.object(self.g, 'probe', return_value=('failed', 'timeout')), patch.object(self.g, 'recover') as recover:
            self.g.step(now=1000)
            self.assertEqual(self.g.state['failures'], 0)
            recover.assert_not_called()
        self.g.state.pop('child')

    def test_unknown_or_degraded_never_restarts(self):
        self.g.state.update(child={'pid': 1}, started=0)
        for health in ('unknown', 'degraded'):
            with patch.object(mod, 'same_process', return_value=True), patch.object(self.g, 'probe', return_value=(health, 'fixture')), patch.object(self.g, 'recover') as recover:
                self.g.step()
                recover.assert_not_called()
        self.g.state.pop('child')

    def test_consecutive_explicit_failures_trigger_recovery(self):
        self.g.state.update(child={'pid': 1}, started=0)
        with patch.object(mod, 'same_process', return_value=True), patch.object(self.g, 'probe', return_value=('failed', 'fixture')), patch.object(self.g, 'recover') as recover:
            self.g.step()
            self.g.step()
            recover.assert_not_called()
            self.g.step()
            recover.assert_called_once()
        self.g.state.pop('child')

    def test_repair_reservation_survives_restart_and_uncertain_usage(self):
        turns, dollars, seconds = self.g.reserve_repair()
        self.assertEqual((turns, dollars), (15, 6.0))
        self.assertLessEqual(seconds, 1200)
        restored = mod.Guard(self.g.directory)
        with self.assertRaisesRegex(mod.Blocked, 'accounting uncertain'):
            restored.reserve_repair()
        restored.state['incident']['accounting_uncertain'] = False
        restored.state['incident']['usd'] = 1
        restored.save()
        self.assertEqual(restored.reserve_repair()[0], 15)
        restored.state['incident']['accounting_uncertain'] = False
        restored.save()
        with self.assertRaisesRegex(mod.Blocked, 'budget'):
            mod.Guard(self.g.directory).reserve_repair()

    def test_incident_budget_and_bounded_restart(self):
        with patch.object(self.g, 'stop_child'), patch.object(self.g, 'repair') as repair:
            self.g.recover()
            self.g.recover()
            self.assertEqual(self.g.state['incident']['restarts'], 2)
            repair.side_effect = mod.Blocked('disabled')
            # Disabled repair must fail closed rather than loop indefinitely.
            self.g.cfg['repair_enabled'] = False
            with self.assertRaises(mod.Blocked):
                self.g.recover()

    def test_only_enrolled_bundle_changes(self):
        with self.assertRaises(mod.Blocked):
            self.g.candidate([{'bundle': 'core', 'enabled': False}])
        rid = self.g.candidate([{'bundle': 'optional', 'enabled': False}])
        pkg = mod.load(self.root / 'releases' / rid / 'profile/package.json')
        self.assertEqual(pkg['dsh']['profile']['bundles'], ['core'])
        with self.assertRaisesRegex(mod.Blocked, 'validation'):
            self.g.publish(rid)

    def candidate_validated_fixture(self):
        rid = self.g.candidate([{'bundle': 'optional', 'enabled': False}])
        path = self.root / 'releases' / rid / 'release.json'
        manifest = mod.load(path)
        manifest['validated'] = True  # fixture HTTP server already exercises the contract
        mod.atomic(path, manifest)
        return rid

    def test_publication_failure_restores_files_and_dependency_directory(self):
        rid = self.candidate_validated_fixture()
        original = (self.profile / 'package.json').read_text()
        with patch.object(self.g, 'probe', return_value=('failed', 'fixture')):
            with self.assertRaisesRegex(mod.Blocked, 'readiness'):
                self.g.publish(rid)
        self.assertEqual((self.profile / 'package.json').read_text(), original)
        self.assertTrue((self.profile / 'node_modules').is_dir())
        self.assertFalse((self.profile / 'node_modules').is_symlink())
        self.assertEqual(self.g.state['release'], 'good')
        self.assertFalse((self.g.directory / 'publication.json').exists())
        self.wait_ready()

    def test_publication_commits_only_after_authenticated_stability(self):
        rid = self.candidate_validated_fixture()
        self.g.cfg['startup_grace'] = 3
        self.g.publish(rid)
        self.assertEqual(self.g.state['known_good'], rid)
        self.assertTrue((self.profile / 'node_modules').resolve().is_relative_to((self.g.directory / 'installed').resolve()))
        (self.profile / 'node_modules/live-only').write_text('package-manager fixture')
        self.assertFalse((self.root / 'releases' / rid / 'profile/node_modules/live-only').exists())
        self.assertFalse((self.g.directory / 'publication.json').exists())
        self.wait_ready()

    def test_interrupted_publication_restores_before_restart(self):
        rid = self.candidate_validated_fixture()
        modules = self.g.module_state()
        backups = {n: mod.safe_file(self.home,n).read_text() for n in self.manifest['files']}
        mod.atomic(self.g.directory / 'publication.json', {
            'previous_release':'good','previous_hashes':self.manifest['source_hashes'],
            'previous_known_good':'good', 'backups':backups,'modules':modules})
        self.g.switch_modules(self.root / 'releases' / rid / 'profile/node_modules', modules)
        (self.profile / 'package.json').write_text('broken')
        self.g.state.update(release=rid, known_good=rid)
        self.g.save()
        self.g.restore_journal()
        self.assertEqual(self.g.state['release'], 'good')
        self.assertEqual(self.g.state['known_good'], 'good')
        self.assertEqual(mod.load(self.profile / 'package.json')['dsh']['profile']['bundles'], ['core','optional'])
        self.assertFalse((self.profile / 'node_modules').is_symlink())
        self.wait_ready()

    def test_concurrent_production_edit_blocks_publication(self):
        rid = self.candidate_validated_fixture()
        (self.profile / 'cordis.patch.yml').write_text('new user edit')
        with patch.object(self.g, 'stop_child') as stop:
            with self.assertRaisesRegex(mod.Blocked, 'Concurrent'):
                self.g.publish(rid)
            stop.assert_not_called()
        self.assertEqual((self.profile / 'cordis.patch.yml').read_text(), 'new user edit')

    def fake_claude(self, result):
        script = self.root / 'claude-fixture'
        evidence = self.root / 'argv.json'
        script.write_text('#!' + sys.executable + '\nimport json,sys,pathlib\n' +
                          'pathlib.Path(' + repr(str(evidence)) + ').write_text(json.dumps(sys.argv[1:]))\n' +
                          'print(' + repr(json.dumps(result)) + ')\n')
        script.chmod(0o700)
        self.g.cfg.update(claude=str(script), repair_enabled=True)
        return evidence

    def test_background_claude_reuses_current_settings_without_customizations(self):
        settings = Path(self.g.cfg['claude_settings'])
        mod.atomic(settings, {'model':'configured-model', 'env':{
            'ANTHROPIC_BASE_URL':'http://127.0.0.1:1234', 'ANTHROPIC_AUTH_TOKEN':'fixture-private',
            'CLAUDE_CODE_MODEL_CAPABILITIES':'{"configured-model":{"supportsToolCalls":true}}',
            'CLAUDE_CODE_AUTO_MODE_SERVER':'must-not-load'},
            'hooks':{'Stop':[]}, 'permissions':{'defaultMode':'bypassPermissions'}})
        with patch.dict(os.environ, {}, clear=True):
            env, model = mod.claude_context(settings)
            self.assertEqual(model, 'configured-model')
            self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'fixture-private')
            self.assertEqual(env['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:1234')
            self.assertIn('CLAUDE_CODE_MODEL_CAPABILITIES', env)
            self.assertNotIn('CLAUDE_CODE_AUTO_MODE_SERVER', env)
            self.assertNotIn('DSH_HOME', env)
            self.assertNotIn('ANTHROPIC_AUTH_TOKEN', mod.child_env(self.home))
            mod.atomic(settings, {'model':'changed-model', 'env':{'ANTHROPIC_AUTH_TOKEN':'changed-fixture'}})
            fresh_env, fresh_model = mod.claude_context(settings)
            self.assertEqual((fresh_model, fresh_env['ANTHROPIC_AUTH_TOKEN']), ('changed-model','changed-fixture'))

    def test_fake_claude_proposes_only_and_accounts_both_calls(self):
        result = {'subtype':'success','is_error':False,'total_cost_usd':.2,
                  'usage':{'input_tokens':30,'output_tokens':10},
                  'structured_output':{'diagnosis':'fixture','bundles':[{'bundle':'optional','enabled':False}],'fields':[]}}
        evidence = self.fake_claude(result)
        mod.atomic(Path(self.g.cfg['claude_settings']), {'model':'fixture-selected-model'})
        for _ in range(2):
            rid = self.g.repair()
            self.assertTrue((self.root / 'releases' / rid).is_dir())
        argv = mod.load(evidence)
        self.assertEqual(argv[argv.index('--tools')+1], '')
        self.assertEqual(argv[argv.index('--model')+1], 'fixture-selected-model')
        self.assertIn('--restricted', argv)
        self.assertIn('--safe-mode', argv)
        self.assertIn('--max-turns', argv)
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(self.g.state['incident']['attempts'], 2)
        self.assertEqual(self.g.state['incident']['turns'], 30)
        self.assertAlmostEqual(self.g.state['incident']['usd'], .4)
        with self.assertRaisesRegex(mod.Blocked, 'budget'):
            self.g.repair()

    def test_import_diagnostics_only_name_failed_enrolled_bundles(self):
        log = self.root / 'child.log'
        self.g.state['child_log'] = str(log)
        log.write_text("INFO loaded optional\nError: Cannot find package 'optional-other'\n"
                       "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'unapproved' token=private\n")
        self.assertEqual(self.g.diagnostic_codes(), ['MODULE_NOT_FOUND', 'ERR_MODULE_NOT_FOUND'])
        with log.open('a') as f:
            f.write("Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'optional' token=private\n")
        self.assertEqual(self.g.diagnostic_codes(),
                         ['MODULE_NOT_FOUND', 'ERR_MODULE_NOT_FOUND', 'optional-bundle-import:optional'])

    def test_missing_claude_usage_never_refunds_reservation(self):
        self.fake_claude({'subtype':'success','structured_output':{'bundles':[]}})
        with self.assertRaisesRegex(mod.Blocked, 'usage'):
            self.g.repair()
        self.assertTrue(mod.Guard(self.g.directory).state['incident']['accounting_uncertain'])

    def test_incident_deadline_blocks_publication_before_writes(self):
        rid = self.candidate_validated_fixture()
        self.g.state['incident'] = {'started': time.time()-1201}
        with patch.object(self.g, 'stop_child') as stop:
            with self.assertRaisesRegex(mod.Blocked, 'deadline'):
                self.g.publish(rid)
            stop.assert_not_called()

    def test_symlink_and_path_escape_rejected(self):
        for path in ('../sessions/a', '/etc/hosts', 'profiles/superagent/package.json', 'settings.yaml'):
            with self.assertRaises(mod.Blocked):
                mod.safe_file(self.home, path)
        (self.profile / 'cordis.patch.yml').unlink()
        (self.profile / 'cordis.patch.yml').symlink_to(self.root / 'outside')
        with self.assertRaises(mod.Blocked):
            mod.safe_file(self.home, 'profiles/web/cordis.patch.yml')

    def test_artifact_drift_rejected(self):
        (self.root / 'releases/good/runtime/fake.py').write_text('changed')
        with self.assertRaisesRegex(mod.Blocked, 'drift'):
            self.g.release(verify=True)

    def test_untracked_config_is_never_marked_known_good(self):
        (self.profile / 'cordis.patch.yml').write_text('- broken\n')
        self.g.state.update(child={'pid': 1}, ready_since=1)
        with patch.object(mod, 'same_process', return_value=True), patch.object(self.g, 'probe', return_value=('ready', 'fixture')):
            self.g.step()
        self.assertNotIn('known_good', self.g.state)
        self.g.state.pop('child')

    def test_checkpoint_retries_are_bounded_and_new_edits_rearm_them(self):
        self.g.cfg.update(development_mode=True, checkpoint_quiet=0, accept_window=0)
        with patch.object(self.g, 'development_fingerprint', return_value={'fixture': 'one'}) as fingerprint, \
                patch.object(self.g, 'checkpoint', side_effect=mod.Blocked('transient validation timeout')) as checkpoint:
            for now in (100, 101, 400, 700, 1000):
                self.g.observe_development(now, {'phase': 'idle'})
            self.assertEqual(checkpoint.call_count, 3)
            fingerprint.return_value = {'fixture': 'two'}
            self.g.observe_development(1001, {'phase': 'idle'})
            self.assertEqual(checkpoint.call_count, 4)

    def test_large_stdin_survives_delayed_child_and_poll_timeouts(self):
        code = 'import sys,time; time.sleep(.35); print(len(sys.stdin.read()))'
        text = 'x' * (512 * 1024)
        code, out, err = mod.run_bounded([sys.executable, '-c', code], self.root, input_text=text, timeout=3)
        self.assertEqual(code, 0, err)
        self.assertEqual(int(out.strip()), len(text))

    def test_subprocess_timeout_is_bounded(self):
        with self.assertRaisesRegex(mod.Blocked, 'deadline'):
            mod.run_bounded([sys.executable, '-c', 'import time; time.sleep(5)'], self.root, timeout=.05)

    def test_failed_console_operation_reports_its_request_id(self):
        self.g.state['mode'] = 'observe'
        mod.atomic(self.g.directory / 'control.json', {'action': 'checkpoint', 'request_id': 'owned-request'})
        self.g.step()
        self.assertFalse(self.g.state['last_control']['ok'])
        self.assertEqual(self.g.state['last_control']['request_id'], 'owned-request')
        self.assertIn('not enabled', self.g.state['last_control']['error'])
        self.assertFalse((self.g.directory / 'control.json').exists())

    def test_cli_operation_does_not_write_supervisor_heartbeat(self):
        path = self.g.directory / 'heartbeat.json'
        heartbeat = {'pid': 123, 'updated_at': 1, 'phase': 'tick'}
        mod.atomic(path, heartbeat)
        @mod.bounded_operation
        def operation(guard):
            return 'completed'
        self.assertEqual(operation(self.g), 'completed')
        self.assertEqual(mod.load(path), heartbeat)

    def test_console_stop_preempts_older_queue_and_reports_completion(self):
        mod.atomic(self.g.directory / 'control.json', {'action': 'restart', 'request_id': 'older', 'queued_at': 1})
        mod.atomic(self.g.directory / 'manual-stop', {'request_id': 'priority-stop', 'requested': 2})
        with patch.object(self.g, 'control') as control:
            self.g.step()
            control.assert_not_called()
        self.assertEqual(self.g.state['mode'], 'stopped')
        self.assertEqual(self.g.state['last_control']['request_id'], 'priority-stop')
        self.assertTrue(self.g.state['last_control']['ok'])
        self.assertFalse((self.g.directory / 'control.json').exists())

    def test_repeated_snapshot_reuses_exact_frozen_dependency_without_files_list(self):
        runtime = self.root / 'source-runtime'
        core = runtime / 'node_modules/@deepseek-ai/dsh'
        core.mkdir(parents=True)
        mod.atomic(core / 'package.json', {'name': '@deepseek-ai/dsh', 'version': 'fixture'})
        old = self.root / 'previous-artifact/zod'
        old.mkdir(parents=True)
        mod.atomic(old / 'package.json', {'name': 'zod', 'version': '1', 'files': ['*.js']})
        (old / 'index.js').write_text('export const fixture = true;')
        frozen = runtime / 'node_modules/zod'
        shutil.copytree(old, frozen)
        (self.profile / 'node_modules/zod').symlink_to(old, target_is_directory=True)
        (self.profile / 'node_modules/zod-alias').symlink_to(old, target_is_directory=True)
        with patch.object(mod, 'tree_hash', wraps=mod.tree_hash) as hashes:
            result = mod.prepare(self.g, runtime, Path(sys.executable), self.home, 'next-snapshot')
        self.assertEqual(sum(call.args == (old.resolve(),) for call in hashes.call_args_list), 1)
        link = result / 'profile/node_modules/zod'
        self.assertTrue(link.is_symlink())
        self.assertTrue(link.resolve().is_relative_to((result / 'runtime').resolve()))
        self.assertEqual((old / 'index.js').read_text(), 'export const fixture = true;')

    def test_identity_uses_fixed_timezone_and_locale(self):
        with patch.dict(os.environ, {'TZ': 'America/Los_Angeles', 'LC_ALL': 'zh_CN.UTF-8'}):
            first = mod.identity(os.getpid())
        with patch.dict(os.environ, {'TZ': 'Asia/Shanghai', 'LC_ALL': 'C'}):
            self.assertEqual(first, mod.identity(os.getpid()))

    def test_native_profile_lock_is_compatible_and_never_stolen(self):
        path = self.profile / 'package.json.lock'
        with self.g.profile_lock():
            self.assertEqual(path.read_text(), str(os.getpid()) + '\n')
            with self.assertRaisesRegex(mod.Blocked, 'busy'):
                with self.g.profile_lock():
                    pass
        self.assertFalse(path.exists())
        path.write_text('99999999\n')
        with self.assertRaises(mod.Blocked):
            with self.g.profile_lock():
                pass
        self.assertTrue(path.exists())
        path.unlink()
        (self.profile / '.plugin-manager').mkdir()
        (self.profile / '.plugin-manager/run.json').write_text('{}')
        with self.assertRaisesRegex(mod.Blocked, 'native recovery'):
            with self.g.profile_lock():
                pass

    def test_development_rescue_preserves_broken_worktree_and_data(self):
        self.g.cfg['development_mode'] = True
        self.g.cfg['startup_grace'] = 3
        self.g.state['known_good'] = 'good'
        session = self.home / 'sessions/session-evidence'
        session.parent.mkdir()
        session.write_text('existing session remains')
        patchfile = self.profile / 'cordis.patch.yml'
        patchfile.write_text('[broken development YAML')
        self.g.rescue_publish('good')
        self.assertEqual(patchfile.read_text(), '[broken development YAML')
        self.assertFalse((self.profile / 'node_modules').is_symlink())
        rescue = Path(self.g.state['run_home'])
        self.assertEqual((rescue / 'profiles/web/cordis.patch.yml').read_text(), '[]\n')
        self.assertEqual((rescue / 'sessions/session-evidence').resolve(), session.resolve())
        self.assertEqual(session.read_text(), 'existing session remains')
        self.assertEqual(self.g.probe()[0], 'ready')
        command = mod.identity(self.g.state['child']['pid'])['signature']
        self.assertLess(command.index('--patch'), command.index('--port'))
        self.assertTrue(list((self.g.directory / 'incidents').glob('*/profiles/web/cordis.patch.yml')))

    def test_busy_operation_defers_recovery_but_has_deadline(self):
        self.g.cfg.update(development_mode=True, operation_grace=10)
        self.g.state.update(child={'pid': 999999}, started=0)
        path = self.profile / 'package.json.lock'
        path.write_text(str(os.getpid()) + '\n')
        with patch.object(self.g, 'recover') as recover:
            self.g.step(now=100)
            recover.assert_not_called()
            with self.assertRaisesRegex(mod.Blocked, 'deadline'):
                self.g.step(now=111)
        self.g.state.pop('child')

    def test_planned_restart_does_not_consume_incident_budget(self):
        self.g.launch()
        self.wait_ready()
        pid = self.g.child.pid
        self.g.control('restart')
        self.wait_ready()
        self.assertNotEqual(pid, self.g.child.pid)
        self.assertIsNone(self.g.state.get('incident'))

    def test_bridge_rejects_stale_wrong_epoch_or_pid(self):
        self.g.state.update(child={'pid': 12}, bridge_epoch='owned-epoch')
        value = {'schema': 1, 'pid': 12, 'epoch': 'owned-epoch', 'updated_at': 100,
                 'phase': 'idle', 'capabilities': []}
        mod.atomic(self.g.directory / 'bridge.json', value)
        self.assertIsNotNone(self.g.bridge(now=105))
        self.assertIsNone(self.g.bridge(now=116))
        value.update(updated_at=100, epoch='unrelated')
        mod.atomic(self.g.directory / 'bridge.json', value)
        self.assertIsNone(self.g.bridge(now=105))
        self.g.state.pop('child')

    def test_unavailable_bridge_never_accepts_checkpoint(self):
        self.g.cfg['development_mode'] = True
        with self.assertRaisesRegex(mod.Blocked, 'unavailable'):
            self.g.checkpoint()
        self.assertEqual(self.g.state['release'], 'good')

    def test_rescue_settings_edit_blocks_return(self):
        self.g.cfg.update(development_mode=True, startup_grace=3)
        self.g.rescue_publish('good')
        file = Path(self.g.state['run_home']) / 'profiles/web/cordis.patch.yml'
        file.write_text('user edit in rescue')
        pid = self.g.child.pid
        with self.assertRaisesRegex(mod.Blocked, 'settings changed'):
            self.g.return_development()
        self.assertEqual(self.g.child.pid, pid)
        self.assertEqual(file.read_text(), 'user edit in rescue')

    def test_redaction(self):
        secret = 'http://127.0.0.1:3080/?token=abc123\nAuthorization: Bearer hidden\napi_key=hello'
        text = mod.redact(secret)
        self.assertNotIn('abc123', text)
        self.assertNotIn('hidden', text)
        self.assertNotIn('hello', text)

    @unittest.skipUnless(shutil.which('node'), 'Node required for existing YAML dependency')
    def test_native_watch_configuration_preserves_other_fields_and_refuses_expressions(self):
        configured = os.environ.get('DSH_GUARD_TEST_YAML_PACKAGE')
        if not configured:
            self.skipTest('Set DSH_GUARD_TEST_YAML_PACKAGE to an installed yaml/package.json')
        runtime = Path(configured)
        self.assertTrue(runtime.is_file(), 'Explicit YAML test package is missing')
        path = self.profile / 'cordis.patch.yml'
        path.write_text('- id: hmr\n  config:\n    root: ["/existing"]\n    debounce: 250 # retain\n- id: other\n  disabled: !!js process.exit(99)\n')
        command = ['node', str(SOURCE.with_name('configure-watch.mjs')), str(runtime), str(path), '["/new", "/existing"]']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = json.loads(result.stdout)['text']
        self.assertEqual(text.count('/existing'), 1)
        self.assertIn('/new', text)
        self.assertIn('250 # retain', text)
        self.assertIn('!!js process.exit(99)', text)
        path.write_text('- id: hmr\n  config:\n    root: !!js process.exit(99)\n')
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_yaml_edits_preserve_expressions_and_reject_executable_fields(self):
        configured = os.environ.get('DSH_GUARD_TEST_YAML_PACKAGE')
        if not configured:
            self.skipTest('Set DSH_GUARD_TEST_YAML_PACKAGE to an installed yaml/package.json')
        runtime = Path(configured)
        self.assertTrue(runtime.is_file(), 'Explicit YAML test package is missing')
        text = '- id: optional\n  disabled: true # retain\n  config:\n    expr: !!js process.exit(99)\n    command: hello\n'
        request = {'action': 'apply', 'yamlPackage': str(runtime), 'text': text,
                   'changes': [{'path': [0,'disabled'], 'expected': True,'value':False}],
                   'allowed': [{'path':[0,'disabled'],'values':[True,False]}]}
        p = subprocess.run(['node', str(SOURCE.with_name('validate.mjs'))], input=json.dumps(request), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        output = json.loads(p.stdout)
        self.assertIn('!!js process.exit(99)', output['text'])
        self.assertIn('# retain', output['text'])
        request['changes'] = [{'path':[0,'config','command'],'expected':'hello','value':'bad'}]
        request['allowed'] = [{'path':[0,'config','command'],'values':['hello','bad']}]
        p = subprocess.run(['node', str(SOURCE.with_name('validate.mjs'))], input=json.dumps(request), capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)


if __name__ == '__main__':
    unittest.main()
