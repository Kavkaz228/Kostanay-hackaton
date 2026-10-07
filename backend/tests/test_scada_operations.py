import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, date
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from auth_client import TestClient
from test_api import client, db_url
from test_manufacturing import robot
from test_scada import save, state
from app.main import create_app
from app.database import ScadaRecord, User, Audit


def scoped(client, operation):
    r=client.get('/api/scada',params={'operation':operation})
    assert r.status_code==200,r.text
    return r.json()


def assign(client, asset_id, operation, revision=1, request_id=None):
    return client.put(f'/api/scada/assets/{asset_id}/operation',json={
        'operation':operation,'expected_revision':revision,'request_id':request_id or uuid4().hex})


def seed_sections(client):
    for key, operation in [('W','welding'),('P','painting'),('A','assembly'),('U',None)]:
        save(client,'/assets',{'id':key,'name':key,'kind':'equipment','operation':operation})
    for part in ('COMMON','W-ONLY','P-ONLY','UNUSED'):
        save(client,'/parts',{'id':part,'name':part,'minimum':1})
        save(client,'/stock',{'record_id':'stock-'+part,'part_id':part,'quantity':1,'reason':'Подтверждённый начальный остаток'})
        save(client,'/offers',{'id':'offer-'+part,'part_id':part,'supplier':'Поставщик из договора',
            'price_minor':10000,'currency':'KZT','lead_workdays':5,
            'valid_until':(date.today()+timedelta(days=30)).isoformat(),'reference':'Коммерческое предложение 1'})
        save(client,'/orders',{'id':'order-'+part,'offer_id':'offer-'+part,'quantity':2})
        save(client,'/orders/order-'+part+'/confirm',{'reference':'Подтверждение заказа 1'})
    for key, asset, part, quantity in [('CW','W','COMMON',2),('CP','P','COMMON',3),
                                     ('CW2','W','W-ONLY',1),('CP2','P','P-ONLY',1),('CA','A',None,1),('CU','U',None,1)]:
        save(client,'/components',{'id':key,'asset_id':asset,'name':'Узел '+key,'part_id':part,
            'part_quantity':quantity,'life_model':'hours','rated_life':100.,'initial_wear_pct':80.,
            'specification':'Регламент контрольного оборудования',
            'thresholds':[{'metric':'temperature_c','label':'Температура','warning':80.,'alarm':100.,'hysteresis':5.}]})
        save(client,'/tasks',{'id':'task-'+key,'component_id':key,'due_date':date.today().isoformat(),
                             'note':'Плановое обслуживание по регламенту'})
        save(client,'/readings',{'readings':[{'record_id':'reading-'+key,'component_id':key,
            'timestamp':datetime.now(timezone.utc).isoformat(),'runtime_hours':10.,'metrics':{'temperature_c':110.}}]})


def test_operation_scope_isolates_equipment_tasks_and_alarms_but_keeps_shared_stock(client):
    seed_sections(client)
    whole=state(client)
    by_part={p['id']:p for p in whole['parts']}
    welding,painting=scoped(client,'welding'),scoped(client,'painting')
    assert {a['id'] for a in welding['assets']}=={'W'}
    assert {c['id'] for c in welding['components']}=={'CW','CW2'}
    assert {t['component_id'] for t in welding['tasks']}=={'CW','CW2'}
    assert {a['component_id'] for a in welding['alarms']}=={'CW','CW2'}
    assert {p['id'] for p in welding['parts']}=={'COMMON','W-ONLY'}
    assert {o['part_id'] for o in welding['offers']}=={'COMMON','W-ONLY'}
    assert {o['part_id'] for o in welding['orders']}=={'COMMON','W-ONLY'}
    assert {p['id'] for p in painting['parts']}=={'COMMON','P-ONLY'}
    for view,demand in [(welding,2),(painting,3)]:
        common=next(p for p in view['parts'] if p['id']=='COMMON')
        assert common['scope_needed']==demand
        for key in ('stock','minimum','on_order','needed','deficit','recommendations'):
            assert common[key]==by_part['COMMON'][key]
        assert common['needed']==5 and common['deficit']==3
        assert common['inventory_scope']=='shared' and view['scope']['inventory']=='shared'
        assert {p['id'] for p in view['part_catalog']}==set(by_part)
        assert all(set(p)=={'id','name'} for p in view['part_catalog'])
    assert {a['id'] for a in scoped(client,'assembly')['assets']}=={'A'}
    assert {a['id'] for a in scoped(client,'unknown')['assets']}=={'U'}
    # A scoped read must never mutate the cached global representation.
    global_after=state(client)
    assert len(global_after['assets'])==4 and len(global_after['components'])==6
    assert all('scope_needed' not in p and 'inventory_scope' not in p for p in global_after['parts'])
    assert 'scope' not in global_after and 'part_catalog' not in global_after


