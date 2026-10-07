import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from auth_client import TestClient

from app.main import create_app
from test_api import client, db_url, upload, csv_content
from test_robots import SAMPLE, HEADER


def ingest(client, rows):
    return client.post('/api/robots/measurements', json={'measurements': rows})


def measurement(second=0, value=40, **extra):
    return {'timestamp': (datetime(2026, 10, 5, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat(),
            'robot_id': 'A', 'cycle_status': 'In_Progress', 'joint_temperature_c': value, **extra}


def configure(client, limits=None, **extra):
    return client.put('/api/robots/config?robot_id=A', json={'limits': limits or {}, **extra})


def test_preview_checks_without_mutating_and_log(client):
    before = client.get('/api/state').json()
    response = client.post('/api/import/preview', files={'file': ('robots.csv', SAMPLE.encode())})
    assert response.status_code == 200
    assert response.json()['count'] == 5 and len(response.json()['identifiers']) == 3
    assert client.get('/api/state').json() == before
    assert client.get('/api/imports').json() == []
    assert upload(client, SAMPLE.encode()).status_code == 200
    assert client.get('/api/imports').json()[0]['count'] == 5
    assert client.post('/api/import/preview', files={'file': ('again.csv', SAMPLE.encode())}).status_code == 422
    assert len(client.get('/api/imports').json()) == 1


def test_production_preview_checks_durable_counters(client):
    content = csv_content(client)
    assert client.post('/api/import/preview', files={'file': ('plant.csv', content)}).json()['kind'] == 'production'
    assert upload(client, content).status_code == 200
    assert client.post('/api/import/preview', files={'file': ('plant.csv', content)}).status_code == 422


def test_statistics_linear_trend_exact_and_range(client):
    assert ingest(client, [measurement(i * 20, 40 + i, vibration_mm_s=0.5) for i in range(10)]).status_code == 200
    assert configure(client, {'joint_temperature_c': {'high_warning': 55, 'high_critical': 65}}).status_code == 200
    result = client.get('/api/robots/analytics?robot_id=A').json()
    stats = result['sensors']['joint_temperature_c']
    assert stats['count'] == 10 and stats['mean'] == 44.5 and stats['min'] == 40 and stats['max'] == 49
    assert stats['trend']['per_minute'] == 3 and stats['trend']['r_squared'] == 1
    assert stats['trend']['crossing']['eta_minutes'] == 2
    assert stats['trend']['crossing']['timestamp'] == '2026-10-05T00:05:00+00:00'
    assert result['status_seconds'] == {'In_Progress': 180}
    assert result['unknown_seconds'] == 0
    params = {'robot_id': 'A', 'start': '2026-10-05T00:01:00Z', 'end': '2026-10-05T00:02:00Z'}
    rows = client.get('/api/robots/history', params={**params, 'limit': 2, 'offset': 1}).json()
    assert rows['total'] == 4 and [r['joint_temperature_c'] for r in rows['rows']] == [44, 45]
    exported = list(csv.DictReader(io.StringIO(client.get('/api/robots/export', params=params).content.decode('utf-8-sig'))))
    assert len(exported) == 4 and exported[0]['robot_id'] == 'A'
    assert client.get('/api/robots/analytics', params={'robot_id': 'A', 'start': params['end'], 'end': params['start']}).status_code == 422
    assert client.get('/api/robots/history?robot_id=A&start=2026-10-05').status_code == 422


def test_trend_with_missing_data_and_long_gaps(client):
    rows = [measurement(i * 20, 40 + i) for i in range(6)]
    rows += [measurement(1000, None, vibration_mm_s=1)]
    assert ingest(client, rows).status_code == 200
    stats = client.get('/api/robots/analytics?robot_id=A').json()
    assert stats['unknown_seconds'] == 900
    assert stats['sensors']['joint_temperature_c']['trend'] is None
    assert stats['sensors']['joint_temperature_c']['missing'] == 1
    empty = client.get('/api/robots/analytics?robot_id=A&start=2030-01-01T00:00:00Z').json()
    assert empty['measurements'] == 0 and empty['sensors']['joint_temperature_c']['mean'] is None


def test_no_forecast_for_noisy_or_insufficient_series(client):
    assert ingest(client, [measurement(i * 20, v) for i, v in enumerate([10, 90, 20, 80, 30, 70, 40, 60])]).status_code == 200
    configure(client, {'joint_temperature_c': {'high_warning': 100}})
    stats = client.get('/api/robots/analytics?robot_id=A').json()['sensors']['joint_temperature_c']
    assert stats['trend']['r_squared'] < .8 and stats['trend']['crossing'] is None


def test_limits_lifecycle_missing_sensor_and_persistence(client, db_url):
    assert ingest(client, [measurement(0, 40, vibration_mm_s=1)]).status_code == 200
    config = {'joint_temperature_c': {'high_warning': 45, 'high_critical': 50}, 'vibration_mm_s': {'low_warning': .5, 'high_warning': 2}}
    assert configure(client, config).status_code == 200
    assert ingest(client, [measurement(20, 45, vibration_mm_s=.4)]).status_code == 200
    active = client.get('/api/incidents').json()
    assert len(active) == 2 and all(i['severity'] == 'warning' for i in active)
    assert ingest(client, [measurement(40, 51, vibration_mm_s=None)]).status_code == 200
    active = client.get('/api/incidents').json()
    assert len(active) == 2 and all(i['status'] == 'open' for i in active)
    assert next(i for i in active if 'temperature' in i['kind'])['severity'] == 'critical'
    with TestClient(create_app(db_url, runner_enabled=False)) as restored:
        assert restored.get('/api/robots/config?robot_id=A').json()['limits']['joint_temperature_c']['high_warning'] == 45
        assert len(restored.get('/api/incidents').json()) == 2
        assert len(restored.get('/api/imports').json()) == 3
    assert ingest(client, [measurement(60, 40, vibration_mm_s=1)]).status_code == 200
    assert all(i['status'] == 'resolved' for i in client.get('/api/incidents').json())


def test_dictionary_and_config_does_not_switch_source(client):
    ingest(client, [measurement(error_code='W-203', cycle_status='Warning')])
    client.post('/api/source', json={'source': 'simulation'})
    simulation = client.get('/api/state').json()
    assert configure(client, error_codes={'W-203': {'description': 'Описание пользователя', 'action': 'Проверить по регламенту', 'severity': 'critical'}}).status_code == 200
    after = client.get('/api/state').json()
    assert after['source'] == 'simulation' and after['run_id'] == simulation['run_id']
    client.post('/api/source', json={'source': 'telemetry', 'telemetry_kind': 'robots'})
    incident = client.get('/api/incidents').json()[0]
    assert incident['severity'] == 'critical' and 'Описание пользователя' in incident['description']
    assert 'Проверить по регламенту' in incident['description']


@pytest.mark.parametrize('body', [
    {'limits': {'vibration_mm_s': {'high_warning': 5, 'high_critical': 3}}},
    {'limits': {'vibration_mm_s': {'low_warning': -1}}},
    {'limits': {'unknown_sensor': {'high_warning': 10}}},
    {'error_codes': {'0': {'description': 'Wrong'}}},
    {'expected_interval_seconds': 0},
    {'expected_interval_seconds': True},
])
def test_invalid_config_is_atomic(client, body):
    ingest(client, [measurement()])
    before = client.get('/api/robots/config?robot_id=A').json()
    assert client.put('/api/robots/config?robot_id=A', json=body).status_code == 422
    assert client.get('/api/robots/config?robot_id=A').json() == before


def test_json_batch_is_atomic_and_checks_unknown_fields(client):
    assert ingest(client, [measurement(), measurement(1, -300)]).status_code == 422
    assert client.get('/api/robots/history?robot_id=A').json()['total'] == 0
    assert ingest(client, [measurement(extra_field=10)]).status_code == 422
    assert ingest(client, [measurement()]).status_code == 200
    assert ingest(client, [measurement()]).status_code == 422
    assert client.get('/api/robots/history?robot_id=A').json()['total'] == 1


def test_long_robot_id_and_code_use_bounded_incident_key(client):
    assert ingest(client, [measurement(robot_id='R' * 128, error_code='E' * 128)]).status_code == 200
    assert len(client.get('/api/incidents').json()) == 1


def test_bulk_transitions_keep_every_incident_and_paginate(client):
    rows = [measurement(i, 40, cycle_status='Warning' if i % 2 == 0 else 'In_Progress') for i in range(1000)]
    assert ingest(client, rows).status_code == 200
    incidents = client.get('/api/incidents').json()
    assert len(incidents) == 500 and all(i['status'] == 'resolved' for i in incidents)
    first = client.get('/api/robots/history?robot_id=A&limit=100&offset=900').json()
    assert first['total'] == 1000 and len(first['rows']) == 100
    assert first['rows'][0]['timestamp'] == rows[0]['timestamp']
    assert first['rows'][-1]['timestamp'] == rows[99]['timestamp']


def test_rule_removal_resolves_only_its_events(client):
    ingest(client, [measurement(0, 51, error_code='W')])
    configure(client, {'joint_temperature_c': {'high_warning': 45}})
    assert len(client.get('/api/state').json()['predictions']) == 2
    configure(client, {})
    events = client.get('/api/incidents').json()
    assert next(i for i in events if i['kind'].startswith('limit:'))['status'] == 'resolved'
    assert next(i for i in events if i['kind'].startswith('robot_error:'))['status'] == 'open'


def test_old_open_alarm_remains_visible_and_full_export_is_available(client):
    ingest(client, [measurement(0, 40, robot_id='OLD', cycle_status='Alarm')])
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['timestamp', 'robot_id', 'cycle_status', 'joint_temperature_c'])
    writer.writeheader()
    writer.writerows(measurement(i, 40, cycle_status='Warning' if i % 2 else 'Idle') for i in range(1, 4003))
    assert upload(client, output.getvalue().encode()).status_code == 200
    events = client.get('/api/incidents').json()
    assert len(events) == 2000 and events[0]['station_id'] == 'OLD' and events[0]['status'] == 'open'
    exported = list(csv.DictReader(io.StringIO(client.get('/api/incidents/export').content.decode('utf-8-sig'))))
    telemetry = [r for r in exported if r['source'] == 'telemetry']
    assert len(telemetry) == 2002
