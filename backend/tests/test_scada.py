import csv
import io
import json
from datetime import datetime, timezone, timedelta, date
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from auth_client import TestClient
from test_api import client, db_url
from app.main import create_app
from app.database import ScadaRecord, Snapshot
from app.scada import work_date, DEFAULT_CALENDAR, check_robot_interlock, ai_context


def save(client,path,body):
    response=client.post('/api/scada'+path,json=body)
    assert response.status_code==200,response.text
    return response.json()


def setup(client,**extra):
    save(client,'/assets',{'id':'CV1','name':'Конвейер','kind':'conveyor','robot_id':'ROBOT1'})
    save(client,'/parts',{'id':'MOTOR','name':'Двигатель','minimum':1})
    return save(client,'/components',{'id':'CV1-motor','asset_id':'CV1','name':'Двигатель привода','node':'drive','kind':'motor','part_id':'MOTOR',
        'life_model':'hours','rated_life':100.0,'initial_wear_pct':80.0,'specification':'Тестовый паспорт для проверки',
        'thresholds':[{'metric':'temperature_c','label':'Температура','unit':'°C','warning':80.0,'alarm':100.0,'hysteresis':5.0}],**extra})


def reading(key='r1',hours=100,temperature=70,ago=7200,**extra):
    return {'record_id':key,'component_id':'CV1-motor','timestamp':(datetime.now(timezone.utc)-timedelta(seconds=ago)).isoformat(),
        'runtime_hours':hours,'metrics':{'temperature_c':temperature},**extra}


def ingest(client,*rows): return save(client,'/readings',{'readings':list(rows)})
def state(client):
    r=client.get('/api/scada');assert r.status_code==200,r.text
    return r.json()


def test_empty_installation_has_no_fictional_scada_data(client):
    original=client.get('/api/state').json()
    data=state(client)
    assert all(data[k]==[] for k in ('assets','components','parts','offers','orders','alarms','tasks'))
    assert client.get('/api/state').json()['metrics']==original['metrics']


def test_real_counters_resource_stale_state_and_persistence(db_url):
    with TestClient(create_app(db_url,runner_enabled=False)) as client:
        setup(client)
        ingest(client,reading(),reading('r2',110,75,3600))
        c=state(client)['components'][0]
        assert c['wear_pct']==90 and c['remaining_hours']==10
        assert c['connection']=='stale' and c['severity']=='warning'
        assert state(client)['parts'][0]['deficit']==2
    with TestClient(create_app(db_url,runner_enabled=False)) as client:
        assert state(client)['components'][0]['wear_pct']==90
        assert client.get('/api/scada/history?component_id=CV1-motor').json()['total']==2


def test_alarm_hysteresis_missing_value_acknowledgment_recovery(client):
    setup(client,life_model='none',rated_life=None)
    ingest(client,reading(temperature=101))
    alarm=state(client)['alarms'][0]
    save(client,f"/alarms/{alarm['id']}/acknowledge",{})
    ingest(client,reading('r2',101,97,6000))
    assert state(client)['alarms'][0]['severity']=='alarm'
    ingest(client,reading('r3',102,0,5000,metrics={'vibration_mm_s':1.1}))
    assert state(client)['alarms'][0]['status']=='acknowledged'
    ingest(client,reading('r4',103,74,4000))
    assert state(client)['alarms'][0]['status']=='closed'
    assert state(client)['components'][0]['active_alarms']=={}


def test_batch_atomic_idempotent_conflict_out_of_order_and_counter_reset(client):
    setup(client);first=reading()
    assert ingest(client,first)['imported']==1
    assert ingest(client,first)=={'imported':0,'duplicates':1}
    assert client.post('/api/scada/readings',json={'readings':[{**first,'runtime_hours':999}]}).status_code==409
    assert client.post('/api/scada/readings',json={'readings':[reading('new',101,70,4000),reading('bad',2,70,3000)]}).status_code==409
    assert client.get('/api/scada/history?component_id=CV1-motor').json()['total']==1
    assert client.post('/api/scada/readings',json={'readings':[reading('old',110,70,9000)]}).status_code==409
    assert state(client)['components'][0]['wear_pct']==80


@pytest.mark.parametrize('model,metrics,rated,expected',[('thermal',{'temperature_c':90},40000,0.005),('reducer',{'load_ratio':1,'speed_ratio':1},6000,100/6000),('chain',{'load_ratio':1,'lubrication_pct':0},30000,900/30000),('cycles',{},1000,10),('distance',{},1000,1)])
def test_engineering_models_use_measured_deltas(client,model,metrics,rated,expected):
    setup(client,life_model=model,rated_life=float(rated),initial_wear_pct=0.0)
    ingest(client,reading(metrics=metrics,total_cycles=100,distance_km=100),reading('r2',101,70,3600,metrics=metrics,total_cycles=200,distance_km=110))
    assert state(client)['components'][0]['wear_pct']==pytest.approx(expected)