def test_journal_scope_applies_before_pagination(client):
    seed_sections(client)
    for kind,expected in [('task',{'CW','CW2'}),('alarm',{'CW','CW2'}),
                           ('movement',{'COMMON','W-ONLY'}),('order',{'COMMON','W-ONLY'})]:
        r=client.get('/api/scada/journal',params={'kind':kind,'operation':'welding'})
        assert r.status_code==200,r.text
        key='component_id' if kind in ('task','alarm') else 'part_id'
        assert {x[key] for x in r.json()['rows']}==expected
        assert r.json()['total']==len(r.json()['rows'])
        assert client.get('/api/scada/journal',params={'kind':kind,'operation':'welding','offset':100}).json()['rows']==[]
    assert client.get('/api/scada/journal',params={'kind':'task'}).json()['total']==6


def test_scope_keeps_part_reserved_by_existing_task_after_component_configuration_changes(client):
    save(client,'/assets',{'id':'W','name':'Сварка','kind':'equipment','operation':'welding'})
    for part in ('OLD','NEW'):
        save(client,'/parts',{'id':part,'name':part})
    save(client,'/stock',{'record_id':'initial-old','part_id':'OLD','quantity':1,'reason':'Резерв для подтверждённой работы'})
    save(client,'/components',{'id':'C','asset_id':'W','name':'Узел','part_id':'OLD'})
    save(client,'/tasks',{'id':'T','component_id':'C','due_date':date.today().isoformat(),'note':'Ранее запланированная работа'})
    with client.app.state.service.sessions() as db:
        config={**db.get(ScadaRecord,('component','C')).data['request'],'part_id':'NEW'}
    r=client.put('/api/scada/components/C',json={'config':config,'expected_revision':1})
    assert r.status_code==200,r.text
    d=scoped(client,'welding')
    assert d['components'][0]['part_id']=='NEW' and d['tasks'][0]['part_id']=='OLD'
    assert {p['id'] for p in d['parts']}=={'OLD','NEW'}
    assert client.get('/api/scada/journal',params={'kind':'movement','operation':'welding'}).json()['rows'][0]['part_id']=='OLD'


def test_legacy_asset_mapping_priority_and_create_retry_survive_upgrade(db_url):
    original={'id':'LEGACY','name':'Историческое оборудование','kind':'robot','section':'Assembly_Line','robot_id':'ROBOT-X'}
    with TestClient(create_app(db_url,runner_enabled=False)) as c:
        save(c,'/assets',original)
        with c.app.state.service.sessions.begin() as db:
            row=db.get(ScadaRecord,('asset','LEGACY'))
            old=copy.deepcopy(row.data)
            old.pop('operation');old.pop('revision')
            old['request'].pop('operation')
            row.data=old
    with TestClient(create_app(db_url,runner_enabled=False)) as c:
        asset=state(c)['assets'][0]
        assert asset['operation']=='assembly' and asset['operation_source']=='section'
        assert asset['operation_configured'] is None and asset['revision']==1
        # No reset/rewrite is required to resolve or replay old records.
        assert save(c,'/assets',original)['operation']=='assembly'
        r=c.post('/api/robots/measurements',json={'measurements':[robot('ROBOT-X',operation='painting')]})
        assert r.status_code==200,r.text
        assert scoped(c,'painting')['assets'][0]['operation_source']=='robot'
        assert scoped(c,'assembly')['assets']==[]
        assigned=assign(c,'LEGACY','welding')
        assert assigned.status_code==200 and assigned.json()['revision']==2
        assert assigned.json()['operation_source']=='explicit'
        assert {a['id'] for a in scoped(c,'welding')['assets']}=={'LEGACY'}
        # Replaying the initial create must not undo the explicit reassignment.
        replay=save(c,'/assets',original)
        assert replay['operation']=='welding' and replay['revision']==2
        assert replay['section']==original['section'] and replay['robot_id']==original['robot_id']
        assert assign(c,'LEGACY','unknown',2).json()['operation_source']=='explicit'
        assert scoped(c,'unknown')['assets'][0]['id']=='LEGACY'
        reset=assign(c,'LEGACY',None,3)
        assert reset.status_code==200 and reset.json()['operation']=='painting'


@pytest.mark.parametrize('section,operation',[('Welding_Shop','welding'),('сварочный цех','welding'),
    ('Painting_Shop','painting'),('Участок покраски','painting'),('Assembly_Line','assembly'),
    ('Цех сборки','assembly'),('Цех 7','unknown')])
