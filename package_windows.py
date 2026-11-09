"""Build a source-only Windows preview ZIP with an exact SHA-256 file inventory."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = ('dsh_guard.py', 'launcher.py', 'windows_platform.py', 'console.py', 'validate.mjs',
         'ui/index.html', 'bridge/package.json', 'bridge/index.mjs', 'bridge/cordis.patch.yml',
         'windows_setup.py', 'windows_command.py', 'windows_acceptance.py',
         'Install-DshGuard.ps1', 'Test-DshGuard.ps1', 'test_windows.py', 'test_console.py',
         'test_launcher.py', 'test_dsh_guard.py', 'README.md', 'README.zh.md', 'README.i18n.yaml',
         'WINDOWS.md', 'ACCEPTANCE.md', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md',
         'docs/environment.md', 'docs/macos.md', 'bridge/README.md', 'bridge/README.zh.md',
         'bridge/README.i18n.yaml', 'bridge/test-bridge.mjs', 'IMPLEMENTATION.md',
         'macos_setup.py', 'command.py', 'configure-watch.mjs', 'acceptance_development.py',
         'test_publication.py', '.agents/notes/implemented/architecture/external-supervisor.md',
         '.agents/notes/implemented/architecture/windows-supervision-preview.md',
         '.agents/notes/implemented/architecture/public-enrollment.md')


def build(output):
    output = Path(output).resolve()
    checksum = output.with_suffix(output.suffix + '.sha256')
    if output.exists() or checksum.exists():
        raise FileExistsError('Use a fresh output path; existing packages are preserved')
    payload = {}
    for name in FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or linked package file: ' + name)
        payload[name] = path.read_bytes()
    manifest = {'schema': 1, 'name': 'dsh-guard-windows-supervision-preview',
                'native_windows_acceptance': 'not-run', 'contained_recovery_enabled': False,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    payload['windows-package.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            archive.writestr('dsh-guard-windows-preview/' + name, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    with checksum.open('x', encoding='ascii') as stream:
        stream.write(digest + '  ' + output.name + '\n')
    return {'archive': str(output), 'sha256': digest, 'files': len(payload),
            'native_windows_acceptance': 'not-run', 'contained_recovery_enabled': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))
