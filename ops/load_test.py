"""HTTP load: independent users distributed across processes to avoid a client CPU bottleneck."""
import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import secrets
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import httpx
from sqlalchemy import delete
from sqlalchemy.engine import URL
from app.database import make_database, User, LoginSession, AccessToken, Snapshot, ScadaRecord
from app.security import password_hash


def percentile(values, p):
    return round(sorted(values)[min(len(values)-1, int(len(values)*p))]*1000, 2) if values else None


def viewer_worker(entries, count, run, started, duration, queue, detail):
    latencies, detail_latencies, sizes, errors = [], [], [], Counter()
    async def work():
        async def viewer(n, cookie):
            limits = httpx.Limits(max_connections=2, max_keepalive_connections=2)
            async with httpx.AsyncClient(base_url='http://web:8080', limits=limits, timeout=15, verify=False, headers={'Cookie': 'allur_session=' + cookie}) as client:
                tick = started + n/count*2
                while time.monotonic() < started+duration:
                    await asyncio.sleep(max(0, tick-time.monotonic()))
                    if time.monotonic() >= started+duration:
                        break
                    began = time.monotonic()
                    try:
                        response = await client.get('/api/dashboard')
                        latencies.append(time.monotonic()-began)
                        sizes.append(len(response.content))
                        if response.status_code != 200:
                            errors[f'dashboard:{response.status_code}'] += 1
                        else:
                            assert 'state' in response.json()
                        if detail:
                            began = time.monotonic()
                            if detail == 'manufacturing':
                                response = await client.get('/api/automation' if n%2 else '/api/manufacturing')
                            elif detail == 'scada':
                                response = await client.get('/api/scada')
                            elif detail == 'emulation':
                                response = await client.get('/api/emulation')
                                response.raise_for_status()
                                assert response.json()['source']=='emulation'
                                response = await client.get(f'/api/emulation/components/R{n%4+1}-J{n%6+1}-mot')
                            else:
                                response = await client.get('/api/robots/detail', params={'robot_id': f'LOAD-{run}-{n%100}'})
                            detail_latencies.append(time.monotonic()-began)
                            if response.status_code != 200:
                                errors[f'detail:{response.status_code}'] += 1
                            else:
                                content = response.json()
                                assert ('robots' in content or 'quality' in content) if detail == 'manufacturing' else 'components' in content if detail == 'scada' else 'sensors' in content if detail == 'emulation' else 'history' in content
                    except Exception as exc:
                        errors[type(exc).__name__] += 1
                    tick += 2
        await asyncio.gather(*(viewer(n, cookie) for n, cookie in entries))
    try:
        asyncio.run(work())
    except Exception as exc:
        errors['worker:' + type(exc).__name__] += 1
    queue.put({'latencies': latencies, 'detail': detail_latencies, 'sizes': sizes, 'errors': dict(errors)})