def test_known_section_resolver_does_not_guess_from_asset_name(client,section,operation):
    asset=save(client,'/assets',{'id':'SECTION','name':'Сварочный робот, имя не определяет участок',
                               'kind':'equipment','section':section})
    assert asset['operation']==operation
    assert asset['operation_source']==('unknown' if operation=='unknown' else 'section')


def test_robot_mapping_tracks_latest_telemetry_and_invalidates_scoped_cache(client):
    save(client,'/assets',{'id':'AUTO','name':'Робот','kind':'robot','robot_id':'DYNAMIC','section':'Assembly_Line'})
    for operation,section,expected in [('', 'Welding_Shop', 'welding'),('painting','Welding_Shop','painting'),('', 'Цех 7', 'assembly')]:
        response=client.post('/api/robots/measurements',json={'measurements':[
            robot('DYNAMIC',operation=operation,line_section=section)]})
        assert response.status_code==200,response.text
        assert scoped(client,expected)['assets'][0]['id']=='AUTO'
        assert all(scoped(client,other)['assets']==[] for other in ('welding','painting','assembly','unknown') if other!=expected)


def test_reassignment_moves_existing_history_and_is_idempotent_audited_and_revision_guarded(client):
    seed_sections(client)
    with client.app.state.service.sessions() as db:
        components_before={r.id:copy.deepcopy(r.data) for r in db.scalars(select(ScadaRecord).where(ScadaRecord.kind=='component'))}
    request_id=uuid4().hex
    first=assign(client,'W','painting',request_id=request_id)
    assert first.status_code==200,first.text
    assert assign(client,'W','painting',request_id=request_id).json()==first.json()
    assert assign(client,'W','assembly',request_id=request_id).status_code==409
    assert assign(client,'W','assembly').status_code==409
    assert scoped(client,'welding')['assets']==[] and scoped(client,'welding')['tasks']==[]
    assert {a['id'] for a in scoped(client,'painting')['assets']}=={'W','P'}
    assert {a['component_id'] for a in scoped(client,'painting')['alarms']}=={'CW','CW2','CP','CP2'}
    with client.app.state.service.sessions() as db:
        assert {r.id:r.data for r in db.scalars(select(ScadaRecord).where(ScadaRecord.kind=='component'))}==components_before
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='scada.asset.operation'))==1
        assert db.scalar(select(func.count()).select_from(ScadaRecord).where(ScadaRecord.kind=='asset_operation'))==1
    # Two operators editing the same revision cannot silently overwrite each other.
    def concurrent(operation): return assign(client,'W',operation,2).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(concurrent,('welding','assembly')))==[200,409]


def test_reassignment_rejects_viewer_tokens_missing_csrf_and_unknown_operation(client):
    save(client,'/assets',{'id':'W','name':'Сварка','kind':'equipment','operation':'welding'})
    body={'operation':'painting','expected_revision':1,'request_id':uuid4().hex}
    for invalid in ('all','laser',1,True,[],{}):
        assert client.put('/api/scada/assets/W/operation',json={**body,'operation':invalid}).status_code==422
    for query in ('all','laser','1','true'):
        assert client.get('/api/scada',params={'operation':query}).status_code==422
        assert client.get('/api/scada/journal',params={'operation':query}).status_code==422
    assert client.put('/api/scada/assets/W/operation',json=body,headers={'X-CSRF-Token':''}).status_code==403
    token=client.post('/api/admin/tokens',json={'name':'SCADA operation scope','days':1}).json()['token']
    assert client.put('/api/scada/assets/W/operation',json=body,headers={'Authorization':'Bearer '+token}).status_code==403
    assert client.put('/api/scada/assets/MISSING/operation',json=body).status_code==404
    for role in ('viewer','operator'):
        username='scada-scope-'+role
        user=client.post('/api/admin/users',json={'username':username,'password':'SCADA section user password 2026!','role':role}).json()
        with client.app.state.service.sessions.begin() as db: db.get(User,user['id']).must_change=False
    client.post('/api/auth/logout')
    for role,expected in [('viewer',403),('operator',200)]:
        assert client.post('/api/auth/login',json={'username':'scada-scope-'+role,'password':'SCADA section user password 2026!'}).status_code==200
        client.headers['X-CSRF-Token']=client.get('/api/auth/me').json()['csrf']
        assert client.put('/api/scada/assets/W/operation',json=body).status_code==expected
        assert client.get('/api/scada',params={'operation':'painting'}).status_code==200
        client.post('/api/auth/logout')