def test_missing_counter_interval_suppresses_forecast(client):
    setup(client)
    ingest(client,reading(),reading('r2',None,70,4000),reading('r3',110,70,3000),reading('r4',111,70,2000))
    c=state(client)['components'][0]
    assert c['resource_incomplete'] and c['remaining_hours'] is None and c['due_date'] is None


def test_calendar_alarm_and_workday_rules(client):
    setup(client,life_model='calendar',rated_life=2.0,initial_wear_pct=0.0,commissioned_at=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat())
    assert state(client)['components'][0]['severity']=='alarm'
    assert any(a['metric']=='resource' for a in state(client)['alarms'])
    assert work_date(date(2026,10,9),1,DEFAULT_CALENDAR)=='2026-10-12'
    cal={**DEFAULT_CALENDAR,'holidays':['2026-10-12']}
    assert work_date(date(2026,10,9),1,cal)=='2026-10-13'
    assert work_date(date(2026,10,10),0,cal)=='2026-10-13'


def test_stock_supplier_purchase_partial_receive_and_replacement(client):
    setup(client)
    ingest(client,reading(temperature=105))
    offer={'id':'offer-1','part_id':'MOTOR','supplier':'Проверочный поставщик','price_minor':12345,'currency':'KZT','lead_workdays':2,'valid_until':'2099-12-31','reference':'КП-2026-01'}
    save(client,'/offers',offer)
    order=save(client,'/orders',{'id':'po1','offer_id':'offer-1','quantity':2})
    assert order['status']=='draft' and state(client)['parts'][0]['on_order']==0
    assert client.post('/api/scada/orders/po1/receive',json={'record_id':'delivery1','quantity':1,'reference':'Накладная 01'}).status_code==409
    save(client,'/orders/po1/confirm',{'reference':'Подтверждение 01'})
    assert state(client)['parts'][0]['on_order']==2
    receive={'record_id':'delivery1','quantity':1,'reference':'Накладная 01'}
    save(client,'/orders/po1/receive',receive);save(client,'/orders/po1/receive',receive)
    assert state(client)['parts'][0]['stock']==1 and state(client)['parts'][0]['on_order']==1
    save(client,'/orders/po1/receive',{**receive,'record_id':'delivery2'})
    assert state(client)['parts'][0]['stock']==2 and not state(client)['orders']
    save(client,'/tasks',{'id':'task1','component_id':'CV1-motor','due_date':'2026-10-07','note':'Замена привода по регламенту'})
    body={'evidence':'Акт замены двигателя 2026-01','reset_counters':True}
    save(client,'/tasks/task1/complete',body);save(client,'/tasks/task1/complete',body)
    data=state(client)
    assert data['parts'][0]['stock']==1 and data['components'][0]['wear_pct']==0
    assert any(a['metric']=='temperature_c' and a['status']!='closed' for a in data['alarms'])
    assert client.get('/api/scada/journal?kind=movement').json()['total']==3
    ingest(client,reading('after-service',0,60,0))
    assert state(client)['components'][0]['wear_pct']==0


def test_maintenance_failure_rolls_back_and_concurrent_stock_cannot_go_negative(client):
    setup(client);save(client,'/tasks',{'id':'task1','component_id':'CV1-motor','due_date':'2026-10-07','note':'Плановая замена привода'})
    assert client.post('/api/scada/tasks/task1/complete',json={'evidence':'Акт выполненной работы'}).status_code==409
    assert state(client)['tasks'][0]['status']=='planned' and state(client)['components'][0]['wear_pct']==80
    save(client,'/stock',{'record_id':'initial','part_id':'MOTOR','quantity':1,'reason':'Акт инвентаризации'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda key:client.post('/api/scada/stock',json={'record_id':key,'part_id':'MOTOR','quantity':-1,'reason':'Выдача в производство'}).status_code,['out1','out2']))
    assert sorted(results)==[200,409] and state(client)['parts'][0]['stock']==0


def test_offers_are_compared_within_currency_and_expired_not_orderable(client):
    setup(client)
    for key,currency,price,expiry in [('tenge','KZT',10000,'2099-12-31'),('usd','USD',100,'2099-12-31'),('old','KZT',1,'2020-01-01')]:
        save(client,'/offers',{'id':key,'part_id':'MOTOR','supplier':key,'price_minor':price,'currency':currency,'lead_workdays':2,'valid_until':expiry,'reference':'Документ КП'})
    recommendations=state(client)['parts'][0]['recommendations']
    assert {r['offer_id'] for r in recommendations}=={'tenge','usd'}
    assert client.post('/api/scada/orders',json={'id':'bad','offer_id':'old','quantity':1}).status_code==409


