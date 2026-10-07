import copy
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from auth_client import TestClient
from test_api import client, db_url
from test_emulation import cmd, state
from app.main import create_app
from app.emulation import transaction, KEY
from app.emulation_model import Stand, initial
from app import emulation_support as support
from app.database import Observation, ScadaRecord, Audit, User, Snapshot


def test_work_calendar_forecast_and_metrics_are_derived():
    m = Stand(initial())
    m.s['start'] = '2026-10-09T07:00:00+05:00'  # Friday; weekends skipped.
    assert m.clock(16) == '2026-10-12T07:00:00+05:00'
    assert m.clock(80) == '2026-10-16T07:00:00+05:00'
    c = m.by['R1-cups']
    c['wear'] = 1 - 10 * m.rate(c, True)
    f = support.forecast(m, c)
    assert f['remaining_hours'] == pytest.approx(10)
    assert f['working_days'] == pytest.approx(10/16)
    assert datetime.fromisoformat(f['from']).hour == 15
    assert datetime.fromisoformat(f['to']).hour == 19
    assert f['uncertainty_pct'] == 20 and not f['open_ended']
    c['wear'] = 1
    assert support.forecast(m, c)['from'] == m.clock()
    long_lived = m.by['R1-fan']
    long_lived['wear'] = 0
    wide = support.forecast(m, long_lived)
    assert wide['open_ended'] and wide['from'] is None and wide['to'] is None and wide['after']
    m.s.update(runH=1., downH=1., carsF=60., nokF=6.)
    metrics = support.summary(m)
    assert metrics['performance_pct'] == 100
    assert metrics['availability_pct'] == 50
    assert metrics['quality_pct'] == 90
    assert metrics['oee_pct'] == 45


def test_supplier_selection_deficits_and_exactly_once_delivery():
    m = Stand(initial())
    cup = lambda: next(p for p in support.parts_view(m) if p['id']=='VAC-CUP')
    assert cup()['recommended_offer']['supplier_id'] == 'F'
    m.by['R1-cups']['wear'] = .999
    assert cup()['recommended_offer']['supplier_id'] == 'C'
    assert 'не успеть' in cup()['recommended_offer']['reason']
    needed = cup()['deficit']
    order = support.place_order(m, 'VAC-CUP')
    assert order['quantity'] == needed
    assert order['total_kzt'] == needed * 96000
    assert cup()['in_transit'] == needed and cup()['deficit'] == 0 and cup()['stock'] == 0
    m.s['t'] = order['eta_t'] - .01
    support.receive_due(m)
    assert cup()['stock'] == 0 and order['status'] == 'in_transit'
    m.s['t'] = order['eta_t']
    support.receive_due(m)
    assert order['status'] == 'received' and order['received_at'] == order['eta']
    assert cup()['stock'] == needed and cup()['in_transit'] == 0
    support.receive_due(m)
    assert cup()['stock'] == needed  # repeated tick cannot receive twice
    assert any('поставка' in n['text'] and n['channel']=='stock' for n in m.s['notifications'])


def test_order_api_atomic_idempotent_audited_and_isolated(client):
    before = client.get('/api/state').json()['metrics']
    body = dict(action='order', target='VAC-CUP', expected_revision=0, request_id=uuid4().hex)
    response = client.post('/api/emulation/commands', json=body)
    assert response.status_code == 200, response.text
    assert client.post('/api/emulation/commands', json=body).json() == response.json()
    d = state(client)
    assert len(d['orders']) == 1
    assert d['orders'][0]['source'] == 'emulation'
    cmd(client, 'order_all')
    covered = state(client)
    assert all(p['deficit']==0 for p in covered['parts'])
    cmd(client, 'order_all')
    assert state(client)['orders'] == covered['orders']
    with client.app.state.service.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Observation)) == 0
        assert db.scalar(select(func.count()).select_from(ScadaRecord)) == 0
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='emulation.order')) == 1
    assert client.get('/api/state').json()['metrics'] == before
    for action, target, status in [('order','TQ-CAL',404),('order','NOT-A-PART',404),('channel','tg',422),('notification_read','no-notice',404)]:
        r = client.post('/api/emulation/commands',json=dict(action=action,target=target,request_id=uuid4().hex,
            expected_revision=state(client)['revision'], **({'active':True} if action=='channel' else {})))
        assert r.status_code == status, r.text


