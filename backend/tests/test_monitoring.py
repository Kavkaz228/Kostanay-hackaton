"""Monitoring uses configured facts and survives partial/stale input and restart."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import local_ai
from app.database import RobotCommand, ScadaRecord, User
from app.monitoring import ALERT, Monitoring
from app.service import Service
from test_api import client, db_url

ORIGIN = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)


@pytest.fixture
def monitored(tmp_path, monkeypatch):
    monkeypatch.setenv('MONITORING_AI_ENABLED', 'false')
    url = 'sqlite:///' + str(tmp_path / 'monitor.db')
    service = Service(url)
    monitor = Monitoring(service)
    yield service, monitor, url
    monitor.close()
    service.close()


def sample(service, seconds, value=65, **extra):
    row = {'robot_id': 'ROBOT-A', 'timestamp': (ORIGIN + timedelta(seconds=seconds)).isoformat(),
           'line_section': 'Сварка', 'cycle_status': 'In_Progress', 'error_code': '',
           'joint_temperature_c': value, **extra}
    with service._mutation():
        service.robot_rows = service._compact(service.robot_rows + [row])
        service.robot_configs.setdefault('ROBOT-A', {'expected_interval_seconds': 10,
            'limits': {'joint_temperature_c': {'high_warning': 60, 'high_critical': 80}}, 'error_codes': {}})
    return row


def scan(monitor, seconds):
    monitor.scan_once(ORIGIN + timedelta(seconds=seconds))
    return monitor.snapshot()


def sensor_alert(monitor):
    return next(a for a in monitor.snapshot()['alerts'] if a['type'] == 'limit:joint_temperature_c:high')


def test_empty_installation_and_deduplicated_escalation_acknowledgment(monitored):
    service, monitor, _ = monitored
    empty = scan(monitor, 0)
    assert empty['robots_monitored'] == 0 and empty['alerts'] == []
    sample(service, 0)
    first = scan(monitor, 1)['alerts'][0]
    assert first['severity'] == 'warning' and first['ai_status'] == 'not_needed'
    monitor.acknowledge(first['id'])
    scan(monitor, 2)
    assert len(monitor.snapshot()['alerts']) == 1
    assert monitor.snapshot()['unread_count'] == 0
    sample(service, 3, 90)
    alert = scan(monitor, 4)['alerts'][0]
    assert alert['id'] == first['id'] and alert['severity'] == 'critical'
    assert not alert['acknowledged'] and monitor.snapshot()['unread_count'] == 1
    assert service.source == 'simulation'  # monitoring is not tied to selected UI source


def test_recovery_needs_fresh_same_sensor_then_recurrence_gets_new_id(monitored):
    service, monitor, _ = monitored
    sample(service, 0, 90)
    original = scan(monitor, 1)['alerts'][0]
    sample(service, 2, None)
    assert scan(monitor, 3)['active_count'] == 1
    assert sensor_alert(monitor)['status'] == 'active'
    stale = scan(monitor, 50)
    assert stale['active_count'] == 2
    assert any(a['type'] == 'telemetry_stale' for a in stale['alerts'])
    assert 'свежих показаний' in sensor_alert(monitor)['description']
    sample(service, 51, None)
    assert scan(monitor, 52)['active_count'] == 1  # communication recovers, sensor does not
    sample(service, 53, 40)
    assert scan(monitor, 54)['active_count'] == 0
    assert sensor_alert(monitor)['resolved_at'] is not None
    sample(service, 55, 90)
    result = scan(monitor, 56)
    active = next(a for a in result['alerts'] if a['status'] == 'active')
    assert active['id'] != original['id'] and not active['acknowledged']


def test_stale_healthy_sample_cannot_resolve_and_future_sample_is_not_recovery(monitored):
    service, monitor, _ = monitored
    sample(service, 0, 90)
    scan(monitor, 1)
    sample(service, 2, 30)
    scan(monitor, 60)
    assert sensor_alert(monitor)['status'] == 'active'
    sample(service, 200, 30)
    state = scan(monitor, 61)
    assert sensor_alert(monitor)['status'] == 'active'
    assert any(a['type'] == 'telemetry_future' and a['status'] == 'active' for a in state['alerts'])


def test_stale_fault_discovered_on_first_scan_is_not_reported_as_current(monitored):
    service, monitor, _ = monitored
    sample(service, 0, 90)
    state = scan(monitor, 1000)
    assert state['active_count'] == 2
    fault = sensor_alert(monitor)
    assert fault['severity'] == 'critical' and 'последнее известное' in fault['description']
    assert fault['observed_at'] == ORIGIN.isoformat()


def test_no_invented_thresholds_zero_boundaries_and_rule_removal(monitored):
    service, monitor, _ = monitored
    sample(service, 0, 90)
    service.robot_configs['ROBOT-A']['limits'] = {}
    assert scan(monitor, 1)['active_count'] == 0
    service.robot_configs['ROBOT-A']['limits'] = {'joint_temperature_c': {'low_warning': 0}}
    sample(service, 2, 0)
    alert = scan(monitor, 3)['alerts'][0]
    assert alert['type'] == 'limit:joint_temperature_c:low'
    service.robot_configs['ROBOT-A']['limits'] = {'joint_temperature_c': {'high_warning': 80}}
    sample(service, 4, 30)
    assert scan(monitor, 5)['active_count'] == 1


def test_configured_error_recommendation_unknown_status_and_restart(monitored):
    service, monitor, url = monitored
    sample(service, 0, 40, error_code='E17', cycle_status='fault')
    service.robot_configs['ROBOT-A']['error_codes'] = {'E17': {
        'severity': 'critical', 'description': 'Ошибка датчика', 'action': 'Сверить датчик по карте М-17'}}
    first = scan(monitor, 1)['alerts'][0]
    assert 'карте М-17' in first['recommendation']
    monitor.acknowledge(first['id'])
    sample(service, 2, 40, cycle_status='unknown')
    scan(monitor, 3)
    assert monitor.snapshot()['active_count'] == 1
    # New objects and database connections must read the same acknowledgment.
    second_service = Service(url)
    second = Monitoring(second_service)
    try:
        persisted = second.snapshot()['alerts'][0]
        assert persisted['id'] == first['id'] and persisted['acknowledged']
        assert second.snapshot()['last_scan_at'] == (ORIGIN + timedelta(seconds=3)).isoformat()
        sample(second_service, 4, 40)
        assert scan(second, 5)['active_count'] == 0
    finally:
        second.close()
        second_service.close()


def test_ai_failure_retry_rate_is_bounded_and_does_not_fabricate_analysis(monitored, monkeypatch):
    service, monitor, _ = monitored
    monitor.ai_enabled = True
    sample(service, 0)
    alert = scan(monitor, 1)['alerts'][0]
    attempts = []
    def offline(*args, **kwargs):
        attempts.append(True)
        raise HTTPException(503, 'Локальная модель недоступна')
    monkeypatch.setattr(local_ai, 'ollama', offline)
    for _ in range(3):
        monitor.ai_resume_at = 0
        with monitor._write() as db:
            row = db.get(ScadaRecord, (ALERT, alert['id']))
            row.data = {**row.data, '_retry_at': 0}
        assert monitor.process_ai_once()
    monitor.ai_resume_at = 0
    assert not monitor.process_ai_once() and len(attempts) == 3  # backoff prevents hot retries
    result = sensor_alert(monitor)
    assert result['ai_status'] == 'unavailable' and result['ai_analysis'] is None
    assert result['ai_actions'] == [] and 'недоступна' in result['ai_error']
    assert result['recommendation'] and result['status'] == 'active'
    # After the capped backoff, an installed/recovered model can handle the alert.
    with monitor._write() as db:
        row = db.get(ScadaRecord, (ALERT, alert['id']))
        row.data = {**row.data, '_retry_at': 0}
    monkeypatch.setattr(local_ai, 'ollama', lambda *args, **kwargs: {'message': {'content': json.dumps({
        'analysis': 'Причина не установлена.', 'actions': ['Проверить датчик.']})}})
    assert monitor.process_ai_once() and sensor_alert(monitor)['ai_status'] == 'ready'
    with service.sessions() as db:
        assert db.scalar(select(RobotCommand)) is None


def test_ai_is_advisory_and_rejects_command_fields(monitored, monkeypatch):
    service, monitor, _ = monitored
    monitor.ai_enabled = True
    sample(service, 0)
    scan(monitor, 1)
    monkeypatch.setattr(local_ai, 'ollama', lambda *args, **kwargs: {'message': {'content': json.dumps({
        'analysis': 'Причина не установлена.', 'actions': ['Сверить показание датчика.'], 'action': 'hold'})}})
    assert monitor.process_ai_once()
    assert sensor_alert(monitor)['ai_status'] == 'unavailable'
    sample(service, 2, 90)  # escalation schedules a fresh analysis
    scan(monitor, 3)
    monitor.ai_resume_at = 0
    def model(path, payload, **kwargs):
        assert payload['format']['additionalProperties'] is False
        assert json.loads(payload['messages'][1]['content'])['commands_allowed'] is False
        return {'message': {'content': json.dumps({'analysis': 'Причина не установлена.', 'actions': ['Сверить показание датчика.']})}}
    monkeypatch.setattr(local_ai, 'ollama', model)
    assert monitor.process_ai_once()
    result = sensor_alert(monitor)
    assert result['ai_status'] == 'ready' and result['ai_analysis'] == 'Причина не установлена.'
    assert result['ai_observed_at'] == (ORIGIN + timedelta(seconds=2)).isoformat()
    with service.sessions() as db:
        assert db.scalar(select(RobotCommand)) is None


@pytest.mark.parametrize('change', ['escalation', 'recovery', 'close'])
def test_inference_never_blocks_scanner_and_late_result_is_discarded(monitored, monkeypatch, change):
    service, monitor, _ = monitored
    monitor.ai_enabled = True
    sample(service, 0)
    scan(monitor, 1)
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'message': {'content': json.dumps({'analysis': 'Старый анализ', 'actions': ['Сверить показания.']})}}
    monkeypatch.setattr(local_ai, 'ollama', slow)
    worker = threading.Thread(target=monitor.process_ai_once)
    worker.start()
    try:
        assert entered.wait(2)
        if change == 'close':
            monitor.close()
        else:
            sample(service, 2, 90 if change == 'escalation' else 30)
            scan(monitor, 3)  # completes while model is still waiting
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    result = sensor_alert(monitor)
    assert result['ai_analysis'] is None
    if change == 'escalation':
        assert result['severity'] == 'critical' and result['ai_status'] == 'pending'
    elif change == 'recovery':
        assert result['status'] == 'resolved' and result['ai_status'] == 'not_needed'


def test_monitoring_routes_authorization_and_shared_acknowledgment(client):
    assert client.post('/api/robots/measurements', json={'measurements': [{
        'robot_id': 'SEC-A', 'timestamp': datetime.now(timezone.utc).isoformat(),
        'cycle_status': 'fault', 'error_code': 'E1', 'joint_temperature_c': 45}]}).status_code == 200
    client.app.state.monitoring.scan_once()
    response = client.get('/api/monitoring')
    assert response.status_code == 200
    identifier = response.json()['alerts'][0]['id']
    path = '/api/monitoring/' + identifier + '/acknowledge'
    assert client.post(path, headers={'X-CSRF-Token': ''}).status_code == 403
    assert client.post(path).json()['acknowledged'] is True
    assert client.post('/api/monitoring/unknown/acknowledge').status_code == 404
    with client.app.state.service.sessions.begin() as db:
        user = db.scalar(select(User).where(User.username == 'admin'))
        user.role = 'viewer'
    client.app.state.security.invalidate()
    assert client.get('/api/monitoring').status_code == 200
    assert client.post(path).status_code == 403
    client.cookies.clear()
    assert client.get('/api/monitoring').status_code == 401
