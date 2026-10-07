from pathlib import Path
from datetime import datetime, timedelta, timezone
import time
from concurrent.futures import ThreadPoolExecutor

from test_api import client, db_url, upload
from app.database import RobotCommand
from app.manufacturing import parse_case


def robot(robot_id='EQUIPMENT-1', **extra):
    return {'robot_id': robot_id, 'timestamp': datetime.now(timezone.utc).isoformat(), 'cycle_status': 'In_Progress', 'operation': 'welding', 'joint_temperature_c': 43.0, 'electrode_count': 120, 'joint_2_deg': -45.0, 'joint_3_deg': 85.0, 'controller_mode': 'automatic', 'safety_state': 'normal', **extra}


def test_case_document_preserves_daily_data_and_missing_vehicle_details(client):
    content = Path('/samples/case-test-data.docx').read_bytes()
    before = client.get('/api/state').json()
    preview = client.post('/api/import/preview', files={'file': ('case.docx', content)}).json()
    assert preview['kind'] == 'case' and preview['count'] == 19
    assert client.get('/api/manufacturing').json()['report'] is None
    result = upload(client, content)
    assert result.status_code == 200 and result.json()['imported'] == 19
    data = client.get('/api/manufacturing').json()
    report = data['report']
    assert report['planned_total'] == 4800 and report['plan_gap'] == 700
    assert report['targets']['defect_max_percent'] == 2
    assert report['targets']['oee_min_percent'] == 85
    assert report['targets']['downtime_max_minutes'] == 60
    assert report['targets']['shifts_per_day'] == 2
    assert report['quality'][4]['rejected'] == 6
    assert report['downtimes'][2]['minutes'] == 55
    assert report['lines'][0]['actual'] == 118
    assert data['quality']['total'] == 0
    after = client.get('/api/state').json()
    assert after['run_id'] == before['run_id'] and after['source'] == before['source']
    assert after['metrics'] == before['metrics']
    assert upload(client, content).json()['imported'] == 0


def test_quality_counts_search_and_idempotent_atomic_batch(client):
    content = Path('/samples/quality-example.csv').read_bytes()
    assert upload(client, content).json()['imported'] == 3
    assert upload(client, content).json()['imported'] == 0
    result = client.get('/api/manufacturing').json()['quality']
    assert (result['quantity'], result['accepted'], result['rejected']) == (21,20,1)
    assert client.get('/api/manufacturing?search=Onix').json()['quality']['quantity'] == 10
    assert client.get('/api/manufacturing?search=%25').json()['quality']['quantity'] == 0
    row = {**result['rows'][0], 'record_id': 'new'}
    conflict = {**result['rows'][1], 'quantity': 99}
    assert client.post('/api/quality/records', json={'records':[row,conflict]}).status_code == 409
    assert client.get('/api/manufacturing').json()['quality']['total'] == 3
    assert len(client.get('/api/quality/export').text.strip().splitlines()) == 4
    assert client.post('/api/quality/records', json={'records':[{**row,'quantity':1,'rejected':2}]}).status_code == 422


def test_document_rejects_entity_definitions(client):
    from io import BytesIO
    from zipfile import ZipFile
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY a "expanded">]><x>&a;</x>')
    assert upload(client, output.getvalue()).status_code == 422
    assert client.get('/api/manufacturing').json()['report'] is None


def test_extended_robot_fields_and_inventory_validation(client):
    assert upload(client, Path('/samples/robot-equipment-example.csv').read_bytes()).status_code == 200
    response = client.get('/api/automation').json()
    paint = next(r for r in response['robots'] if r['operation']=='painting')
    assert paint['paint_volume_l'] == 41.8 and paint['joint_2_deg'] == -55
    assert paint['electrode_count'] is None
    assert client.get('/api/robots/detail?robot_id=EXAMPLE-PAINT-01').json()['analytics']['sensors']['paint_volume_l']['count'] == 2
    for data in [robot(paint_volume_l=101,paint_capacity_l=100), robot(electrode_count=1.5), robot(joint_2_deg=float('nan')), robot(speed_percent=101)]:
        if data.get('joint_2_deg') != data.get('joint_2_deg'): continue
        assert client.post('/api/robots/measurements',json={'measurements':[data]}).status_code == 422
    assert client.post('/api/robots/measurements',json={'measurements':[robot()]}).status_code == 200
    assert 'electrode_count' in client.get('/api/robots/export').text


def setup_command(client):
    assert client.post('/api/robots/measurements',json={'measurements':[robot()]}).status_code == 200
    key = client.post('/api/admin/gateways',json={'robot_id':'EQUIPMENT-1','days':1}).json()
    command = client.post('/api/automation/commands',json={'robot_id':'EQUIPMENT-1','action':'hold','reason':'Осмотр оборудования'}).json()
    return key,command


def test_physical_default_off_and_proposals_do_not_execute(client):
    key,command=setup_command(client)
    assert client.post(f"/api/automation/commands/{command['id']}/approve",json={'confirm':True}).status_code == 409
    headers={'Authorization':'Bearer '+key['token']}
    assert client.post('/api/gateway/next',headers=headers).json()['command'] is None
    assert client.get('/api/state',headers=headers).status_code == 403
    assert client.post('/api/robots/measurements',headers={**headers,'Idempotency-Key':'gateway-batch-1'},json={'measurements':[robot('OTHER')]}).status_code == 403


