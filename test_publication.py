"""Portable enrollment defaults and macOS first-install checks; no live DSH access."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
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
    spec = importlib.util.spec_from_file_location('publication_' + name, ROOT / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


guard = module('dsh_guard')
launcher = module('launcher')


class DefaultsTests(unittest.TestCase):
    def test_home_precedence_and_absolute_xdg_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DSH_HOME': tmp}):
            self.assertEqual(guard.enrollment_home(), Path(tmp).resolve())
            self.assertEqual(guard.enrollment_home(config={'home': '/enrolled'}), Path('/enrolled').resolve())
            self.assertEqual(guard.enrollment_home('/explicit', {'home': '/enrolled'}), Path('/explicit').resolve())
        with patch.dict(os.environ, {'XDG_STATE_HOME': 'relative'}):
            self.assertEqual(guard.xdg_root('XDG_STATE_HOME', '/fallback'), Path('/fallback'))
        with patch.dict(os.environ, {'XDG_STATE_HOME': '/absolute'}):
            self.assertEqual(guard.xdg_root('XDG_STATE_HOME', '/fallback'), Path('/absolute'))

    def test_launcher_environment_reaches_dsh_without_inheriting_agent_secrets(self):
        values = {'HTTPS_PROXY': 'http://127.0.0.1:8080', 'https_proxy': 'http://127.0.0.1:8081',
                  'ANTHROPIC_API_KEY': 'fixture', 'PYTHONPATH': '/untrusted',
                  'NODE_EXTRA_CA_CERTS': '/certs/custom.pem', 'PNPM_HOME': '/tools/pnpm',
                  'DSH_HOME': '/unexpected', 'NODE_OPTIONS': '--require=/untrusted'}
        with patch.dict(os.environ, values):
            with patch.dict(os.environ, launcher.guard_environment(), clear=True):
                env = guard.child_env(Path('/enrolled'))
            self.assertEqual(env['HTTPS_PROXY'], values['https_proxy' if os.name == 'nt' else 'HTTPS_PROXY'])
            self.assertEqual(env['https_proxy'], values['https_proxy'])
            self.assertEqual(env['DSH_HOME'], '/enrolled')
            self.assertEqual(env['NODE_EXTRA_CA_CERTS'], '/certs/custom.pem')
            self.assertEqual(env['PNPM_HOME'], '/tools/pnpm')
            for key in ('ANTHROPIC_API_KEY', 'PYTHONPATH', 'NODE_OPTIONS'):
                self.assertNotIn(key, env)

    def test_claude_settings_transport_is_scoped_and_never_loads_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'settings.json'
            settings.write_text(json.dumps({'env': {'https_proxy': 'http://127.0.0.1:8080',
                'NODE_EXTRA_CA_CERTS': '/certs/claude.pem', 'ANTHROPIC_API_KEY': 'fixture',
                'NODE_OPTIONS': '--require=/untrusted'}, 'hooks': {'never': 'executed'}}))
            with patch.dict(os.environ, {}, clear=True):
                env, _ = guard.claude_context(settings)
                self.assertEqual(env['NODE_EXTRA_CA_CERTS'], '/certs/claude.pem')
                self.assertEqual(env['https_proxy'], 'http://127.0.0.1:8080')
                self.assertEqual(env['ANTHROPIC_API_KEY'], 'fixture')
                self.assertNotIn('NODE_OPTIONS', env)
                self.assertNotIn('ANTHROPIC_API_KEY', guard.child_env(Path(tmp)))


@unittest.skipUnless(sys.platform == 'darwin', 'macOS installer only')
class MacEnrollmentTests(unittest.TestCase):
    def test_install_survives_source_removal_and_environment_changes(self):
        setup = module('macos_setup')
        with tempfile.TemporaryDirectory(prefix='guard enrollment ') as tmp:
            root = Path(tmp).resolve()
            home = root / 'dsh home'
            profile = home / 'profiles/web'
            profile.mkdir(parents=True)
            for name, text in {'package.json': '{"dependencies":{},"dsh":{"profile":{"bundles":[]}}}',
                               'cordis.yml': '[]\n', 'cordis.patch.yml': '[]\n',
                               'pnpm-lock.yaml': 'lockfileVersion: 9\n'}.items():
                (profile / name).write_text(text)
            (profile / 'node_modules').mkdir()
            runtime = root / 'runtime'
            package = runtime / 'node_modules/@deepseek-ai/dsh'
            (package / 'lib').mkdir(parents=True)
            (package / 'package.json').write_text('{"name":"@deepseek-ai/dsh","version":"0.2.2-rc.1"}')
            (package / 'lib/bin.js').write_text('// never executed by enrollment\n')
            # A copied reviewed source lets the test really remove its development tree.
            source = root / 'source'
            shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('__pycache__', 'dist', '.git'))
            base, state = root / 'app', root / 'state'
            with socket.socket() as app_socket, socket.socket() as console_socket:
                app_socket.bind(('127.0.0.1', 0))
                console_socket.bind(('127.0.0.1', 0))
                paths = dict(base=base, state=state, bin_dir=root / 'bin', plist=root / 'agents/guard.plist',
                             port=app_socket.getsockname()[1], console_port=console_socket.getsockname()[1])
            before = {p.name: p.read_bytes() for p in profile.iterdir() if p.is_file()}
            with patch.object(setup, 'ROOT', source), patch.dict(os.environ, {'DSH_HOME': str(home),
                                  'ANTHROPIC_API_KEY': 'fixture', 'XDG_STATE_HOME': str(root / 'other')}):
                result = setup.enroll(runtime, node=Path(shutil.which('node')), **paths)
            self.assertEqual(result['mode'], 'observe')
            self.assertFalse(result['launchd_loaded'])
            job = plistlib.loads(paths['plist'].read_bytes())
            self.assertEqual(job['ProgramArguments'][1], '-I')
            self.assertEqual(job['ProgramArguments'][-1], str(state))
            self.assertNotIn('ANTHROPIC_API_KEY', job['EnvironmentVariables'])
            self.assertTrue((base / 'app/versions' / result['version'] / 'LICENSE').is_file())
            enrolled = guard.Guard(state)
            guard.prepare(enrolled, runtime, Path(shutil.which('node')), home, 'first')
            self.assertEqual(guard.Guard(state).state['release'], 'first')
            guard.prepare(enrolled, runtime, Path(shutil.which('node')), home, 'retry')
            self.assertEqual(guard.Guard(state).state['release'], 'retry')
            # Development capture can precede any selected release and does not replace live modules.
            dev = guard.Guard(root / 'dev-state')
            dev.cfg = {**enrolled.cfg, 'releases': str(root / 'dev-releases'), 'development_mode': True}
            dev.cfg.pop('initial_release', None)
            guard.atomic(dev.directory / 'config.json', dev.cfg)
            guard.prepare(dev, runtime, Path(shutil.which('node')), home, 'dev-first')
            manifest_path = root / 'dev-releases/dev-first/release.json'
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest['development_source'], dev.development_fingerprint())
            manifest['validated'] = True  # Only tests first-selection policy; no candidate boot claim.
            guard.atomic(manifest_path, manifest)
            activate = [sys.executable, '-B', str(ROOT / 'dsh_guard.py'), '--state', str(dev.directory), 'resume', '--manage']
            # No supervising launcher is running for this state and the CLI does not launch DSH.
            (profile / 'cordis.patch.yml').write_text('# edited since validation\n[]\n')
            rejected = subprocess.run(activate, capture_output=True, timeout=10)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(guard.Guard(dev.directory).state['mode'], 'observe')
            (profile / 'cordis.patch.yml').write_text('[]\n')
            activated = subprocess.run(activate, capture_output=True, timeout=10)
            self.assertEqual(activated.returncode, 0, activated.stderr)
            self.assertFalse(guard.Guard(dev.directory).state.get('run_home'))
            self.assertEqual(guard.Guard(dev.directory).state['installed_hashes'], manifest['source_hashes'])
            self.assertTrue((profile / 'node_modules').is_dir())
            shutil.rmtree(source)
            changed = {**os.environ, 'DSH_HOME': str(root / 'different'),
                       'XDG_STATE_HOME': str(root / 'different-state'), 'PYTHONPATH': '/untrusted'}
            answer = subprocess.run([str(root / 'bin/dsh-guard'), 'status'], env=changed,
                                    text=True, capture_output=True, timeout=10, check=True)
            self.assertEqual(json.loads(answer.stdout)['mode'], 'observe')
            self.assertEqual(json.loads((state / 'config.json').read_text())['home'], str(home))
            self.assertFalse((root / 'different-state').exists())
            self.assertEqual(before, {p.name: p.read_bytes() for p in profile.iterdir() if p.is_file()})
            # launchd's exact generated argv also runs from the installed copy.
            foreground = subprocess.Popen(job['ProgramArguments'], env=job['EnvironmentVariables'],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and foreground.poll() is None:
                    if (state / 'heartbeat.json').exists():
                        self.assertEqual(json.loads((state / 'state.json').read_text())['mode'], 'observe')
                        break
                    time.sleep(.03)
                else:
                    self.fail('Generated launcher did not reach its guard heartbeat')
            finally:
                foreground.terminate()
                foreground.communicate(timeout=15)
            override = subprocess.run([str(root / 'bin/dsh-guard'), '--state', str(root / 'elsewhere'), 'status'],
                                      capture_output=True, timeout=10)
            self.assertNotEqual(override.returncode, 0)
            with self.assertRaises(ValueError):
                setup.enroll(runtime, node=Path(shutil.which('node')), home=home, **paths)
