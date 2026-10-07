import json

import pytest

from test_api import client, db_url
from test_manufacturing import robot
from app import local_ai


def fake_decision(action='hold'):
    return {'message':{'content':json.dumps({'analysis':'Анализ учебных показаний','action':action,'speed_percent':None,'reason':'Данные стенда'})}}


def test_emulation_ai_never_reads_or_commands_plant(client, monkeypatch):
    assert client.post('/api/robots/measurements',json={'measurements':[robot('REAL-CONTROLLER')]}).status_code == 200
    plant_before = client.get('/api/state').json()
    commands_before = client.get('/api/automation').json()['commands']
    seen = []
    def model(path, payload, **kwargs):
        text = payload['messages'][1]['content']
        context = json.loads(text.split('Наблюдения JSON:\n',1)[1].split('\nВопрос:\n',1)[0])
        seen.append(context)
        return fake_decision()
    monkeypatch.setattr(local_ai,'ollama',model)
    reply = client.post('/api/ai/analyze',json={'question':'Что с учебным R2?','data_source':'emulation','robot_id':'R2'})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data['data_source'] == 'emulation' and data['action'] == 'none' and data['proposal'] is None
    assert data['maintenance_facts'] is None
    assert data['emulation_facts']['component_count'] > 12
    assert len(data['emulation_facts']['components']) == 12
    assert all(c['robot']=='R2' for c in seen[0]['stand']['components'])
    assert 'REAL-CONTROLLER' not in json.dumps(seen)
    assert 'vehicle_quality' not in seen[0] and 'maintenance_and_stock' not in seen[0]
    assert client.get('/api/automation').json()['commands'] == commands_before
    assert client.get('/api/state').json()['source'] == plant_before['source']
    assert client.get('/api/state').json()['robots'] == plant_before['robots']


@pytest.mark.parametrize('extra', [{'allow_command':True}, {'robot_id':'REAL-CONTROLLER'}, {'data_source':'unknown'}])
def test_emulation_source_rejects_plant_commands_and_unknown_equipment(client, monkeypatch, extra):
    def unexpected(*args,**kwargs):
        raise AssertionError('Invalid request must not invoke inference')
    monkeypatch.setattr(local_ai,'ollama',unexpected)
    reply = client.post('/api/ai/analyze',json={'question':'Остановить робота','data_source':'emulation',**extra})
    assert reply.status_code == 422


def test_default_ai_source_preserves_plant_contract(client, monkeypatch):
    seen=[]
    def model(path,payload,**kwargs):
        seen.append(payload['messages'][1]['content'])
        return fake_decision('none')
    monkeypatch.setattr(local_ai,'ollama',model)
    reply=client.post('/api/ai/analyze',json={'question':'Проанализируй производство'})
    assert reply.status_code == 200 and reply.json()['data_source'] == 'plant'
    assert reply.json()['emulation_facts'] is None
    assert 'maintenance_and_stock' in seen[0] and '"stand"' not in seen[0]


def test_local_model_unavailability_does_not_fabricate_answer(client, monkeypatch):
    from fastapi import HTTPException
    def unavailable(*args,**kwargs):
        raise HTTPException(503,'Локальная модель недоступна')
    monkeypatch.setattr(local_ai,'ollama',unavailable)
    reply=client.post('/api/ai/analyze',json={'question':'Состояние учебного стенда','data_source':'emulation'})
    assert reply.status_code == 503
    assert 'analysis' not in reply.json()
