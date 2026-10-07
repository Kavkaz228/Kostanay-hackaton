import hashlib
import time
from fastapi.testclient import TestClient as AnonymousClient
from sqlalchemy import select, delete
from app.main import create_app
from app.database import LoginSession, User, Observation, Snapshot, Audit
from app.security import password_hash
from test_api import client, db_url
from test_analytics import measurement


def test_authentication_csrf_roles_and_immediate_revocation(client):
    app = client.app
    with AnonymousClient(app, cookies={}) as anonymous:
        assert anonymous.get('/api/health').status_code == 200
        assert anonymous.get('/api/state').status_code == 401
        assert anonymous.get('/api/docs').status_code == 401
        assert anonymous.post('/api/auth/login', json={'username': 'admin', 'password': 'incorrect'}).status_code == 401
    # Restarting an app in nested TestClient closes the shared service: use existing app's engine (SQLite reconnects).
    raw = client.cookies.get('allur_session')
    assert client.post('/api/control', json={'running': False}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert client.post('/api/control', json={'running': False}, headers={'Origin': 'https://attacker.example'}).status_code == 403
    user = client.post('/api/admin/users', json={'username': 'observer', 'password': 'Observer temporary 2026!', 'role': 'viewer'}).json()
    with app.state.service.sessions.begin() as db:
        observer = db.get(User, user['id'])
        observer.must_change = False
        db.add(LoginSession(id=hashlib.sha256(b'viewer-token').hexdigest(), user_id=observer.id, csrf='viewer-csrf', expires=time.time()+3600))
    client.cookies.clear(); client.cookies.set('allur_session', 'viewer-token')
    client.headers['X-CSRF-Token'] = 'viewer-csrf'
    assert client.get('/api/state').status_code == 200
    assert client.get('/api/admin/users').status_code == 403
    assert client.post('/api/source', json={'source': 'simulation'}).status_code == 403
    client.cookies.clear(); client.cookies.set('allur_session', raw)
    with app.state.service.sessions() as db:
        client.headers['X-CSRF-Token'] = db.get(LoginSession, hashlib.sha256(raw.encode()).hexdigest()).csrf
    assert client.patch('/api/admin/users/' + user['id'], json={'active': False}).status_code == 200
    client.cookies.clear(); client.cookies.set('allur_session', 'viewer-token')
    assert client.get('/api/state').status_code == 401


def test_real_login_password_rotation_and_logout(client):
    user = client.post('/api/admin/users', json={'username': 'employee', 'password': 'Temporary employee 2026!', 'role': 'operator'}).json()
    client.cookies.clear()
    login = client.post('/api/auth/login', json={'username': 'employee', 'password': 'Temporary employee 2026!'})
    assert login.status_code == 200
    assert 'HttpOnly' in login.headers['set-cookie'] and 'SameSite=strict' in login.headers['set-cookie']
    assert client.get('/api/state').status_code == 403
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    assert client.post('/api/auth/password', json={'current_password': 'wrong', 'new_password': 'New employee phrase 2027!'}).status_code == 403
    old_cookie = client.cookies.get('allur_session')
    assert client.post('/api/auth/password', json={'current_password': 'Temporary employee 2026!', 'new_password': 'New employee phrase 2027!'}).status_code == 200
    client.cookies.set('allur_session', old_cookie)
    assert client.get('/api/state').status_code == 401
    client.cookies.clear()
    login = client.post('/api/auth/login', json={'username': 'employee', 'password': 'New employee phrase 2027!'})
    assert login.status_code == 200 and not login.json()['must_change']
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/state').status_code == 401


def test_machine_token_scope_idempotency_and_revoke(client):
    result = client.post('/api/admin/tokens', json={'name': 'gateway', 'days': 1}).json()
    headers = {'Authorization': 'Bearer ' + result['token'], 'Idempotency-Key': 'batch-000001'}
    assert client.get('/api/state', headers=headers).status_code == 403
    payload = {'measurements': [measurement()]}
    first = client.post('/api/robots/measurements', json=payload, headers=headers)
    assert first.status_code == 200
    assert client.post('/api/robots/measurements', json=payload, headers=headers).json() == first.json()
    assert client.get('/api/robots/history?robot_id=A').json()['total'] == 1
    assert client.post('/api/robots/measurements', json={'measurements': [measurement(1)]}, headers=headers).status_code == 409
    assert client.post('/api/robots/measurements', json=payload, headers={'Authorization': headers['Authorization']}).status_code == 422
    assert client.delete('/api/admin/tokens/' + result['id']).status_code == 200
    assert client.post('/api/robots/measurements', json=payload, headers=headers).status_code == 401


def test_compact_snapshot_preserves_history_and_transactional_audit(client):
    assert client.post('/api/robots/measurements', json={'measurements': [measurement(i) for i in range(1000)]}).status_code == 200
    with client.app.state.service.sessions() as db:
        saved = db.get(Snapshot, 'application').data
        assert saved['storage_version'] == 2 and saved['robot_count'] == 1000
        assert len(saved['robot_rows']) <= 7
        assert len(list(db.scalars(select(Observation)))) == 1000
        assert db.scalar(select(Audit).where(Audit.action == 'POST /api/robots/measurements')) is not None
    assert client.get('/api/robots/history?robot_id=A&limit=5&offset=10').json()['rows'][0]['timestamp'] == measurement(985)['timestamp']
    assert len(client.get('/api/robots/export').text.strip().splitlines()) == 1001


def test_login_rate_limit_and_secret_not_exposed(client):
    for _ in range(8):
        assert client.post('/api/auth/login', json={'username': 'nobody', 'password': 'incorrect'}).status_code == 401
    assert client.post('/api/auth/login', json={'username': 'nobody', 'password': 'incorrect'}).status_code == 429
    assert 'scrypt$' not in client.get('/api/admin/users').text
    assert 'incorrect' not in client.get('/api/admin/audit').text


def test_v1_snapshot_migrates_once_without_losing_measurements(client, db_url):
    from app.service import Service
    client.post('/api/robots/measurements', json={'measurements': [measurement(i) for i in range(20)]})
    with client.app.state.service.sessions.begin() as db:
        saved = db.get(Snapshot, 'application')
        legacy = dict(saved.data)
        legacy.pop('storage_version'); legacy.pop('robot_count')
        legacy['robot_rows'] = [r.data for r in db.scalars(select(Observation).order_by(Observation.timestamp))]
        saved.data = legacy
        db.execute(delete(Observation))
    for _ in range(2):
        restored = Service(db_url)
        try:
            assert restored.robot_history('A', 100)['total'] == 20
            assert restored.state()['observation_count'] == 20
            assert len(restored.robot_rows) <= 7
        finally:
            restored.close()


def test_simultaneous_retries_commit_one_batch(client):
    from concurrent.futures import ThreadPoolExecutor
    payload = {'measurements': [measurement()]}
    def send(_):
        return client.post('/api/robots/measurements', json=payload, headers={'Idempotency-Key': 'concurrent-retry-0001'})
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(send, range(8)))
    assert all(r.status_code == 200 for r in replies)
    assert all(r.json() == replies[0].json() for r in replies)
    assert client.get('/api/robots/history?robot_id=A').json()['total'] == 1


