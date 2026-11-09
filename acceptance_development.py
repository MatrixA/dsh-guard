#!/usr/bin/env python3
"""Native DSH development acceptance, contained to a new private working directory."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
from unittest.mock import patch

import dsh_guard as mod


def run(root, baseline):
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = {'started': time.time(), 'passed': False, 'checks': [], 'work': str(root)}
    g = mod.Guard(root / 'guard')
    original_popen = subprocess.Popen
    manifest = mod.load(baseline / 'release.json')
    home = root / 'home'
    profile = home / 'profiles/web'
    profile.parent.mkdir(parents=True)
    mod.clone(baseline / 'profile', profile)
    mod.atomic(home / 'package.json', {'name': 'guard-acceptance-home', 'private': True})
    mod.atomic(profile / 'cordis.patch.yml', '[]\n', raw=True)
    mod.atomic(profile / 'cordis.yml', '[]\n', raw=True)
    (home / 'sessions').mkdir()
    (home / 'sessions/acceptance-sentinel').write_text('preserve this existing data')
    for link in (profile / 'node_modules').rglob('*'):
        if link.is_symlink():
            original = baseline / 'profile' / link.relative_to(profile)
            target = original.resolve()
            if target.is_relative_to(baseline / 'profile'):
                continue
            link.unlink()
            link.symlink_to(target, target_is_directory=target.is_dir())
    bridge = root / 'workspace/bridge'
    shutil.copytree(mod.ROOT / 'bridge', bridge, ignore=shutil.ignore_patterns('test*'))
    widget = root / 'workspace/widget'
    widget.mkdir()
    mod.atomic(widget / 'package.json', {
        'name': '@local/guard-acceptance-widget', 'version': '1.0.0', 'type': 'module',
        'exports': './index.mjs', 'files': ['index.mjs', 'cordis.patch.yml'],
        'dsh': {'bundle': {'patch': './cordis.patch.yml'}}})
    mod.atomic(widget / 'cordis.patch.yml', "- insert:\n    - id: guard-acceptance-widget\n      name: '@local/guard-acceptance-widget'\n", raw=True)

    def code(version, fatal=False):
        return ("import fs from 'node:fs';import path from 'node:path';\n"
                "export const name='guard-acceptance-widget';\n"
                "export function apply(ctx){fs.writeFileSync(path.join(process.env.DSH_HOME,'widget-version')," + json.dumps(version) + ");" +
                ("setTimeout(()=>{throw new Error('guard native acceptance fatal callback')},250);" if fatal else '') + "}\n")

    mod.atomic(widget / 'index.mjs', code('v1'), raw=True)
    # The shipped profile deliberately sets hmr.root=[]; source watching is a native opt-in.
    base_patch = '- id: hmr\n  config:\n    root: [' + json.dumps(str(widget)) + ']\n'
    mod.atomic(profile / 'cordis.patch.yml', base_patch, raw=True)
    package = mod.load(profile / 'package.json')
    package['dependencies'].update({'@local/dsh-guard-bridge': 'link:' + str(bridge),
                                    '@local/guard-acceptance-widget': 'link:' + str(widget)})
    package['dsh']['profile']['bundles'] = ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app',
                                           '@local/dsh-guard-bridge', '@local/guard-acceptance-widget']
    for name, source in (('@local/dsh-guard-bridge', bridge), ('@local/guard-acceptance-widget', widget)):
        dest = profile / 'node_modules' / name
        dest.parent.mkdir(exist_ok=True)
        dest.symlink_to(source, target_is_directory=True)
    # Existing unrelated frozen local packages remain available but unselected.
    for name, spec in list(package['dependencies'].items()):
        if name not in ('@local/dsh-guard-bridge', '@local/guard-acceptance-widget') and spec.startswith(('link:', 'file:')):
            package['dependencies'][name] = 'link:' + str((profile / 'node_modules' / name).resolve())
    mod.atomic(profile / 'package.json', package)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    g.cfg = {'home': str(home), 'releases': str(root / 'releases'), 'port': port,
             'development_mode': True, 'repair_enabled': False, 'optional_bundles': [], 'yaml_fields': {},
             'startup_grace': 60, 'accept_window': 2, 'checkpoint_quiet': 2, 'request_timeout': 3,
             'validation_timeout': 90, 'stop_timeout': 20, 'interval': 1}
    mod.atomic(g.directory / 'config.json', g.cfg)

    def save(check, **values):
        report['checks'].append({'name': check, 'passed': True, 'time': time.time(), **values})
        mod.atomic(root / 'report.json', report)
        print('PASS:', check, flush=True)

    def wait(predicate, timeout=120, tick=False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if g.child:
                g.child.poll()
            if tick:
                g.step()
            if predicate():
                return
            time.sleep(.5)
        status = g.status()
        brief = {key: status[key] for key in ('mode', 'health', 'reason', 'owned_child_alive', 'known_good')}
        brief['workflow'] = {key: status['workflow'][key] for key in ('phase', 'message', 'bridge')}
        brief['bad_capabilities'] = [x for x in status['workflow']['capabilities'] if x['status'] not in ('ready', 'disabled')]
        raise AssertionError('Acceptance deadline: ' + json.dumps(brief, ensure_ascii=False))

    def contained_popen(argv, *args, **kwargs):
        if argv[0] == manifest['node'] and len(argv) > 1 and argv[1].endswith('/dsh/lib/bin.js'):
            allowed = [root, baseline, Path(manifest['node'])]
            argv = ['/usr/bin/sandbox-exec', '-p', mod.sandbox_profile(root, allowed, port), *argv]
            kwargs['env'] = {**kwargs.get('env', {}), 'HOME': str(home), 'TMPDIR': str(root)}
        child = original_popen(argv, *args, **kwargs)
        if argv[0] == '/usr/bin/sandbox-exec' and manifest['node'] in argv:
            # sandbox-exec returns before its exec(node); don't record that transient argv as Node identity.
            deadline = time.monotonic() + 5
            while child.poll() is None and time.monotonic() < deadline:
                record = mod.identity(child.pid)
                if record and '/dsh/lib/bin.js' in record['signature'] and 'sandbox-exec' not in record['signature']:
                    break
                time.sleep(.02)
        return child

    try:
        mod.prepare(g, baseline / 'runtime', Path(manifest['node']), home, 'baseline')
        g.state.update(mode='manage', release='baseline', known_good='baseline')
        g.save()
        g.validate('baseline')
        save('native-scratch-composition-and-startup')
        with patch.object(mod.subprocess, 'Popen', side_effect=contained_popen):
            g.launch()
            wait(lambda: g.probe()[0] == 'ready' and g.bridge() and g.bridge()['phase'] == 'idle')
            save('native-host-bridge-and-capabilities', capabilities=g.bridge()['capabilities'])
            first = g.checkpoint()
            save('initial-validated-checkpoint', release=first)
            link = profile / 'node_modules/@local/guard-acceptance-widget'
            pid = g.state['child']['pid']
            mod.atomic(widget / 'index.mjs', code('v2'), raw=True)
            wait(lambda: (home / 'widget-version').read_text() == 'v2')
            wait(lambda: g.state.get('known_good') != first, timeout=180, tick=True)
            assert link.is_symlink() and link.resolve() == widget
            assert g.state['child']['pid'] == pid and not g.state.get('incident')
            save('source-hmr-and-automatic-checkpoint-preserve-link', release=g.state['known_good'])
            mod.atomic(profile / 'cordis.patch.yml', base_patch + '- id: guard-acceptance-widget\n  disabled: true\n', raw=True)
            wait(lambda: any(x['id'].split(':')[-1] == 'guard-acceptance-widget' and x['status'] == 'disabled'
                             for x in (g.bridge() or {}).get('capabilities', [])))
            assert g.state['child']['pid'] == pid
            save('native-plugin-disable-without-restart')
            mod.atomic(profile / 'cordis.patch.yml', base_patch, raw=True)
            wait(lambda: any(x['id'].split(':')[-1] == 'guard-acceptance-widget' and x['status'] == 'ready'
                             for x in (g.bridge() or {}).get('capabilities', [])))
            g.control('restart')
            wait(lambda: g.probe()[0] == 'ready' and g.bridge() and g.bridge()['phase'] == 'idle')
            assert g.state['child']['pid'] != pid and not g.state.get('incident')
            save('planned-restart-without-recovery-budget')
            good_before_bad_plugin = g.state['known_good']
            pid = g.state['child']['pid']
            mod.atomic(widget / 'index.mjs', "export const name='guard-acceptance-widget';export function apply(){throw new Error('acceptance optional activation failure')}\n", raw=True)
            # Native partialReload may retain the previous healthy instance. Candidate audit must still reject the saved bad code.
            time.sleep(3)
            try:
                g.checkpoint()
                raise AssertionError('Bad optional plugin was accepted as a recovery point')
            except mod.Blocked:
                pass
            assert g.state['known_good'] == good_before_bad_plugin and g.state['child']['pid'] == pid
            save('bad-optional-source-cannot-become-good-checkpoint')
            generation = g.bridge()['generation']
            mod.atomic(widget / 'index.mjs', code('v2'), raw=True)
            wait(lambda: g.bridge() and g.bridge()['phase'] == 'idle' and g.bridge()['generation'] != generation)
            checkpoint = g.checkpoint()
            pid = g.state['child']['pid']
            mod.atomic(profile / 'cordis.patch.yml', '[UNCLOSED_ACCEPTANCE_YAML\n', raw=True)
            wait(lambda: g.bridge() and g.bridge()['phase'] == 'error')
            for _ in range(3):
                g.step()
            assert g.state['child']['pid'] == pid and not g.state.get('incident')
            assert g.state['known_good'] == checkpoint
            save('bad-yaml-hmr-keeps-live-host-and-good-checkpoint')
            mod.atomic(profile / 'cordis.patch.yml', base_patch, raw=True)
            wait(lambda: g.bridge() and g.bridge()['phase'] == 'idle')
            mod.atomic(widget / 'index.mjs', code('fatal', fatal=True), raw=True)
            wait(lambda: not mod.same_process(g.state.get('child')))
            wait(lambda: bool(g.state.get('run_home')) and g.probe()[0] == 'ready', timeout=240, tick=True)
            assert 'fatal callback' in (widget / 'index.mjs').read_text()
            assert link.is_symlink() and link.resolve() == widget
            assert (home / 'sessions/acceptance-sentinel').read_text() == 'preserve this existing data'
            save('real-plugin-fatal-callback-to-rescue-preserves-worktree', incident=g.state.get('incident'))
            mod.atomic(widget / 'index.mjs', code('v3'), raw=True)
            wait(lambda: not g.state.get('run_home') and g.probe()[0] == 'ready', timeout=240, tick=True)
            assert (home / 'widget-version').read_text() == 'v3'
            assert link.is_symlink() and link.resolve() == widget
            save('fixed-source-validates-and-returns-to-development')
            g.control('stop')
            reloaded = mod.Guard(g.directory)
            reloaded.step()
            assert reloaded.status()['mode'] == 'stopped' and not mod.listening(port)
            g = reloaded
            g.control('resume')
            g.step()
            wait(lambda: g.probe()[0] == 'ready')
            save('manual-stop-persistence-and-managed-resume')
        report.update(passed=True, completed=time.time())
    except BaseException as error:
        report.update(error=mod.redact(str(error)), status=g.status(), completed=time.time())
        raise
    finally:
        with contextlib.suppress(Exception):
            g.stop_child()
        if g.child and g.child.poll() is None:
            # Only this harness's contained Popen child; never production or stored PIDs.
            os.killpg(g.child.pid, signal.SIGINT)
            try:
                g.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(g.child.pid, signal.SIGKILL)
                g.child.wait()
        mod.atomic(root / 'report.json', report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    run(args.work.resolve(), args.baseline.resolve())
