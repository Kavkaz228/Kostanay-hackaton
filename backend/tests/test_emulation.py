import copy
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from auth_client import TestClient
from test_api import client, db_url
from app.main import create_app
from app.database import Snapshot, ScadaRecord, Observation, Audit
from app.emulation import KEY, tick
from app.emulation_model import Stand, SEED, initial


def state(c):
    r=c.get('/api/emulation');assert r.status_code==200,r.text
    return r.json()


def cmd(c,action,**kwargs):
    body=dict(request_id=uuid4().hex,expected_revision=state(c)['revision'],action=action,**kwargs)
    r=c.post('/api/emulation/commands',json=body)
    assert r.status_code==200,r.text
    return r.json()


def test_topology_and_no_plant_data_creation(client):
    before=client.get('/api/state').json()
    d=state(client)
    assert d['source']=='emulation' and d['paused']
    assert len(d['components'])==107 and len(d['faults'])==11 and len(d['stations'])==4
    for r in ('R1','R2','R3','R4'):
        assert len([c for c in d['components'] if c['robot']==r and c['node'].startswith('J')])==18
    cmd(client,'advance',value=600)
    assert state(client)['cars']==10
    assert client.get('/api/scada').json()['components']==[]
    assert client.get('/api/state').json()['metrics']==before['metrics']
    with client.app.state.service.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ScadaRecord))==0
        assert db.scalar(select(func.count()).select_from(Observation))==0


@pytest.mark.parametrize('identity', [f['id'] for f in SEED['faults']])
def test_every_archive_fault_changes_target_and_is_reversible(identity):
    s=initial();m=Stand(s)
    f=next(f for f in SEED['faults'] if f['id']==identity)
    baseline={k:(m.targets(m.by[k],True),m.rate(m.by[k],True)) for k in f['t']}
    m.fault(identity,True)
    assert all(m.by[k]['fx']==f['fx'] for k in f['t'])
    changed={k:(m.targets(m.by[k],True),m.rate(m.by[k],True)) for k in f['t']}
    assert baseline!=changed
    m.fault(identity,False)
    assert all(m.by[k]['fx']=={} for k in f['t'])
    assert baseline=={k:(m.targets(m.by[k],True),m.rate(m.by[k],True)) for k in f['t']}


def test_conveyor_coupling_and_hysteresis():
    m=Stand(initial())
    old=m.targets(m.by['CV-cvmot'],True)['cur'];rate=m.chain_rate()
    m.fault('f9',True)
    assert m.targets(m.by['CV-cvmot'],True)['cur']>old
    m.fault('f8',True)
    assert m.chain_rate()>rate*5
    d=dict(warn=95,alarm=110)
    assert m.level(d,109,'alarm')=='alarm'
    assert m.level(d,107,'alarm')=='warn'
    assert m.level(d,94,'warn')=='warn'
    assert m.level(d,92,'warn')=='ok'
    assert m.level(dict(warn=5.2,alarm=4.8,lo=True),4.7,'ok')=='alarm'


def test_jidoka_repair_ack_restart_and_pause(client):
    cmd(client,'fault',target='f10',active=True)
    d=state(client);assert d['line']=='stop' and d['stops']==1
    alarm=next(a for a in d['alarms'] if a['component_id']=='CV-pos')
    for action,extra in [('restart',{}),('ack',{'target':alarm['id']})]:
        r=client.post('/api/emulation/commands',json=dict(request_id=uuid4().hex,expected_revision=d['revision'],action=action,**extra))
        assert r.status_code==409
    cmd(client,'advance',value=600)
    assert state(client)['cars']==0 and state(client)['down_hours']==pytest.approx(1/6)
    cmd(client,'fault',target='f10',active=False)
    assert next(a for a in state(client)['alarms'] if a['id']==alarm['id'])['recovered']
    cmd(client,'ack',target=alarm['id']);cmd(client,'restart')
    cmd(client,'advance',value=600)
    assert state(client)['cars']==10
    cmd(client,'play');tick(client.app.state.service,1)
    cmd(client,'pause');t=state(client)['t'];tick(client.app.state.service,1)
    assert state(client)['t']==t


def test_replacement_stock_and_thermal_lag(client):
    cmd(client,'fault',target='f3',active=True)
    r=client.post('/api/emulation/commands',json=dict(request_id=uuid4().hex,expected_revision=state(client)['revision'],action='replace',target='R1-cups'))
    assert r.status_code==409
    cmd(client,'stock',target='VAC-CUP',value=1);cmd(client,'replace',target='R1-cups')
    d=state(client)
    assert not next(f for f in d['faults'] if f['id']=='f3')['active']
    assert next(p for p in d['parts'] if p['id']=='VAC-CUP')['stock']==0
    assert next(c for c in d['components'] if c['id']=='R1-cups')['wear_pct']==0
    assert next(a for a in d['alarms'] if a['component_id']=='R1-cups')['recovered']
    m=Stand(initial());c=m.by['R1-J2-mot'];old=c['raw']['temp']
    m.fault('f1',True)
    assert c['raw']['temp']==old
    m.advance(600)
    assert old<c['raw']['temp']<m.targets(c,True)['temp']
    m.advance(3000)
    assert m.s['stops']>=1