def test_robot_mapping_and_scada_alarms_block_resume_but_never_dispatch(client):
    setup(client,life_model='none',rated_life=None,robot_metric_map={'joint_temperature_c':'temperature_c'})
    stamp=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    robot={'robot_id':'ROBOT1','timestamp':stamp,'cycle_status':'In_Progress','joint_temperature_c':110.0}
    assert client.post('/api/robots/measurements',json={'measurements':[robot]}).status_code==200
    data=state(client);assert data['components'][0]['last']['metrics']['temperature_c']==110
    with client.app.state.service.sessions() as db:
        with pytest.raises(HTTPException): check_robot_interlock(db,'ROBOT1')
    assert client.get('/api/automation').json()['commands']==[]
    context=ai_context(client.app.state.service)
    assert context['components'][0]['severity']=='alarm' and context['shortages'][0]['id']=='MOTOR'


def test_csv_roundtrip_validation_and_no_spreadsheet_formula_identifiers(client):
    setup(client);row=reading()
    fields=client.get('/api/scada/template').text.lstrip('\ufeff').strip().split(',')
    buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=fields);writer.writeheader()
    writer.writerow({**{k:row.get(k) for k in fields if k!='metrics_json'},'metrics_json':json.dumps(row['metrics'])})
    r=client.post('/api/scada/import',files={'file':('readings.csv',buf.getvalue())});assert r.status_code==200,r.text
    exported=client.get('/api/scada/export?component_id=CV1-motor').content
    assert client.post('/api/scada/import',files={'file':('readings.csv',exported)}).json()['duplicates']==1
    assert client.post('/api/scada/assets',json={'id':'=1+1','name':'x','kind':'robot'}).status_code==422
    assert client.post('/api/scada/readings',json={'readings':[reading('future',timestamp='2099-01-01T00:00:00Z')]}).status_code==422
    assert client.post('/api/scada/readings',json={'readings':[reading('naive',timestamp='2026-10-06T00:00:00')]}).status_code==422


def test_integration_key_can_ingest_but_not_modify_inventory(client):
    setup(client)
    token=client.post('/api/admin/tokens',json={'name':'SCADA sensor','days':1}).json()['token']
    headers={'Authorization':'Bearer '+token}
    assert client.post('/api/scada/readings',json={'readings':[reading()]},headers=headers).status_code==200
    assert client.post('/api/scada/stock',json={'record_id':'bad','part_id':'MOTOR','quantity':10,'reason':'Not permitted'},headers=headers).status_code==403
    assert client.get('/api/scada',headers=headers).status_code==403
    assert client.post('/api/scada/assets',json={'id':'new','name':'x','kind':'robot'},headers={'X-CSRF-Token':''}).status_code==403


def test_topology_templates_are_idempotent_and_do_not_fabricate_sensors(client):
    save(client,'/assets',{'id':'ARM','name':'Робот','kind':'robot'})
    save(client,'/assets',{'id':'CV','name':'Конвейер','kind':'conveyor'})
    assert save(client,'/assets/ARM/template',{})['created']==23
    assert save(client,'/assets/CV/template',{})['created']==13
    assert save(client,'/assets/ARM/template',{})['created']==0
    assert all(c['last'] is None and c['wear_pct'] is None and c['thresholds']==[] for c in state(client)['components'])


def test_config_edit_has_revision_check_and_preserves_history(client):
    setup(client)
    with client.app.state.service.sessions() as db:
        config=db.get(ScadaRecord,('component','CV1-motor')).data['request'].copy()
    config['name']='Переименованный двигатель'
    body={'config':config,'expected_revision':1}
    result=client.put('/api/scada/components/CV1-motor',json=body)
    assert result.status_code==200,result.text
    assert result.json()['revision']==2
    assert client.put('/api/scada/components/CV1-motor',json=body).status_code==409
    ingest(client,reading(temperature=110))
    assert client.put('/api/scada/components/CV1-motor',json={'config':{**config,'rated_life':200.0},'expected_revision':2}).status_code==409
    assert client.put('/api/scada/components/CV1-motor',json={'config':{**config,'thresholds':[]},'expected_revision':2}).status_code==409
    assert state(client)['components'][0]['rated_life']==100


def test_service_rejects_delayed_old_readings_and_stock_document_is_fixed(client):
    setup(client)
    save(client,'/stock',{'record_id':'initial','part_id':'MOTOR','quantity':1,'reason':'Акт инвентаризации'})
    save(client,'/tasks',{'id':'task1','component_id':'CV1-motor','due_date':'2026-10-07','note':'Плановая замена двигателя'})
    save(client,'/tasks/task1/complete',{'evidence':'Акт замены двигателя','reset_counters':True})
    assert client.post('/api/scada/readings',json={'readings':[reading()]}).status_code==409
    assert state(client)['components'][0]['wear_pct']==0


def test_unconfigured_limits_never_claim_normal_state(client):
    setup(client,life_model='none',rated_life=None,thresholds=[])
    ingest(client,reading(temperature=300,ago=1))
    c=state(client)['components'][0]
    assert c['connection']=='fresh' and c['severity']=='unknown'
