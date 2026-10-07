"""Read-only profiling of the disposable deployment's robot views."""
import cProfile
import os
import pstats
import threading
from pathlib import Path
from sqlalchemy.engine import URL
from app.database import make_database, Snapshot
from app.service import Service

if os.getenv('LOAD_TEST_ALLOWED') != 'isolated-only':
    raise SystemExit('Disposable test deployment only')
url = URL.create('postgresql+psycopg', username='allur', password=Path('/run/secrets/database_password').read_text().strip(), host='db', database='allur').render_as_string(hide_password=False)
service = Service.__new__(Service)
service.database, service.sessions = make_database(url)
service.lock = threading.RLock()
service.read_cache = {}
service.cache_locks = {}
service.cache_guard = threading.Lock()
service.cache_revision = 0
with service.sessions() as db:
    service._restore(db.get(Snapshot, 'application').data)
ids = list(dict.fromkeys(r['robot_id'] for r in service.robot_rows if r['robot_id'].startswith('LOAD-')))[-100:]
profile = cProfile.Profile()
profile.enable()
for robot in ids:
    service.robot_detail(robot)
profile.disable()
pstats.Stats(profile).strip_dirs().sort_stats('cumulative').print_stats(22)
service.database.dispose()