def test_orders_notifications_and_schema_migration_survive_restart(db_url):
    with TestClient(create_app(db_url,runner_enabled=False)) as c:
        cmd(c,'advance',value=600)
        old_time = state(c)['t']
        # Represent a real pre-upgrade snapshot. Existing time/wear/history and
        # command receipts must survive additive migration.
        with c.app.state.service.sessions.begin() as db:
            row = db.get(Snapshot, KEY)
            saved = copy.deepcopy(row.data)
            for key in ('orders','order_sequence','notifications','notification_sequence','channels','stock_low','trips'):
                saved.pop(key, None)
            saved['version'] = 1
            row.data = saved
    with TestClient(create_app(db_url,runner_enabled=False)) as c:
        assert state(c)['t'] == old_time
        cmd(c,'order',target='GBX-OIL')
        cmd(c,'channel',target='summary',active=False)
        cmd(c,'summary')
        saved = state(c)
        assert saved['t'] == old_time
    with TestClient(create_app(db_url,runner_enabled=False)) as c:
        assert state(c) == saved
        order = saved['orders'][0]
        with transaction(c.app.state.service) as (_, data):
            data['t'] = order['eta_t']
            support.receive_due(Stand(data))
        delivered = state(c)
        assert delivered['orders'][0]['status'] == 'received'
        assert next(p for p in delivered['parts'] if p['id']=='GBX-OIL')['stock'] == 3
    with TestClient(create_app(db_url,runner_enabled=False)) as c:
        assert state(c) == delivered


def test_internal_notices_preferences_read_and_repeat_alarm(client):
    cmd(client,'channel',target='alarms',active=False)
    cmd(client,'fault',target='f10',active=True)
    d = state(client)
    alarm = next(a for a in d['alarms'] if a['component_id']=='CV-pos')
    assert alarm['trips']==1 and alarm['value']>=5 and alarm['unit']=='мм'
    assert alarm['cause'] and alarm['action']
    notice = next(n for n in d['notifications'] if n['channel']=='alarms')
    assert notice['status']=='muted' and not notice['external_delivery']
    cmd(client,'notification_read',target=str(notice['id']))
    assert next(n for n in state(client)['notifications'] if n['id']==notice['id'])['read']
    cmd(client,'fault',target='f10',active=False)
    restored = state(client)
    assert restored['conveyor']['operational_status']=='stop'  # still latched
    assert next(a for a in restored['alarms'] if a['id']==alarm['id'])['value']==alarm['value']
    cmd(client,'ack',target=alarm['id']);cmd(client,'restart')
    cmd(client,'channel',target='alarms',active=True)
    cmd(client,'fault',target='f10',active=True)
    assert next(a for a in state(client)['alarms'] if a['id']==alarm['id'])['trips']==2
    assert next(n for n in state(client)['notifications'] if n['channel']=='alarms')['status']=='recorded'
    cmd(client,'summary');cmd(client,'notification_read_all')
    assert all(n['read'] and n['read_at'] for n in state(client)['notifications'])


def test_logistics_limits_and_rollback(client):
    cmd(client,'order',target='GBX-OIL')
    with transaction(client.app.state.service) as (_, data):
        example = data['orders'][0]
        data['orders'] = [dict(example,id=f'SIM-{i:06d}') for i in range(200)]
    before = state(client)
    r = client.post('/api/emulation/commands',json=dict(action='order_all',expected_revision=before['revision'],request_id=uuid4().hex))
    assert r.status_code == 409 and '200' in r.text
    assert state(client) == before
    model=Stand(initial())
    for i in range(140): support.notify(model,'summary','info',str(i))
    assert len(model.s['notifications'])==120 and model.s['notifications'][0]['text']=='139'


@pytest.mark.parametrize('question, expected', [
    ('Что требует замены на R1?', 'склад'), ('Прогноз отказов', '±20%'),
    ('Какие нагрузки по осям?', 'полный ресурс редуктора'), ('Почему стоит линия?', 'аварий'),
    ('Что с конвейером?', 'вытяжка цепи'), ('Чего не хватает на складе?', '90 рабочих дней'),
    ('Где купить присоски?', 'учебный'), ('Закажи всё недостающее', 'подтвердите'),
    ('Отправь сводку', 'Внешняя отправка не подключена'),
])
def test_assistant_archive_topics_and_read_only_proposals(client, question, expected):
    before = state(client)
    r = client.post('/api/emulation/assistant',json={'question':question})
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer['engine']=='rules' and answer['source']=='emulation' and answer['revision']==before['revision']
    assert expected in answer['text']
    assert state(client)==before


def test_assistant_validation_arbitrary_orders_and_viewer(client):
    for question in (' ', '\x00', 'x'*1001):
        assert client.post('/api/emulation/assistant',json={'question':question}).status_code==422
    unknown = client.post('/api/emulation/assistant',json={'question':'Закажи неизвестный товар'}).json()
    assert unknown['actions']==[] and 'Уточните' in unknown['text']
    u = client.post('/api/admin/users',json={'username':'scada-reader','password':'Read only scada password 2026!','role':'viewer'}).json()
    with client.app.state.service.sessions.begin() as db: db.get(User,u['id']).must_change=False
    client.post('/api/auth/logout')
    assert client.post('/api/auth/login',json={'username':'scada-reader','password':'Read only scada password 2026!'}).status_code==200
    client.headers['X-CSRF-Token']=client.get('/api/auth/me').json()['csrf']
    answer=client.post('/api/emulation/assistant',json={'question':'Закажи присоски'})
    assert answer.status_code==200 and answer.json()['actions'][0]['action']=='order'
    assert client.post('/api/emulation/commands',json=dict(action='order',target='VAC-CUP',expected_revision=0,request_id=uuid4().hex)).status_code==403
    assert state(client)['orders']==[]
