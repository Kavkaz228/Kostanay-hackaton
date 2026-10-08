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
    assert seen[0]['facts']['data_source'] == 'emulation'
    assert seen[0]['selected_robot'] == 'R2'
    assert seen[0]['facts']['findings'] and data['facts']['readings']
    assert not any(r['asset'].startswith(('R1', 'R3', 'R4', 'CV')) for r in data['facts']['readings'])
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
    assert client.post('/api/robots/measurements',json={'measurements':[robot('REAL-CONTROLLER')]}).status_code == 200
    seen=[]
    def model(path,payload,**kwargs):
        seen.append(payload['messages'][1]['content'])
        return fake_decision('none')
    monkeypatch.setattr(local_ai,'ollama',model)
    reply=client.post('/api/ai/analyze',json={'question':'Проанализируй производство'})
    assert reply.status_code == 200 and reply.json()['data_source'] == 'plant'
    assert reply.json()['emulation_facts'] is None
    assert 'REAL-CONTROLLER' in seen[0] and '"stand"' not in seen[0]
    assert reply.json()['facts']['has_data'] is True


def test_empty_plant_returns_setup_steps_without_invoking_model(client, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('An empty source must not waste a model call')
    monkeypatch.setattr(local_ai, 'ollama', unexpected)
    reply = client.post('/api/ai/analyze', json={'question': 'Что происходит в цехе?'})
    assert reply.status_code == 200
    data = reply.json()
    assert data['answer_kind'] == 'data_required' and data['facts']['has_data'] is False
    assert data['recommendations'][0]['steps'] and data['recommendations'][0]['origin'] == 'rules'
    assert data['proposal'] is None


def test_simulation_keeps_model_commands_disabled(client, monkeypatch):
    assert client.post('/api/advance', json={'seconds': 3600}).status_code == 200
    monkeypatch.setattr(local_ai, 'ollama', lambda *a, **kw: fake_decision('hold'))
    reply = client.post('/api/ai/analyze', json={'question': 'Что улучшить на линии?', 'data_source': 'simulation'})
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert data['facts']['has_data'] and data['action'] == 'none' and data['proposal'] is None
    assert data['emulation_facts'] is None and data['maintenance_facts'] is None
    assert client.post('/api/ai/analyze', json={'question': 'Останови', 'data_source': 'simulation', 'allow_command': True}).status_code == 422
    assert client.get('/api/ai/context?data_source=simulation&robot_id=R2').status_code == 422


def test_invented_recommendation_reference_falls_back_to_labeled_rules(client, monkeypatch):
    def model(*a, **kw):
        value = json.loads(fake_decision('none')['message']['content'])
        value['recommendations'] = [{'title': 'Несуществующее показание', 'reason': 'Выдуманное основание',
            'steps': ['Проверить узел'], 'priority': 'warning', 'evidence_ids': ['nonexistent-evidence']}]
        return {'message': {'content': json.dumps(value)}}
    monkeypatch.setattr(local_ai, 'ollama', model)
    data = client.post('/api/ai/analyze', json={'question': 'Что проверить?', 'data_source': 'emulation'}).json()
    assert data['recommendations']
    assert all(item['origin'] == 'rules' for item in data['recommendations'])
    assert not any(item['title'] == 'Несуществующее показание' for item in data['recommendations'])


def test_advice_severity_and_evidence_are_owned_by_measurements(client, monkeypatch):
    def model(path, payload, **kwargs):
        context = json.loads(payload['messages'][1]['content'].split('Наблюдения JSON:\n', 1)[1].split('\nВопрос:\n', 1)[0])
        finding = context['facts']['findings'][0]
        value = json.loads(fake_decision('none')['message']['content'])
        value['recommendations'] = [{'title': 'Проверить выбранный узел', 'reason': 'Модель ошибочно усилила важность',
            'steps': ['Проверить датчик и карточку узла'], 'priority': 'critical', 'evidence_ids': [finding['id']]}]
        return {'message': {'content': json.dumps(value)}}
    monkeypatch.setattr(local_ai, 'ollama', model)
    data = client.post('/api/ai/analyze', json={'question': 'Что проверить?', 'data_source': 'emulation', 'robot_id': 'R2'}).json()
    advice = data['recommendations'][0]
    finding = next(f for f in data['facts']['findings'] if f['id'] == advice['evidence_ids'][0])
    assert advice['origin'] == 'model'
    assert advice['reason'] == finding['evidence'] and advice['priority'] == finding['severity']


def test_local_model_unavailability_does_not_fabricate_answer(client, monkeypatch):
    from fastapi import HTTPException
    def unavailable(*args,**kwargs):
        raise HTTPException(503,'Локальная модель недоступна')
    monkeypatch.setattr(local_ai,'ollama',unavailable)
    reply=client.post('/api/ai/analyze',json={'question':'Состояние учебного стенда','data_source':'emulation'})
    assert reply.status_code == 503
    assert 'analysis' not in reply.json()
