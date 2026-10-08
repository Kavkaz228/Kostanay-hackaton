"""Build a distributable source archive without customer data, secrets or test output."""
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path('/workspace')
output = root / 'output'
output.mkdir(exist_ok=True)
archive = output / 'allur-twin.zip'
excluded = {'.update-backups', '.git', '.secrets', '.test-installation', 'backups', 'output', 'node_modules', 'dist', '__pycache__', '.pytest_cache', '.venv', '.profiles', 'results', 'playwright-report', '.release-check-authorized', '.release-check-only', '.release-check-password', '.release-check-result.json', 'id_ed25519', 'id_ed25519.pub'}
with ZipFile(archive, 'w', ZIP_DEFLATED) as zipped:
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if not path.is_file() or set(relative.parts) & excluded or relative.as_posix() == 'gateway/gateway.json' or relative.parts[:2] == ('gateway', 'data') or path.name == '.env' or (path.name.startswith('.env.') and path.name != '.env.example') or path.suffix in ('.zip', '.db', '.sqlite', '.sqlite3', '.dump', '.tsbuildinfo', '.log', '.key', '.pem'):
            continue
        zipped.write(path, 'allur-twin/' + relative.as_posix())
with ZipFile(archive) as zipped:
    assert zipped.testzip() is None
    assert not any('/.secrets/' in name or '/backups/' in name or name.endswith('/.env') for name in zipped.namelist())
    count = len(zipped.namelist())
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(output / 'allur-twin.zip.sha256').write_text(digest + '  allur-twin.zip\n')
print(f'Archive verified: {count} files, {archive.stat().st_size} bytes, SHA256 {digest}')
