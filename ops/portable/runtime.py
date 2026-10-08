"""Offline installer helper. Runs in the bundled API image with no network."""
import argparse
import hashlib
import json
import os
import runpy
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path('/workspace')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def verify():
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    for relative, expected in manifest['files'].items():
        parts = PurePosixPath(relative)
        if parts.is_absolute() or '..' in parts.parts:
            raise ValueError('Unsafe manifest path')
        # The host checks images.tar before trusting the API image; avoid hashing it twice.
        if relative != 'images.tar' and digest(ROOT / relative) != expected:
            raise ValueError(f'Release checksum mismatch: {relative}')
    print('Release files verified (SHA-256).', flush=True)


def init():
    runpy.run_path(str(ROOT / 'ops/init.py'), run_name='__main__')
    (ROOT / 'backups').mkdir(mode=0o700, exist_ok=True)


def import_model():
    target = Path('/modelroot')
    manifest_name = 'models/manifests/registry.ollama.ai/library/qwen3/4b'
    with tarfile.open(ROOT / 'models/qwen3-4b.tar', 'r:') as archive:
        members = archive.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or '..' in path.parts or path.parts[0] != 'models':
                raise ValueError(f'Unsafe model archive entry: {member.name}')
        model_bytes = archive.extractfile(manifest_name).read()
        model = json.loads(model_bytes)
        allowed = {manifest_name} | {'models/blobs/' + layer['digest'].replace(':', '-') for layer in [model['config'], *model['layers']]}
        if {member.name for member in members} != allowed:
            raise ValueError('Model archive contains files outside qwen3:4b')
        existing_manifest = target / manifest_name
        if existing_manifest.exists() and existing_manifest.read_bytes() != model_bytes:
            print('Existing customized qwen3:4b manifest preserved; offline model import skipped.', flush=True)
            return
        # Check every existing blob before writing anything. Never replace a modified model file.
        for member in members:
            destination = target / member.name
            if destination.exists() and member.name != manifest_name:
                if not destination.is_file() or digest(destination) != destination.name.removeprefix('sha256-'):
                    raise ValueError(f'Existing model blob differs; preserved without overwrite: {member.name}')
        added = 0
        # Publish the manifest only after all referenced blobs are complete.
        for member in sorted(members, key=lambda item: item.name == manifest_name):
            destination = target / member.name
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.allur-import-', delete=False) as stream:
                    temporary = Path(stream.name)
                    shutil.copyfileobj(archive.extractfile(member), stream, length=8 * 1024 * 1024)
                if member.name != manifest_name and digest(temporary) != destination.name.removeprefix('sha256-'):
                    raise ValueError(f'Model checksum mismatch: {member.name}')
                temporary.chmod(0o644)
                # Hard linking publishes atomically and refuses to replace any concurrent writer.
                os.link(temporary, destination)
                added += 1
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    print(f'Offline qwen3:4b ready: {added} new files; existing model files preserved.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['verify', 'init', 'import-model'])
    args = parser.parse_args()
    {'verify': verify, 'init': init, 'import-model': import_model}[args.action]()
