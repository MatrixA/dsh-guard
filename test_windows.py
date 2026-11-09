"""Contracts run everywhere; NativeWindowsTests require Windows and native node.exe.

Only temporary fixtures, fresh ports and owned subprocesses are used. Production
DSH homes/listeners/tasks are never touched. Skips are reported, never acceptance.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent

def module(name):
    spec = importlib.util.spec_from_file_location('test_' + name, ROOT / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


guard = module('dsh_guard')
launcher = module('launcher')
if os.name == 'nt':
    win = module('windows_platform')


def wait_for(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Fixture deadline exceeded')


class WindowsPolicyTests(unittest.TestCase):
    def test_portable_path_checks_and_failed_listener_query_fail_closed(self):
        backend = module('windows_platform')
        backend._self_check()
        with patch.object(guard, 'WINDOWS', True), patch.object(guard, 'win', backend, create=True), \
                patch.object(backend, 'listener_pids', side_effect=OSError('access denied')):
            with self.assertRaisesRegex(guard.Blocked, 'ownership'):
                guard.listener_pids(3080)
            with self.assertRaisesRegex(guard.Blocked, 'ownership'):
                guard.listening(3080)
            with patch.object(backend, 'listener_pids', return_value=[999]):
                self.assertTrue(guard.listening(3080))
            with patch.object(guard, 'identity', return_value=None):
                with patch.object(backend, 'process_running', return_value=True):
                    with self.assertRaisesRegex(guard.Blocked, 'assume it exited'):
                        guard.same_process({'pid': 42, 'signature': 'fixture'})
                with patch.object(backend, 'process_running', side_effect=OSError('access denied')):
                    with self.assertRaisesRegex(guard.Blocked, 'inspection failed'):
                        guard.same_process({'pid': 42, 'signature': 'fixture'})
                with patch.object(backend, 'process_running', return_value=False):
                    self.assertFalse(guard.same_process({'pid': 42, 'signature': 'fixture'}))

    def test_recovery_and_model_paths_reject_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            g = guard.Guard(Path(temp))
            with patch.object(guard, 'WINDOWS', True), patch.object(guard.subprocess, 'Popen', side_effect=AssertionError('must not execute')):
                for name, args in [('checkpoint', ()), ('validate', ('candidate',)),
                                   ('publish', ('candidate',)), ('rescue_publish', ('candidate',)),
                                   ('return_development', ()), ('repair', ()), ('check_claude', ()),
                                   ('candidate', ([],)), ('restore_journal', ())]:
                    with self.subTest(name=name), self.assertRaises(guard.Blocked):
                        getattr(g, name)(*args)
                with self.assertRaises(guard.Blocked):
                    guard.prepare(g, Path(temp), Path(sys.executable), Path(temp), 'nope')
                with self.assertRaises(guard.Blocked):
                    guard.run_bounded([sys.executable, '-c', 'raise SystemExit'], temp)

    def test_launch_only_is_distinct_from_a_validated_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            g = guard.Guard(Path(temp))
            g.cfg = {'platform_mode': 'windows-supervision-preview'}
            launch = {'launch_only': True, 'platform': 'win32', 'validated': False}
            with patch.object(guard, 'WINDOWS', True):
                self.assertTrue(g.launch_allowed(launch))
                self.assertFalse(g.launch_allowed({**launch, 'validated': True}))
                self.assertFalse(g.launch_allowed({'validated': True}))
                g.cfg['development_mode'] = True
                self.assertFalse(g.launch_allowed(launch))

    def test_ready_windows_baseline_does_not_become_known_good(self):
        with tempfile.TemporaryDirectory() as temp:
            g = guard.Guard(Path(temp))
            g.cfg = {'home': temp, 'accept_window': 1}
            now = time.time()
            g.state = {'mode': 'manage', 'child': {'pid': 42}, 'release': 'launch-only',
                       'ready_since': now - 10, 'known_good': 'must-clear'}
            with patch.object(guard, 'WINDOWS', True), patch.object(guard, 'same_process', return_value=True), \
                    patch.object(g, 'probe', return_value=('ready', 'fixture')), patch.object(g, 'save'):
                g.step(now)
            self.assertIsNone(g.state['known_good'])
            self.assertIn('Windows', g.state['checkpoint_error'])

    def test_restart_limit_never_enters_repair_even_when_configuration_enables_it(self):
        with tempfile.TemporaryDirectory() as temp:
            g = guard.Guard(Path(temp))
            g.cfg = {'home': temp, 'repair_enabled': True}
            g.state['incident'] = {'started': time.time(), 'restarts': 2}
            with patch.object(guard, 'WINDOWS', True), patch.object(g, 'save'), \
                    patch.object(g, 'stop_child'), patch.object(g, 'repair', side_effect=AssertionError('model called')):
                with self.assertRaisesRegex(guard.Blocked, 'restart limit'):
                    g.recover()


NODE_FIXTURE = r'''const http=require('http'),fs=require('fs'),path=require('path');
const port=Number(process.argv[process.argv.indexOf('--port')+1]);
const server=http.createServer((req,res)=>{
 if(req.method==='GET'&&req.url.startsWith('/?token=')){
  res.writeHead(303,{'Set-Cookie':'fixture=ok; Path=/; HttpOnly','Location':'/'});return res.end();}
 if(req.method==='GET'){res.writeHead(req.headers.cookie?200:401);return res.end('fixture');}
 let body='';req.on('data',s=>body+=s);req.on('end',()=>{
  const x=JSON.parse(body);res.writeHead(200,{'Content-Type':'application/json'});
  res.end(JSON.stringify({type:'server-response',rpcId:x.rpcId,result:{ok:true,value:{items:[]}}}));});
});
const timer=setInterval(()=>fs.writeFileSync(path.join(__dirname,'progress'),String(Date.now())),100);
process.on('SIGINT',()=>{if(fs.existsSync(path.join(__dirname,'ignore-stop')))return;fs.writeFileSync(path.join(__dirname,'stopped'),'SIGINT');clearInterval(timer);server.close(()=>process.exit(0));});
server.listen(port,'127.0.0.1',()=>console.log('http://127.0.0.1:'+port+'/?token=fixture_windows'));
'''


@unittest.skipUnless(os.name == 'nt', 'requires native Windows; not a mock acceptance')
class NativeWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsh-guard-空 格-')
        self.root = Path(self.temp.name).resolve()
        self.children = []
        self.guards = []
        self.node = os.environ.get('DSH_GUARD_TEST_NODE') or shutil.which('node.exe')
        if not self.node:
            self.fail('Install native node.exe or set DSH_GUARD_TEST_NODE')
        self.node = str(Path(self.node).resolve())

    def tearDown(self):
        # Production stop remains graceful. Forced cleanup is limited to fixture
        # Popen children/Job handles created by this test process.
        for g in self.guards:
            try:
                g.stop_child()
            finally:
                if g.child is not None and g.child.poll() is None:
                    g.child.kill()
                    g.child.wait(timeout=5)
        for child in self.children:
            launcher.terminate_owned(child)
        self.temp.cleanup()

    def configured(self):
        state = self.root / 'state'
        state.mkdir()
        home = self.root / 'home'
        (home / 'profiles/web').mkdir(parents=True)
        entry = self.root / 'fixture.js'
        entry.write_text(NODE_FIXTURE, encoding='utf-8')
        release = self.root / 'releases/fixture'
        release.mkdir(parents=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        guard.atomic(release / 'release.json', {'id': 'fixture', 'platform': 'win32', 'launch_only': True,
                     'validated': False, 'node': self.node, 'node_hash': guard.digest(self.node),
                     'entry': str(entry), 'entry_hash': guard.digest(entry), 'artifacts': {}, 'files': {}})
        guard.atomic(state / 'config.json', {'platform_mode': 'windows-supervision-preview',
                     'home': str(home), 'releases': str(self.root / 'releases'), 'initial_release': 'fixture',
                     'port': port, 'stop_timeout': 5, 'startup_grace': 1})
        g = guard.Guard(state)
        g.state.update(mode='manage', release='fixture')
        g.save()
        self.guards.append(g)
        return g

    def test_native_identity_listeners_and_reused_identity_rejection(self):
        own = win.identity(os.getpid())
        self.assertIsNotNone(own)
        self.assertEqual(own, win.identity(os.getpid()))
        self.assertIsNone(win.identity(0))
        self.assertFalse(guard.same_process({**own, 'signature': own['signature'] + 'stale'}))
        for family, address in [(socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')]:
            with socket.socket(family) as sock:
                sock.bind((address, 0))
                sock.listen()
                self.assertIn(os.getpid(), win.listener_pids(sock.getsockname()[1]))

    def test_authenticated_node_graceful_stop_reload_and_manual_stop(self):
        g = self.configured()
        g.launch()
        original = g.state['child']
        wait_for(lambda: g.probe()[0] == 'ready')
        self.assertEqual(win.listener_pids(g.port), [original['pid']])
        reloaded = guard.Guard(g.directory)
        reloaded.step()
        self.assertEqual(reloaded.state['child'], original)
        self.assertTrue(guard.same_process(original))
        reloaded.control('stop')
        self.assertEqual((self.root / 'stopped').read_text(), 'SIGINT')
        self.assertFalse(guard.same_process(original))
        guard.Guard(g.directory).step()
        self.assertFalse(guard.listening(g.port))

    def test_unknown_listener_is_preserved_and_missing_console_proof_refuses_stop(self):
        g = self.configured()
        own = win.identity(os.getpid())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', g.port))
            sock.listen()
            with self.assertRaisesRegex(guard.Blocked, 'Unmanaged'):
                g.launch()
            self.assertEqual(win.identity(os.getpid()), own)
        with socket.socket(socket.AF_INET6) as sock:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(('::1', g.port))
            sock.listen()
            with self.assertRaisesRegex(guard.Blocked, 'Unmanaged'):
                g.launch()
        g.launch()
        wait_for(lambda: g.probe()[0] == 'ready')
        proof = g.state.pop('windows_console_owner')
        try:
            with self.assertRaisesRegex(guard.Blocked, 'console ownership'):
                g.stop_child()
            self.assertTrue(guard.same_process(g.state['child']))
        finally:
            g.state['windows_console_owner'] = proof
        ignore = self.root / 'ignore-stop'
        ignore.write_text('fixture', encoding='utf-8')
        g.cfg['stop_timeout'] = .3
        try:
            with self.assertRaisesRegex(guard.Blocked, 'timed out'):
                g.stop_child()
            self.assertTrue(guard.same_process(g.state['child']))
        finally:
            ignore.unlink()
            g.cfg['stop_timeout'] = 5

    def test_cross_process_lock_release_and_exclusive_complete_queue(self):
        ready = self.root / 'ready'
        lock = self.root / 'operation.lock'
        code = '''import importlib.util,sys,time,pathlib
s=importlib.util.spec_from_file_location('w',sys.argv[1]);w=importlib.util.module_from_spec(s);s.loader.exec_module(w)
with w.exclusive(pathlib.Path(sys.argv[2])):
 pathlib.Path(sys.argv[3]).write_text('ready');time.sleep(30)
'''
        child = subprocess.Popen([sys.executable, '-I', '-c', code, str(ROOT / 'windows_platform.py'), str(lock), str(ready)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        wait_for(ready.exists)
        with self.assertRaises(BlockingIOError):
            with win.exclusive(lock):
                pass
        launcher.terminate_owned(child)
        with win.exclusive(lock):
            pass
        stage, queue = self.root / 'stage.json', self.root / 'queue.json'
        guard.atomic(stage, {'unicode': '中文 空格', 'payload': 'x' * 10000})
        os.link(stage, queue)
        with self.assertRaises(FileExistsError):
            os.link(stage, queue)
        self.assertEqual(guard.load(queue)['unicode'], '中文 空格')

    def test_junction_and_alternate_stream_are_rejected(self):
        target = self.root / 'outside'
        target.mkdir()
        (target / 'secret').write_text('fixture', encoding='utf-8')
        junction = self.root / 'junction'
        script = "$x=[Console]::In.ReadToEnd()|ConvertFrom-Json;New-Item -ItemType Junction -Path $x.link -Target $x.target|Out-Null"
        ps = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command', script],
                       input=json.dumps({'link': str(junction), 'target': str(target)}), text=True,
                       capture_output=True, timeout=15, check=True)
        self.assertTrue(win.is_link(junction))
        for path in [junction / 'secret', target / 'secret:stream']:
            with self.subTest(path=str(path)), self.assertRaises((OSError, ValueError)):
                win.open_regular(path, os.O_RDONLY)
        self.assertEqual((target / 'secret').read_text(), 'fixture')

    def test_guard_job_closure_kills_guard_and_preserves_dsh_progress(self):
        # This runs under the caller's actual job, including a Task Scheduler job
        # when Test-DshGuard.ps1 -Scheduled is selected. Breakaway failures fail.
        entry = self.root / 'progress.js'
        entry.write_text("const fs=require('fs');setInterval(()=>fs.writeFileSync(process.argv[2],String(Date.now())),100);process.on('SIGINT',()=>process.exit(0));", encoding='utf-8')
        record_file, progress = self.root / 'child.json', self.root / 'progress'
        code = '''import importlib.util,json,os,pathlib,subprocess,sys,time
s=importlib.util.spec_from_file_location('w',sys.argv[1]);w=importlib.util.module_from_spec(s);s.loader.exec_module(w)
w.wait_guard_job(os.environ['DSH_GUARD_JOB'])
p=subprocess.Popen([sys.argv[2],sys.argv[3],sys.argv[4]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**w.persistent_options())
record=w.identity(p.pid)
if record is None:
 p.terminate();p.wait();raise SystemExit(3)
pathlib.Path(sys.argv[5]).write_text(json.dumps(record))
while True: time.sleep(.1)
'''
        job = win.GuardJob()
        try:
            child = subprocess.Popen([sys.executable, '-I', '-c', code, str(ROOT / 'windows_platform.py'),
                                      self.node, str(entry), str(progress), str(record_file)],
                                     env={**launcher.guard_environment(), 'DSH_GUARD_JOB': job.name},
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.children.append(child)
            job.assign(child)
            wait_for(record_file.exists)
            record = json.loads(record_file.read_text())
            self.assertIsNotNone(record)
            wait_for(progress.exists)
            before = progress.read_text()
            job.close()
            child.wait(timeout=5)
            self.assertTrue(guard.same_process(record))
            wait_for(lambda: progress.read_text() != before)
        finally:
            job.close()
            if record_file.exists():
                record = json.loads(record_file.read_text())
                if record and guard.same_process(record):
                    win.graceful_stop(record)
                    wait_for(lambda: not guard.same_process(record))

    def test_real_launcher_restarts_guard_after_source_removal_and_keeps_dsh(self):
        source, store, state = (self.root / name for name in ('source', 'store', 'launcher-state'))
        source.mkdir()
        state.mkdir()
        for name in launcher.FILES:
            if name == 'development.py':
                continue
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        entry = self.root / 'ticker.js'
        progress = self.root / 'ticker'
        entry.write_text("const fs=require('fs');setInterval(()=>fs.writeFileSync(process.argv[2],String(Date.now())),100);process.on('SIGINT',()=>process.exit(0));", encoding='utf-8')
        code = '''import json,os,pathlib,subprocess,sys,time
import windows_platform as w
w.wait_guard_job(os.environ['DSH_GUARD_JOB'])
s=pathlib.Path(sys.argv[sys.argv.index('--state')+1]);marker=s/'fixture-child.json'
if not marker.exists():
 p=subprocess.Popen([NODE,ENTRY,PROGRESS],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**w.persistent_options())
 record=w.identity(p.pid)
 if record is None:
  p.terminate();p.wait();raise SystemExit(3)
 marker.write_text(json.dumps(record))
while True:
 if (s/'crash-once').exists():
  (s/'crash-once').unlink();raise SystemExit(9)
 value={'pid':os.getpid(),'updated_at':time.time(),'phase':'tick'}
 t=s/'beat.tmp';t.write_text(json.dumps(value));os.replace(t,s/'heartbeat.json');time.sleep(.05)
'''
        code = ('NODE=' + repr(self.node) + '\nENTRY=' + repr(str(entry)) + '\nPROGRESS=' + repr(str(progress)) + '\n' + code)
        (source / 'dsh_guard.py').write_text(code, encoding='utf-8')
        launcher.install(source, store)
        bootstrap = self.root / 'bootstrap'
        bootstrap.mkdir()
        for name in ('launcher.py', 'windows_platform.py'):
            shutil.copyfile(ROOT / name, bootstrap / name)
        wrapper = '''import importlib.util,sys,threading
s=importlib.util.spec_from_file_location('boot',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.POLL_SECONDS=.05;m.BACKOFF_SECONDS=.1;m.MAX_BACKOFF=.2
raise SystemExit(m.Runner(sys.argv[2],sys.argv[3]).run(threading.Event()))
'''
        child = subprocess.Popen([sys.executable, '-I', '-c', wrapper, str(bootstrap / 'launcher.py'), str(store), str(state)],
                                 cwd=bootstrap, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env={**launcher.guard_environment(), 'ANTHROPIC_API_KEY': 'fixture-never-forward'})
        self.children.append(child)
        marker = state / 'fixture-child.json'
        def value(path):
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return {}
        record = None
        try:
            record = wait_for(lambda: value(marker))
            first = wait_for(lambda: value(state / 'launcher.json').get('guard_pid'))
            source.rename(self.root / 'source-removed')
            (state / 'crash-once').write_text('fixture')
            second = wait_for(lambda: (value(state / 'launcher.json').get('guard_pid') or first) != first)
            self.assertTrue(second)
            wait_for(lambda: value(state / 'launcher.json').get('phase') == 'tick')
            self.assertTrue(guard.same_process(record))
            before = progress.read_text()
            launcher.terminate_owned(child)  # Abrupt launcher exit closes its Job.
            self.assertTrue(guard.same_process(record))
            wait_for(lambda: progress.read_text() != before)
        finally:
            launcher.terminate_owned(child)
            record = record or value(marker)
            if record and guard.same_process(record):
                win.graceful_stop(record)
                wait_for(lambda: not guard.same_process(record))


if __name__ == '__main__':
    unittest.main()