def test_noise_continuity_history_bounds_and_retained_sample():
    s=initial();a=Stand(s);b=Stand(copy.deepcopy(s))
    for _ in range(140): a.advance(1);b.advance(1)
    assert a.s==b.s
    assert len(a.by['R1-J2-mot']['hist']['temp'])==120
    c=a.by['CV-pos'];last=c['sampled_at']['pe'];a.s['line']='stop';a.advance(60)
    assert c['sampled_at']['pe']==last
    assert len(c['hist']['pe'])==120
    assert all(math.isfinite(v) for c in a.cs for v in c['val'].values())


def test_persistence_and_reset_only_emulation(db_url):
    with TestClient(create_app(db_url,runner_enabled=False)) as c:
        before=c.get('/api/state').json()['metrics']
        cmd(c,'advance',value=600);cmd(c,'fault',target='f10',active=True)
        saved=state(c)
    with TestClient(create_app(db_url,runner_enabled=False)) as c:
        assert state(c)==saved
        cmd(c,'reset');d=state(c)
        assert d['t']==0 and d['paused'] and not d['alarms'] and not any(f['active'] for f in d['faults'])
        assert d['revision']==saved['revision']+1
        assert c.get('/api/state').json()['metrics']==before


def test_idempotency_stale_concurrency_validation_and_audit(client):
    body=dict(request_id=uuid4().hex,expected_revision=0,action='stock',target='VAC-CUP',value=1)
    r=client.post('/api/emulation/commands',json=body);assert r.status_code==200
    assert client.post('/api/emulation/commands',json=body).json()==r.json()
    assert next(p for p in state(client)['parts'] if p['id']=='VAC-CUP')['stock']==1
    assert client.post('/api/emulation/commands',json={**body,'value':2}).status_code==409
    assert client.post('/api/emulation/commands',json={**body,'request_id':uuid4().hex}).status_code==409
    for extra in ({'action':'speed','value':9000,'target':None},{'action':'fault','target':'f1','value':None},{'action':'advance','value':0,'target':None},{'action':'stock','value':True}):
        assert client.post('/api/emulation/commands',json={**body,**extra}).status_code==422
    rev=state(client)['revision']
    def write(_): return client.post('/api/emulation/commands',json={**body,'expected_revision':rev,'request_id':uuid4().hex}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: assert sorted(pool.map(write,range(2)))==[200,409]
    with client.app.state.service.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='emulation.stock'))==2


def test_failed_transaction_retains_state(client, monkeypatch):
    cmd(client,'pause');twin=client.app.state.service;before=state(client)
    def broken(self,seconds):
        self.s['t']=999
        raise RuntimeError('forced model failure')
    monkeypatch.setattr(Stand,'advance',broken)
    assert client.post('/api/emulation/commands',json=dict(request_id=uuid4().hex,expected_revision=before['revision'],action='advance',value=10)).status_code==500
    assert state(client)==before


def test_failed_database_commit_does_not_publish_changes(client, monkeypatch):
    from sqlalchemy.orm import Session
    cmd(client,'pause');before=state(client)
    original=Session.flush
    def fail(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise RuntimeError('forced commit failure')
    monkeypatch.setattr(Session,'flush',fail)
    assert client.post('/api/emulation/commands',json=dict(request_id=uuid4().hex,expected_revision=before['revision'],action='advance',value=60)).status_code==500
    monkeypatch.undo()
    assert state(client)==before
    with client.app.state.service.sessions() as db: assert db.get(Snapshot,KEY).data['t']==before['t']


def test_runner_advances_only_saved_running_stand(client):
    twin=client.app.state.service
    tick(twin,1)
    with twin.sessions() as db: assert db.get(Snapshot,KEY) is None
    cmd(client,'play');tick(twin,1)
    assert state(client)['t']==pytest.approx(1/60)
    cmd(client,'pause');before=state(client)['t'];tick(twin,1)
    assert state(client)['t']==before


def test_auth_viewer_and_ingestion_key_cannot_control(client):
    # Runtime authentication, not a model-level permission substitute.
    token=client.post('/api/admin/tokens',json={'name':'emulation-scope-check','days':1}).json()['token']
    headers={'Authorization':'Bearer '+token}
    assert client.get('/api/emulation',headers=headers).status_code==403
    assert client.post('/api/emulation/commands',headers=headers,json=dict(request_id=uuid4().hex,expected_revision=0,action='play')).status_code==403
    r=client.post('/api/admin/users',json={'username':'emulation-viewer','password':'Emulation viewer password 2026!','role':'viewer'})
    assert r.status_code==200,r.text
    user=r.json()
    from app.database import User
    with client.app.state.service.sessions.begin() as db:
        u=db.get(User,user['id']);u.must_change=False
    client.post('/api/auth/logout')
    r=client.post('/api/auth/login',json={'username':'emulation-viewer','password':'Emulation viewer password 2026!'})
    assert r.status_code==200,r.text
    client.headers['X-CSRF-Token']=client.get('/api/auth/me').json()['csrf']
    assert client.get('/api/emulation').status_code==200
    assert client.post('/api/emulation/commands',json=dict(request_id=uuid4().hex,expected_revision=0,action='play')).status_code==403