def main():
    if os.getenv('LOAD_TEST_ALLOWED') != 'isolated-only':
        raise SystemExit('Explicit LOAD_TEST_ALLOWED=isolated-only required; never run against customer data.')
    password = Path('/run/secrets/database_password').read_text().strip()
    url = URL.create('postgresql+psycopg', username='allur', password=password, host='db', database='allur').render_as_string(hide_password=False)
    engine, sessions = make_database(url)
    count = int(os.getenv('LOAD_USERS', '450'))
    duration = int(os.getenv('LOAD_SECONDS', '180'))
    process_count = min(5, count)
    attempt = secrets.token_hex(4)
    cookies, user_ids = [], []
    token = secrets.token_urlsafe(40)
    token_id = hashlib.sha256(token.encode()).hexdigest()
    with sessions.begin() as db:
        snapshot = db.get(Snapshot, 'application').data
        prior = [r for r in snapshot.get('robot_rows', []) if r['robot_id'].startswith('LOAD-')]
        run = max(prior, key=lambda r: r['timestamp'])['robot_id'].split('-')[1] if prior else attempt
        known_robots = len({r['robot_id'] for r in snapshot.get('robot_rows', [])})
        stored_before = snapshot.get('robot_count', 0)
        hashed = password_hash(secrets.token_urlsafe(32))
        for n in range(count):
            uid, raw = str(uuid4()), secrets.token_urlsafe(32)
            db.add(User(id=uid, username=f'load-{attempt}-{n}', password=hashed, role='viewer', active=True, must_change=False))
            db.add(LoginSession(id=hashlib.sha256(raw.encode()).hexdigest(), user_id=uid, csrf=secrets.token_urlsafe(32), expires=time.time()+3600))
            cookies.append(raw); user_ids.append(uid)
        db.add(AccessToken(id=token_id, name='isolated-load-' + attempt, expires=time.time()+3600, active=True))
        if os.getenv('LOAD_VIEW')=='scada':
            from app.scada_schema import AssetBody, ComponentBody
            for n in range(100):
                robot_id=f'LOAD-{run}-{n}'
                aid='QA-SCADA-'+robot_id
                if not db.get(ScadaRecord,('asset',aid)):
                    a=AssetBody(id=aid,name='QA нагрузочный робот',kind='robot',robot_id=robot_id).model_dump(mode='json')
                    db.add(ScadaRecord(kind='asset',id=aid,owner='',created_at=time.time(),data={**a,'request':a}))
                    c=ComponentBody(id=aid+'-sensor',asset_id=aid,name='QA датчик узла',robot_metric_map={'joint_temperature_c':'temperature_c'},thresholds=[{'metric':'temperature_c','label':'Температура','warning':80.0,'alarm':100.0}]).model_dump(mode='json')
                    db.add(ScadaRecord(kind='component',id=c['id'],owner=aid,created_at=time.time(),data={**c,'request':c,'revision':1,'wear_pct':None,'last':None,'states':{},'active_alarms':{},'resource_incomplete':False}))
    origin = datetime.now(timezone.utc)
    def payload(batch):
        return {'measurements': [{'timestamp': (origin+timedelta(seconds=batch)).isoformat(), 'robot_id': f'LOAD-{run}-{n}', 'cycle_status': 'In_Progress', 'joint_temperature_c': 40+n/10, 'vibration_mm_s': 1.2} for n in range(100)]}
    def headers(batch):
        return {'Authorization': 'Bearer ' + token, 'Idempotency-Key': f'load-{attempt}-{batch:08}'}
    workers = []
    errors, ingestion = Counter(), []
    latencies, detail_latencies, sizes = [], [], []
    try:
        with httpx.Client(base_url='http://web:8080', timeout=30) as client:
            response = client.post('/api/robots/measurements', headers=headers(0), json=payload(0))
            response.raise_for_status()
        started = time.monotonic()+3
        context = mp.get_context('spawn')
        queue = context.Queue()
        for worker in range(process_count):
            entries = [(n, cookies[n]) for n in range(worker, count, process_count)]
            detail_mode = os.getenv('LOAD_VIEW', 'cards') if os.getenv('LOAD_DETAIL', 'true') == 'true' else None
            process = context.Process(target=viewer_worker, args=(entries, count, run, started, duration, queue, detail_mode))
            process.start(); workers.append(process)
        async def produce():
            async with httpx.AsyncClient(base_url='http://web:8080', timeout=15) as client:
                batch = 1
                while time.monotonic() < started+duration:
                    await asyncio.sleep(max(0, started+batch-1-time.monotonic()))
                    if time.monotonic() >= started+duration:
                        break
                    began = time.monotonic()
                    try:
                        response = await client.post('/api/robots/measurements', headers=headers(batch), json=payload(batch))
                        ingestion.append(time.monotonic()-began)
                        if response.status_code != 200:
                            errors[f'ingestion:{response.status_code}'] += 1
                    except Exception as exc:
                        errors['ingestion:' + type(exc).__name__] += 1
                    batch += 1
        asyncio.run(produce())
        for _ in workers:
            result = queue.get(timeout=45)
            latencies.extend(result['latencies']); detail_latencies.extend(result['detail']); sizes.extend(result['sizes']); errors.update(result['errors'])
    finally:
        for process in workers:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate(); process.join()
                errors['worker_terminated'] += 1
        with sessions.begin() as db:
            db.execute(delete(LoginSession).where(LoginSession.user_id.in_(user_ids)))
            db.execute(delete(User).where(User.id.in_(user_ids)))
            db.execute(delete(AccessToken).where(AccessToken.id == token_id))
        engine.dispose()
    result = {'users': count, 'secondary_view': os.getenv('LOAD_VIEW','cards'), 'secondary_requests':len(detail_latencies), 'secondary_p95_ms':percentile(detail_latencies,.95), 'load_generator_processes': process_count+1, 'duration_seconds': round(time.monotonic()-started, 2),
        'known_robots_before': known_robots, 'stored_measurements_before': stored_before,
        'dashboard_requests': len(latencies), 'dashboard_rps': round(len(latencies)/duration, 2), 'dashboard_p50_ms': percentile(latencies,.5), 'dashboard_p95_ms': percentile(latencies,.95), 'dashboard_p99_ms': percentile(latencies,.99),
        'robot_detail_requests': len(detail_latencies), 'robot_detail_p95_ms': percentile(detail_latencies,.95),
        'ingestion_batches': len(ingestion), 'measurements_sent': len(ingestion)*100, 'warmup_measurements': 100, 'ingestion_p95_ms': percentile(ingestion,.95),
        'mean_dashboard_bytes': round(statistics.mean(sizes)) if sizes else 0, 'errors': dict(errors)}
    print(json.dumps(result, indent=2), flush=True)
    Path('/results/load-test.json').write_text(json.dumps(result, indent=2))
    if errors or (result['dashboard_p95_ms'] or 99999)>1000 or (result['robot_detail_p95_ms'] or 0)>1000 or len(ingestion)<duration*.95:
        raise SystemExit('Load acceptance criteria failed')


if __name__ == '__main__':
    main()
