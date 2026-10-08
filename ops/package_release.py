"""Build an offline Docker release without installation data or Ollama identity keys."""
import argparse
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

ROOT = Path('/workspace')
NAME = 'Allur-Twin-2026-10-08'
BUNDLE = ROOT / 'output' / NAME
OLLAMA_IMAGE = 'allur-twin-ollama:2026-10-08'
OLLAMA_UPSTREAM = 'ollama/ollama@sha256:292ee7945dfc3d5840a181f3ab86fedb1e66703e02c8af98b50f4da56b7e278c'
CADDY_UPSTREAM = 'caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
FORBIDDEN = {'.env', '.secrets', '.test-installation', 'backups', 'node_modules', 'gateway.json', 'id_ed25519', 'id_ed25519.pub', '.release-check-authorized', '.release-check-only', '.release-check-password', '.release-check-result.json'}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def copy(relative):
    source = ROOT / relative
    destination = BUNDLE / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def refresh():
    for folder in ('docs', 'gateway', 'samples'):
        for path in sorted((ROOT / folder).rglob('*')):
            relative = path.relative_to(ROOT)
            if path.is_file() and not (set(relative.parts) & FORBIDDEN) and 'data' not in relative.parts and '__pycache__' not in relative.parts:
                copy(relative)
    for relative in ('ops/init.py', 'docker/backup.sh', 'docker/Caddyfile', 'compose.production.yaml', 'backup.ps1', 'restore-check.ps1', 'VERSION'):
        copy(relative)
    if (ROOT / 'ops/release_check.py').exists():
        copy('ops/release_check.py')
    production = BUNDLE / 'compose.production.yaml'
    production.write_text(production.read_text(encoding='utf-8').replace(CADDY_UPSTREAM, 'allur-twin-caddy:2.10.2'), encoding='utf-8')
    for path in (ROOT / 'ops/portable').iterdir():
        if path.is_file():
            target = BUNDLE / ('ops/portable_runtime.py' if path.name == 'runtime.py' else path.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            if target.suffix == '.sh':
                target.chmod(0o755)


def prepare():
    BUNDLE.mkdir(parents=True, exist_ok=True)
    if any((BUNDLE / part).exists() for part in ('.env', '.secrets', 'backups')):
        raise ValueError('Release staging contains an installation. Use a separate extracted copy for testing.')
    compose = (ROOT / 'compose.yaml').read_text(encoding='utf-8')
    compose = compose.replace('name: allur-twin\n', 'name: allur-twin-portable\n', 1)
    compose = re.sub(r'^    build:\n(?:^      .*\n)+', '', compose, flags=re.MULTILINE)
    compose = re.sub(r'^  tests:\n.*?(?=^volumes:)', '', compose, flags=re.MULTILINE | re.DOTALL)
    compose = compose.replace(OLLAMA_UPSTREAM, OLLAMA_IMAGE)
    assert 'build:' not in compose and '  tests:' not in compose and '  e2e:' not in compose
    (BUNDLE / 'compose.yaml').write_text(compose, encoding='utf-8')
    (BUNDLE / '.env.example').write_text('PORT=8088\nCOMPOSE_PROJECT_NAME=allur-twin-portable\nCOOKIE_SECURE=false\nAI_MODEL=qwen3:4b\nMONITORING_AI_ENABLED=true\nPHYSICAL_CONTROL_ENABLED=false\n', encoding='utf-8')
    refresh()
    print(f'Release directory prepared: {BUNDLE}', flush=True)


def export_model():
    source = Path('/modelroot')
    manifest_path = Path('models/manifests/registry.ollama.ai/library/qwen3/4b')
    model = json.loads((source / manifest_path).read_text())
    files = [manifest_path]
    for layer in [model['config'], *model['layers']]:
        if not re.fullmatch('sha256:[a-f0-9]{64}', layer['digest']):
            raise ValueError('Invalid model blob digest')
        relative = Path('models/blobs') / layer['digest'].replace(':', '-')
        if sha256(source / relative) != layer['digest'].split(':')[1]:
            raise ValueError(f'Model blob checksum mismatch: {relative}')
        files.append(relative)
    target = BUNDLE / 'models/qwen3-4b.tar'
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, 'w') as archive:
        for relative in files:
            info = archive.gettarinfo(str(source / relative), arcname=relative.as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o644
            with (source / relative).open('rb') as stream:
                archive.addfile(info, stream)
    license_layer = next(layer for layer in model['layers'] if layer['mediaType'].endswith('.license'))
    license_target = BUNDLE / 'licenses/Qwen3-4B-LICENSE.txt'
    license_target.parent.mkdir(exist_ok=True)
    shutil.copyfile(source / 'models/blobs' / license_layer['digest'].replace(':', '-'), license_target)
    target.with_suffix('.tar.sha256').write_text(f'{sha256(target)}  {target.name}\n', encoding='ascii')
    print(f'Exported qwen3:4b only: {len(files)} files, {target.stat().st_size} bytes; no identity keys.', flush=True)


def index():
    refresh()
    large_digests = {}
    for filename in ('images.tar', 'models/qwen3-4b.tar'):
        target = BUNDLE / filename
        large_digests[filename] = sha256(target)
        target.with_suffix('.tar.sha256').write_text(f'{large_digests[filename]}  {target.name}\n', encoding='ascii')
    shutil.copyfile(ROOT / 'output/allur-twin.zip', BUNDLE / 'source.zip')
    images = json.loads((BUNDLE / 'images.metadata.json').read_text(encoding='utf-8-sig'))
    (BUNDLE / 'images.expected.txt').write_text(''.join(f"{image['name']} {image['id']}\n" for image in images), encoding='ascii')
    files = sorted(p for p in BUNDLE.rglob('*') if p.is_file() and p.name not in ('manifest.json', 'SHA256SUMS'))
    for path in files:
        if set(path.relative_to(BUNDLE).parts) & FORBIDDEN:
            raise ValueError(f'Installation-specific file in release: {path.relative_to(BUNDLE)}')
    manifest = {
        'name': 'Allur Twin', 'version': '2.0.0', 'build': '2026-10-08', 'platform': 'linux/amd64',
        'images': images, 'upstream_ollama': OLLAMA_UPSTREAM, 'upstream_caddy': CADDY_UPSTREAM,
        'included_model': 'qwen3:4b', 'optional_downloads': ['Gateway dependencies for connecting real equipment'],
        'files': {p.relative_to(BUNDLE).as_posix(): large_digests.get(p.relative_to(BUNDLE).as_posix()) or sha256(p) for p in files},
    }
    (BUNDLE / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sums = manifest['files'] | {'manifest.json': sha256(BUNDLE / 'manifest.json')}
    (BUNDLE / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in sorted(sums.items())), encoding='utf-8')
    print(f'Indexed {len(files)} files: {BUNDLE}', flush=True)


def finish():
    index()
    archive = ROOT / 'output' / (NAME + '-full-docker.zip')
    with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zipped:
        for path in sorted(BUNDLE.rglob('*')):
            if path.is_file():
                compression = ZIP_STORED if path.suffix in ('.tar', '.zip') else ZIP_DEFLATED
                zipped.write(path, NAME + '/' + path.relative_to(BUNDLE).as_posix(), compress_type=compression)
    with ZipFile(archive) as zipped:
        assert zipped.testzip() is None
    checksum = sha256(archive)
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': checksum}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'export-model', 'index', 'finish'])
    args = parser.parse_args()
    {'prepare': prepare, 'export-model': export_model, 'index': index, 'finish': finish}[args.phase]()
