"""Prepare/finalize an image-based release in Docker, without installation data."""
import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path('/workspace')
BUNDLE = ROOT / 'output' / 'Allur-twin-2.0'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def prepare():
    BUNDLE.mkdir(parents=True, exist_ok=True)
    compose = (ROOT / 'compose.yaml').read_text(encoding='utf-8')
    compose = compose.replace('name: allur-twin\n', 'name: allur-twin-2-0\n', 1)
    compose = re.sub(r'^    build:\n(?:^      .*\n)+', '', compose, flags=re.MULTILINE)
    compose = re.sub(r'^  tests:\n.*?(?=^volumes:)', '', compose, flags=re.MULTILINE | re.DOTALL)
    assert 'build:' not in compose and '  tests:' not in compose and '  e2e:' not in compose
    (BUNDLE / 'compose.yaml').write_text(compose, encoding='utf-8')
    paths = ['ops/init.py', 'docker/backup.sh', 'docker/Caddyfile', 'compose.production.yaml',
             'start-ai.ps1', 'stop.ps1', 'backup.ps1', 'restore-check.ps1', 'VERSION',
             'docs/Allur twin 2.0 - Характеристика приложения.docx',
             'docs/APPLICATION_CHARACTERISTICS.md', 'docs/OPERATIONS.md',
             'docs/EMULATION.md', 'docs/SCADA_MERGE.md', 'docs/SCADA_PARITY.md',
             'docs/MANUFACTURING_AND_AI.md', 'docs/RELEASE_2_0.md', 'docs/SCADA_FULL_VERIFICATION.md',
             'docs/load-scada-full.json', 'docs/AUTOMATION_AND_THEMES.md',
             'docs/AUTOMATION_THEME_VERIFICATION.md']
    for relative in paths:
        source = ROOT / relative
        if not source.exists():
            raise FileNotFoundError(source)
        destination = BUNDLE / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    for name in ['start.ps1', 'start.sh', 'README.md']:
        shutil.copyfile(ROOT / 'ops' / 'portable' / name, BUNDLE / name)
    (BUNDLE / 'start.sh').chmod(0o755)
    print(f'Release directory prepared: {BUNDLE}')


def finish():
    image_archive = BUNDLE / 'images.tar'
    digest = sha256(image_archive)
    (BUNDLE / 'images.tar.sha256').write_text(f'{digest}  images.tar\n', encoding='ascii')
    source_archive = ROOT / 'output' / 'allur-twin.zip'
    shutil.copyfile(source_archive, BUNDLE / 'Allur-twin-2.0-source.zip')
    forbidden = {'.env', '.secrets', '.test-installation', 'backups', 'node_modules', 'gateway.json'}
    files = sorted(p for p in BUNDLE.rglob('*') if p.is_file())
    for path in files:
        if set(path.relative_to(BUNDLE).parts) & forbidden:
            raise ValueError(f'Installation-specific file in release: {path.relative_to(BUNDLE)}')
    manifest = {'name': 'Allur twin 2.0', 'version': '2.0.0', 'platform': 'linux/amd64',
                'images': ['allur-twin-api:2.0.0', 'allur-twin-web:2.0.0', 'postgres:17.6-alpine'],
                'optional_downloads': ['Ollama image and qwen3:4b model', 'Caddy for HTTPS'],
                'files': {p.relative_to(BUNDLE).as_posix(): sha256(p) for p in files if p.name != 'manifest.json'}}
    (BUNDLE / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    archive = ROOT / 'output' / 'Allur-twin-2.0-docker.zip'
    with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in sorted(BUNDLE.rglob('*')):
            if path.is_file():
                zipped.write(path, 'Allur-twin-2.0/' + path.relative_to(BUNDLE).as_posix())
    with ZipFile(archive) as zipped:
        assert zipped.testzip() is None
    checksum = sha256(archive)
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': checksum}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'finish'])
    args = parser.parse_args()
    (prepare if args.phase == 'prepare' else finish)()
