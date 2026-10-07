import csv
import io

import pytest
from auth_client import TestClient

from app.main import create_app
from test_api import client, db_url, csv_content, upload

HEADER = 'timestamp,robot_id,line_section,joint_temperature_c,vibration_mm_s,hydraulic_pressure_bar,cycle_status,error_code\n'
SAMPLE = HEADER + '''2026-10-05T10:00:00Z,R-042,Welding_Shop,42.5,1.2,150.1,In_Progress,0
2026-10-05T10:00:01Z,R-042,Welding_Shop,42.8,1.5,149.8,In_Progress,0
2026-10-05T10:00:02Z,R-043,Assembly_Line,38.1,0.8,0.0,Idle,0
2026-10-05T10:00:03Z,R-042,Welding_Shop,45.2,4.8,155.4,Warning,W-203
2026-10-05T10:00:04Z,R-044,Painting_Shop,24.1,0.4,90.2,In_Progress,0
'''


def test_sample_and_persistence(client, db_url):
    simulation = client.get('/api/state').json()
    assert upload(client, SAMPLE.encode()).status_code == 200
    state = client.get('/api/state').json()
    assert state['telemetry_kind'] == 'robots'
    assert state['observation_count'] == 5 and len(state['robots']) == 3
    assert all(v is None for v in state['metrics'].values())
    assert state['stations'] == []
    assert state['robots'][0]['vibration_mm_s'] == 4.8
    history = client.get('/api/robots/history?robot_id=R-042&limit=2').json()
    assert history['total'] == 3 and len(history['rows']) == 2
    incidents = client.get('/api/incidents').json()
    assert len(incidents) == 1 and incidents[0]['title'] == 'R-042: W-203'
    assert incidents[0]['severity'] == 'warning'
    assert incidents[0]['created_at'] == '2026-10-05T10:00:03+00:00'
    assert client.post('/api/scenarios', json={'name': 'Test', 'horizon_minutes': 60, 'changes': []}).status_code in (409, 422)
    exported = list(csv.DictReader(io.StringIO(client.get('/api/robots/export').content.decode('utf-8-sig'))))
    assert len(exported) == 5 and exported[3]['error_code'] == 'W-203'
    assert client.post('/api/source', json={'source': 'simulation'}).json()['run_id'] == simulation['run_id']
    client.post('/api/source', json={'source': 'telemetry'})
    with TestClient(create_app(db_url, runner_enabled=False)) as restored:
        assert restored.get('/api/state').json()['run_id'] == state['run_id']
        assert restored.get('/api/robots/history?robot_id=R-042').json()['total'] == 3


def test_append_transient_warning_and_atomic_duplicate(client):
    assert upload(client, SAMPLE.encode()).status_code == 200
    before = client.get('/api/state').json()
    assert upload(client, SAMPLE.encode()).status_code == 422
    assert client.get('/api/state').json() == before
    incident = client.get('/api/incidents').json()[0]
    assert client.post(f"/api/incidents/{incident['id']}/acknowledge").status_code == 200
    appended = HEADER + '2026-10-05T10:00:05Z,R-042,Welding_Shop,43,1,150,In_Progress,0\n2026-10-05T10:00:06Z,R-042,Welding_Shop,43,1,150,Warning,W-204\n2026-10-05T10:00:07Z,R-042,Welding_Shop,43,1,150,In_Progress,0\n'
    assert upload(client, appended.encode()).status_code == 200
    incidents = client.get('/api/incidents').json()
    assert len(incidents) == 2 and all(i['status'] == 'resolved' for i in incidents)
    assert {i['resolved_at'] for i in incidents} == {'2026-10-05T10:00:05+00:00', '2026-10-05T10:00:07+00:00'}
    assert client.get('/api/state').json()['predictions'] == []


def test_aliases_units_missing_values_and_unsorted_rows(client):
    data = 'cycle_status,pneumatic_pressure,node_id,timestamp,joint_temperature_c\nIdle,6,N1,2026-10-05T15:00:01+05:00,\nWelding,,N1,2026-10-05T10:00:00Z,-20\n'
    assert upload(client, data.encode()).status_code == 200
    state = client.get('/api/state').json()
    row = state['robots'][0]
    assert row['timestamp'] == '2026-10-05T10:00:01+00:00'
    assert row['pneumatic_pressure'] == 6 and row['pneumatic_pressure_bar'] is None
    assert row['joint_temperature_c'] is None
    assert client.get('/api/robots/history?robot_id=N1').json()['rows'][0]['joint_temperature_c'] == -20


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-1', 'abc'])
def test_invalid_sensor_atomic(client, bad):
    data = SAMPLE.replace('42.5,1.2,150.1', f'42.5,{bad},150.1')
    before = client.get('/api/state').json()
    assert upload(client, data.encode()).status_code == 422
    assert client.get('/api/state').json() == before


@pytest.mark.parametrize('data', [
    SAMPLE.replace('robot_id', 'wrong_id'),
    SAMPLE.replace('robot_id,', 'robot_id,robot_id,'),
    SAMPLE.replace('10:00:00Z', '10:00:00'),
    SAMPLE + SAMPLE.splitlines()[1] + '\n',
    HEADER,
    SAMPLE.replace('R-042,Welding_Shop,42.5,1.2,150.1', 'R-042,Welding_Shop,,,'),
])
def test_invalid_structure(client, data):
    assert upload(client, data.encode()).status_code == 422


def test_chat_backslashes_bom_and_production_compatibility(client):
    prod = csv_content(client)
    assert upload(client, prod).status_code == 200
    prod_run = client.get('/api/state').json()['run_id']
    chat = '\n'.join(line + '\\' for line in SAMPLE.splitlines())
    assert upload(client, chat.encode('utf-8-sig')).status_code == 200
    robot_run = client.get('/api/state').json()['run_id']
    client.post('/api/source', json={'source': 'simulation'})
    newer = csv_content(client, start=2)
    assert upload(client, newer).status_code == 200
    assert client.get('/api/state').json()['run_id'] == prod_run
    assert len(client.get('/api/history').json()) == 4
    extra = HEADER + '2026-10-05T10:00:05Z,R-044,Painting_Shop,25,0.4,90.2,Idle,0\n'
    assert upload(client, extra.encode()).status_code == 200
    assert client.get('/api/state').json()['run_id'] == robot_run
    assert client.get('/api/robots/history?robot_id=R-042').json()['total'] == 3

def test_saved_series_selection(client):
    assert client.post('/api/source', json={'source': 'telemetry', 'telemetry_kind': 'robots'}).status_code == 409
    assert upload(client, csv_content(client)).status_code == 200
    production = client.get('/api/state').json()['run_id']
    assert upload(client, SAMPLE.encode()).status_code == 200
    robot = client.get('/api/state').json()['run_id']
    response = client.post('/api/source', json={'source': 'telemetry', 'telemetry_kind': 'production'})
    assert response.status_code == 200 and response.json()['run_id'] == production
    response = client.post('/api/source', json={'source': 'telemetry', 'telemetry_kind': 'robots'})
    assert response.status_code == 200 and response.json()['run_id'] == robot
    assert response.json()['telemetry_available'] == {'production': True, 'robots': True}
    assert len(client.get('/api/incidents').json()) == 1
    assert client.post('/api/source', json={'source': 'simulation', 'telemetry_kind': 'robots'}).status_code == 422
