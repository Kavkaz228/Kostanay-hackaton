"""Verify that a second state writer is refused by PostgreSQL."""
import os
from pathlib import Path
from sqlalchemy.engine import URL
from app.service import Service

if os.getenv('LOAD_TEST_ALLOWED') != 'isolated-only':
    raise SystemExit('Disposable deployment only')
url = URL.create('postgresql+psycopg', username='allur', password=Path('/run/secrets/database_password').read_text().strip(), host='db', database='allur').render_as_string(hide_password=False)
try:
    second = Service(url)
except RuntimeError as exc:
    assert 'Another application owns' in str(exc)
    print('PASS: second state writer rejected; existing application and data unchanged.')
else:
    second.close()
    raise SystemExit('FAIL: duplicate state writer was allowed')