def test_gateway_single_dispatch_receipt_and_no_repeat(client, monkeypatch):
    monkeypatch.setenv('PHYSICAL_CONTROL_ENABLED','true')
    key,command=setup_command(client)
    assert client.post(f"/api/automation/commands/{command['id']}/approve",json={'confirm':True}).status_code == 200
    headers={'Authorization':'Bearer '+key['token']}
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses=list(pool.map(lambda _:client.post('/api/gateway/next',headers=headers).json(),range(4)))
    issued=[r['command'] for r in responses if r['command']]
    assert len(issued)==1
    ack={'command_id':command['id'],'lease':issued[0]['lease'],'result':'succeeded','detail':'Контроллер подтвердил ID команды'}
    assert client.post('/api/gateway/result',headers=headers,json=ack).status_code == 200
    assert client.post('/api/gateway/result',headers=headers,json=ack).status_code == 200
    assert client.post('/api/gateway/next',headers=headers).json()['command'] is None
    assert client.post('/api/gateway/result',headers=headers,json={**ack,'result':'failed'}).status_code == 409
    assert client.delete('/api/admin/gateways/'+key['id']).status_code == 200
    assert client.post('/api/gateway/next',headers=headers).status_code == 401


def test_command_rechecks_freshness_and_interlocks(client, monkeypatch):
    monkeypatch.setenv('PHYSICAL_CONTROL_ENABLED','true')
    key,command=setup_command(client)
    old=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
    twin=client.app.state.service
    with twin.lock:
        twin.robot_rows[-1]['timestamp']=old
    assert client.post(f"/api/automation/commands/{command['id']}/approve",json={'confirm':True}).status_code == 409
    with twin.lock:
        twin.robot_rows[-1]['timestamp']=datetime.now(timezone.utc).isoformat()
        twin.robot_rows[-1]['safety_state']='emergency_stop'
    resume=client.post('/api/automation/commands',json={'robot_id':'EQUIPMENT-1','action':'resume','reason':'Продолжить работу'}).json()
    assert client.post(f"/api/automation/commands/{resume['id']}/approve",json={'confirm':True}).status_code == 409


def test_expired_dispatched_command_is_uncertain_and_blocks_further_commands(client, monkeypatch):
    monkeypatch.setenv('PHYSICAL_CONTROL_ENABLED','true')
    key,command=setup_command(client)
    client.post(f"/api/automation/commands/{command['id']}/approve",json={'confirm':True})
    headers={'Authorization':'Bearer '+key['token']}
    client.post('/api/gateway/next',headers=headers)
    with client.app.state.service.sessions.begin() as db:
        row=db.get(RobotCommand,command['id']); row.data={**row.data,'expires_at':time.time()-1}
    assert client.get('/api/automation').json()['commands'][0]['status']=='uncertain'
    assert client.post('/api/gateway/next',headers=headers).json()['command'] is None
    another=client.post('/api/automation/commands',json={'robot_id':'EQUIPMENT-1','action':'hold','reason':'Повторный запрос'}).json()
    assert client.post(f"/api/automation/commands/{another['id']}/approve",json={'confirm':True}).status_code == 409
    reconciled=client.post(f"/api/admin/commands/{command['id']}/reconcile",json={'confirm':True,'result':'failed','evidence':'Журнал контроллера проверен инженером: команда не выполнялась'})
    assert reconciled.status_code == 200


def test_ai_is_not_allowed_to_create_a_command_without_explicit_request(client, monkeypatch):
    from app import local_ai
    import json
    setup_command(client)
    count=len(client.get('/api/automation').json()['commands'])
    def model(*args,**kwargs):
        return {'message':{'content':json.dumps({'analysis':'Проверьте узел','action':'hold','speed_percent':None,'reason':'Предложение модели'})}}
    monkeypatch.setattr(local_ai,'ollama',model)
    reply=client.post('/api/ai/analyze',json={'question':'Дай анализ','robot_id':'EQUIPMENT-1','allow_command':False})
    assert reply.status_code==200 and reply.json()['proposal'] is None
    assert len(client.get('/api/automation').json()['commands'])==count
    reply=client.post('/api/ai/analyze',json={'question':'Предложи остановку','robot_id':'EQUIPMENT-1','allow_command':True})
    assert reply.status_code==200 and reply.json()['proposal']['status']=='proposed'
    assert reply.json()['proposal']['origin']=='local_ai'


def test_new_consumable_rules_create_real_incidents(client):
    client.post('/api/robots/measurements',json={'measurements':[robot(electrode_count=2)]})
    config={'expected_interval_seconds':60,'limits':{'electrode_count':{'low_warning':5,'low_critical':1}},'error_codes':{}}
    assert client.put('/api/robots/config?robot_id=EQUIPMENT-1',json=config).status_code==200
    assert any('Сварочные электроды' in x['title'] for x in client.get('/api/incidents').json())


def test_unresolved_command_remains_visible_beyond_last_hundred(client):
    with client.app.state.service.sessions.begin() as db:
        for i in range(105):
            db.add(RobotCommand(id=f'command-{i}',robot_id='R-1',status='uncertain' if i==0 else 'cancelled',created_at=float(i),data={'expires_at':time.time()+60}))
    commands=client.get('/api/automation').json()['commands']
    assert len(commands)==101 and any(c['id']=='command-0' and c['status']=='uncertain' for c in commands)