def test_robot_storage_failure_rolls_back_rows_receipt_and_cache(client, monkeypatch):
    payload = {'measurements': [measurement(cycle_status='Warning')]}
    before = client.get('/api/state').json()
    original = client.app.state.service._reconcile
    def fail(*args, **kwargs):
        raise RuntimeError('injected disk failure')
    monkeypatch.setattr(client.app.state.service, '_reconcile', fail)
    assert client.post('/api/robots/measurements', json=payload, headers={'Idempotency-Key': 'retry-after-failure'}).status_code == 500
    assert client.get('/api/state').json() == before
    assert client.get('/api/robots/history?robot_id=A').json()['total'] == 0
    monkeypatch.setattr(client.app.state.service, '_reconcile', original)
    assert client.post('/api/robots/measurements', json=payload, headers={'Idempotency-Key': 'retry-after-failure'}).status_code == 200


def test_combined_robot_detail_has_real_history_analytics_and_config(client):
    assert client.post('/api/robots/measurements', json={'measurements': [measurement(i) for i in range(600)]}).status_code == 200
    result = client.get('/api/robots/detail?robot_id=A').json()
    assert result['history']['total'] == 600 and len(result['history']['rows']) == 500
    assert result['analytics']['measurements'] == 600
    assert result['config']['expected_interval_seconds'] == 60
    assert result['warning'] is None


def test_slow_reader_does_not_block_ingestion_or_cache_a_stale_result(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    client.post('/api/robots/measurements', json={'measurements': [measurement()]})
    entered, release = Event(), Event()
    original = client.app.state.service._robot_range
    def slow(*args, **kwargs):
        rows = original(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return rows
    monkeypatch.setattr(client.app.state.service, '_robot_range', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(client.get, '/api/robots/detail?robot_id=A')
        assert entered.wait(5)
        try:
            result = client.post('/api/robots/measurements', json={'measurements': [measurement(1)]})
            assert result.status_code == 200
        finally:
            release.set()
        assert pending.result().status_code == 200
    assert client.get('/api/robots/detail?robot_id=A').json()['history']['total'] == 2


def test_live_detail_freshness_is_bounded_and_raw_history_is_current(client):
    import time
    twin = client.app.state.service
    client.post('/api/robots/measurements', json={'measurements': [measurement()]})
    first = client.get('/api/robots/detail?robot_id=A').json()
    assert first['history']['total'] == 1 and first['computed_at']
    client.post('/api/robots/measurements', json={'measurements': [measurement(1)]})
    assert client.get('/api/robots/history?robot_id=A').json()['total'] == 2
    assert client.get('/api/robots/detail?robot_id=A').json()['history']['total'] == 1
    key = ('detail', 'A', None, None, 0)
    twin.read_cache[key] = (time.monotonic()-6, twin.read_cache[key][1])
    assert client.get('/api/robots/detail?robot_id=A').json()['history']['total'] == 2
    # Configuration/control mutations invalidate all views immediately.
    with twin._mutation():
        pass
    assert key not in twin.read_cache
