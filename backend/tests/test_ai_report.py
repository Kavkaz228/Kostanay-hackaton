"""The assistant's visible facts must preserve source, values and provenance."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.ai_report import build_context, build_report
from app.database import RobotCommand, ScadaRecord, Snapshot
from app.service import Service


@pytest.fixture
def twin(tmp_path):
    service = Service('sqlite:///' + str(tmp_path / 'assistant-facts.db'))
    yield service
    service.close()


def add_robot(twin, identifier='PHYSICAL-ONLY', value=85, *, seconds=0, **extra):
    row = dict(robot_id=identifier, timestamp=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
        cycle_status='running', line_section='Сварка', error_code='', joint_temperature_c=value, **extra)
    with twin._mutation():
        twin.robot_rows = twin._compact(twin.robot_rows + [row])
        twin.robot_configs.setdefault(identifier, {'expected_interval_seconds': 60,
            'limits': {'joint_temperature_c': {'high_warning': 60, 'high_critical': 80}}, 'error_codes': {}})
    return row


def metrics(report):
    return {m['key']: m['value'] for m in report['metrics']}


def test_empty_plant_reports_missing_not_zero_quality_or_simulation(twin):
    context = build_context(twin, 'plant')
    report = build_report(context)
    assert context['vehicle_quality']['total'] == 0
    assert report['has_data'] is False
    assert metrics(report)['rejected'] is None
    assert metrics(report)['quantity'] is None
    assert report['findings'] == [] and report['readings'] == []
    assert any('CSV' in step and 'timestamp' in step for step in report['next_steps'])
    assert 'simulation' not in context and 'stand' not in context
    with twin.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ScadaRecord)) == 0
        assert db.scalar(select(func.count()).select_from(RobotCommand)) == 0


def test_zero_rejected_is_real_value_when_quality_records_exist(twin):
    twin.quality_records([dict(timestamp=datetime.now(timezone.utc).isoformat(), record_id='qa-1',
        brand='Test', model='Car', color='White', quantity=12, rejected=0)])
    report = build_report(build_context(twin, 'plant'))
    assert report['has_data']
    assert metrics(report)['quantity'] == 12
    assert metrics(report)['accepted'] == 12
    assert metrics(report)['rejected'] == 0
    assert report['observed_at'] is not None


def test_plant_detects_configured_limit_without_scanning_or_mutation(twin):
    add_robot(twin)
    context = build_context(twin, 'plant')
    report = build_report(context)
    assert context['robot_configs']['PHYSICAL-ONLY']['limits']['joint_temperature_c']['high_critical'] == 80
    event = next(f for f in report['findings'] if f['id'].endswith('limit:joint_temperature_c:high'))
    assert event['severity'] == 'critical'
    assert '85' in event['evidence'] and '80' in event['evidence']
    assert 'настроенную границу' in event['recommendation']
    reading = next(r for r in report['readings'] if r['id'] == 'PHYSICAL-ONLY:joint_temperature_c')
    assert reading['value'] == 85 and reading['unit'] == '°C' and reading['status'] == 'fresh'
    with twin.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ScadaRecord)) == 0


def test_partial_packet_preserves_sensor_time_and_zero_reading(twin):
    old = add_robot(twin, value=85, seconds=-600)
    add_robot(twin, value=None, vibration_mm_s=0)
    report = build_report(build_context(twin, 'plant'))
    values = {r['id']: r for r in report['readings']}
    temperature = values['PHYSICAL-ONLY:joint_temperature_c']
    assert temperature['value'] == 85 and temperature['observed_at'] == old['timestamp']
    assert temperature['status'] == 'stale'
    assert values['PHYSICAL-ONLY:vibration_mm_s']['value'] == 0
    assert any('устарело' in f['evidence'] for f in report['findings'])


def test_monitor_events_are_visible_deduplicated_and_scoped(twin):
    add_robot(twin, identifier='R-A')
    add_robot(twin, identifier='R-B', value=45)
    with twin.sessions.begin() as db:
        for identifier in ('R-A', 'R-B'):
            data = dict(id='alert-' + identifier, robot_id=identifier, type='limit:joint_temperature_c:high',
                status='active', severity='critical', title=identifier + ' temperature', description='85 > 80',
                observed_at=datetime.now(timezone.utc).isoformat(), recommendation='Проверить датчик', acknowledged=False,
                _context={'secret_internal_field': 'not-for-model'})
            db.add(ScadaRecord(kind='monitor_alert', id=data['id'], owner=identifier, created_at=1, data=data))
    context = build_context(twin, 'plant', 'R-A')
    report = build_report(context)
    assert context['monitor_active_count'] == 1
    assert len(report['findings']) == 1
    assert report['findings'][0]['id'] == 'monitor:alert-R-A'
    assert 'R-B' not in json.dumps(context)
    assert 'secret_internal_field' not in json.dumps(context)


def test_emulation_has_sensor_values_recommendations_and_no_fake_oee(twin):
    add_robot(twin)
    context = build_context(twin, 'emulation', 'R2')
    report = build_report(context)
    assert report['has_data'] and context['stand']['component_count'] > 12
    assert len(context['stand']['components']) == 12
    assert all(c['robot'] == 'R2' for c in context['stand']['components'])
    assert 'PHYSICAL-ONLY' not in json.dumps(context)
    assert context['stand']['summary']['oee_pct'] is None
    assert metrics(report)['oee'] is None
    assert metrics(report)['cars'] == 0
    assert report['findings'] and all(f['recommendation'] for f in report['findings'])
    assert any(r['id'] == 'R2-J3-red:wear' and r['value'] == 93 for r in report['readings'])
    assert next(r for r in report['readings'] if r['id'] == 'R2-calib:drift')['observed_at'] == context['stand']['model_time']
    assert any('warn' in s and 'alarm' in s for c in context['stand']['components'] for s in c['sensors'])
    reducer = next(f for f in report['findings'] if f['id'] == 'component:R2-J3-red')
    assert '3.18' in reducer['evidence'] and '1.8' in reducer['evidence']  # vibration and its actual configured boundary
    calibration = next(f for f in report['findings'] if f['id'] == 'component:R2-calib')
    assert '1.49' in calibration['evidence'] and 'граница сверху 1' in calibration['evidence']
    assert len(report['metrics']) <= 8 and len(report['readings']) <= 36 and len(report['findings']) <= 12
    with twin.sessions() as db:
        assert db.get(Snapshot, 'scada-emulation-v1') is None  # Preview did not persist/reset/step a stand.
        assert db.scalar(select(func.count()).select_from(RobotCommand)) == 0


def test_simulation_uses_engine_even_when_dashboard_source_is_measurements(twin):
    add_robot(twin)
    twin.source = 'telemetry'
    twin.telemetry = {'source': 'telemetry', 'private_plant_marker': 'PHYSICAL-ONLY'}
    context = build_context(twin, 'simulation')
    report = build_report(context)
    assert context['simulation']['source'] == 'simulation'
    assert report['has_data']
    assert 'PHYSICAL-ONLY' not in json.dumps(context)
    assert 'vehicle_quality' not in context and 'stand' not in context
    assert metrics(report)['quality'] is None
    assert metrics(report)['oee'] is None
    assert any(f['id'] == 'simulation:bottleneck' for f in report['findings'])
    assert twin.source == 'telemetry'


def test_simulation_completed_shift_has_exact_plan_result(twin):
    twin.engine.advance(28800)
    context = build_context(twin, 'simulation')
    report = build_report(context)
    assert metrics(report)['good'] == twin.engine.metrics()['good']
    assert metrics(report)['plan_progress'] == twin.engine.metrics()['plan_progress']
    assert any(f['id'] == 'simulation:shift' and str(twin.engine.shift_plan) in f['evidence'] for f in report['findings'])


def test_context_bounds_keep_selected_robot_and_counts_truthful(twin):
    for index in range(15):
        add_robot(twin, identifier=f'REAL-{index:02}', value=30)
    context = build_context(twin, 'plant')
    assert context['robot_count'] == 15 and len(context['robots']) == 10
    assert metrics(build_report(context))['robots'] == 15
    selected = build_context(twin, 'plant', 'REAL-14')
    assert selected['robot_count'] == 1 and selected['robots'][0]['robot_id'] == 'REAL-14'


def test_large_robot_fleet_does_not_hide_daily_plan_evidence(twin):
    for index in range(10):
        add_robot(twin, identifier=f'REAL-{index:02}', value=30, vibration_mm_s=0,
            motor_current_a=5, speed_percent=50, hydraulic_pressure_bar=20)
    context = build_context(twin, 'plant')
    context['daily_report'] = {'lines': [{'line': 'Сварка', 'date': '2026-10-07', 'actual': 90, 'plan': 100}], 'imported_at': now()}
    report = build_report(context)
    assert len(report['readings']) <= 36
    assert any(r['id'] == 'daily:0' and r['value'] == '90 / 100' for r in report['readings'])
    assert any(f['id'] == 'daily:0' and 'недовыпуск 10' in f['evidence'] for f in report['findings'])


def now():
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.parametrize('source,robot,allow', [('wrong', None, False), ('simulation', 'R1', False),
    ('simulation', None, True), ('emulation', 'PHYSICAL', False), ('emulation', None, True)])
def test_reject_source_and_physical_command_mismatch(twin, source, robot, allow):
    with pytest.raises(HTTPException) as error:
        build_context(twin, source, robot, allow)
    assert error.value.status_code == 422
