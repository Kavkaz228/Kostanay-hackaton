"""Run in Docker, bind-mounted at /workspace. Never overwrite existing secrets."""
import os
import secrets
from pathlib import Path

root = Path('/workspace') / ('.test-installation' if os.getenv('TEST_INSTALLATION') == 'true' else '')
root.mkdir(mode=0o700, exist_ok=True)
owner = Path('/workspace').stat()
os.chown(root, owner.st_uid, owner.st_gid)
directory = root / '.secrets'
directory.mkdir(mode=0o700, exist_ok=True)
os.chown(directory, owner.st_uid, owner.st_gid)
directory.chmod(0o700)
for name in ('database_password', 'bootstrap_password'):
    target = directory / name
    if not target.exists():
        with target.open('x') as stream:
            stream.write(secrets.token_urlsafe(36))
    # Directory is owner-only on the host; Compose bind-mounts each file individually
    # into non-root containers and does not support remapping uid/mode for file secrets.
    target.chmod(0o444)
    os.chown(target, owner.st_uid, owner.st_gid)
env = root / '.env'
if not env.exists():
    env.write_text('PORT=8088\nCOOKIE_SECURE=false\n')
    os.chown(env, owner.st_uid, owner.st_gid)
print('Installation secrets are ready. Initial login: admin; password: .secrets/bootstrap_password. Change it on first login.')
