"""Exercise more than the previous 200,000-row limit through authenticated CSV API."""
import csv
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import httpx

if os.getenv('LOAD_TEST_ALLOWED') != 'isolated-only':
    raise SystemExit('Disposable deployment only')
run = uuid4().hex[:8]
origin = datetime.now(timezone.utc)
started = time.monotonic()
with httpx.Client(base_url='http://web:8080', timeout=120) as client:
    login = client.post('/api/auth/login', json={'username': 'admin', 'password': 'Isolated E2E password 2026!'})
    login.raise_for_status()
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    before = client.get('/api/admin/system').json()['robot_measurements']
    for batch in range(11):
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['timestamp', 'robot_id', 'cycle_status', 'joint_temperature_c'])
        for i in range(20000):
            step = batch*20000+i
            writer.writerow([(origin+timedelta(seconds=step//100)).isoformat(), f'CAPACITY-{run}-{step%100:03}', 'In_Progress', 40+(step%100)/10])
        response = client.post('/api/import', files={'file': ('capacity.csv', output.getvalue().encode(), 'text/csv')})
        response.raise_for_status()
        assert response.json()['imported'] == 20000
    after = client.get('/api/admin/system').json()['robot_measurements']
    assert after-before == 220000
    history = client.get('/api/robots/history', params={'robot_id': f'CAPACITY-{run}-000', 'limit': 500, 'offset': 1700}).json()
    assert history['total'] == 2200 and len(history['rows']) == 500
    response = client.get('/api/robots/export', params={'robot_id': f'CAPACITY-{run}-000'})
    assert len(list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))) == 2200
result = {'imported': after-before, 'total_measurements': after, 'duration_seconds': round(time.monotonic()-started, 2), 'history_pagination': 'passed', 'export_count': 2200}
print(json.dumps(result, indent=2))
Path('/results/storage-check.json').write_text(json.dumps(result, indent=2))
