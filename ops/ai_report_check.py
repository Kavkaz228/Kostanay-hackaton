"""Exercise AI reports with real local inference in a disposable installation.

Run from the tests image on the inference network, with backend/app mounted.
This creates a temporary SQLite database and never reads plant credentials/data.
"""
import json
import os
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import local_ai
from app.database import RobotCommand
from app.main import create_app


def checked(response):
    assert response.is_success, (response.status_code, response.text)
    return response.json()


def main():
    os.environ['BOOTSTRAP_PASSWORD'] = 'Isolated AI report validation 2026!'
    os.environ['MONITORING_AI_ENABLED'] = 'false'
    os.environ['COOKIE_SECURE'] = 'false'
    os.environ['PUBLIC_ORIGIN'] = ''
    started = time.monotonic()
    calls = []
    original = local_ai.ollama

    def counted(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)

    local_ai.ollama = counted
    try:
        with tempfile.TemporaryDirectory(prefix='allur-ai-report-check-') as directory:
            app = create_app('sqlite:///' + str(Path(directory) / 'isolated.db'), runner_enabled=False)
            with TestClient(app) as client:
                password = os.environ['BOOTSTRAP_PASSWORD']
                identity = checked(client.post('/api/auth/login', json={'username': 'admin', 'password': password}))
                client.headers['X-CSRF-Token'] = identity['csrf']
                checked(client.post('/api/auth/password', json={
                    'current_password': password, 'new_password': password + ' Changed',
                }))
                identity = checked(client.post('/api/auth/login', json={'username': 'admin', 'password': password + ' Changed'}))
                client.headers['X-CSRF-Token'] = identity['csrf']

                plant = checked(client.get('/api/ai/context?data_source=plant'))
                assert plant['data_source'] == 'plant' and plant['has_data'] is False
                empty = checked(client.post('/api/ai/analyze', json={
                    'data_source': 'plant', 'question': 'Проанализируй работу цеха и предложи действия.',
                }))
                assert empty['answer_kind'] == 'data_required', empty
                assert empty['recommendations'], 'Missing practical setup steps for the empty plant'
                assert '/api/chat' not in calls, 'An empty plant must not be sent to the model'

                simulation = checked(client.get('/api/ai/context?data_source=simulation'))
                assert simulation['data_source'] == 'simulation'
                assert simulation['has_data'] and simulation['metrics']
                stand_before = checked(client.get('/api/emulation'))
                preview = checked(client.get('/api/ai/context?data_source=emulation&robot_id=R2'))
                assert preview['has_data'] and preview['metrics'] and preview['readings']
                answer = checked(client.post('/api/ai/analyze', json={
                    'data_source': 'emulation', 'robot_id': 'R2',
                    'question': 'Назови конкретные узлы учебного R2, которые требуют внимания, приведи их показания и предложи 2–3 проверки. Различай подтверждённые факты и гипотезы.',
                }))
                assert answer['answer_kind'] == 'model'
                assert answer['data_source'] == 'emulation' and answer['proposal'] is None
                assert answer['action'] == 'none'
                assert answer['facts']['has_data'] and answer['recommendations']
                assert any(r.get('steps') for r in answer['recommendations'])
                assert any(r['origin'] == 'model' for r in answer['recommendations']), 'No grounded model recommendations survived validation'
                evidence_ids = ({f['id'] for f in answer['facts']['findings']}
                                | {r['id'] for r in answer['facts']['readings']}
                                | {m['key'] for m in answer['facts']['metrics']})
                assert all(set(r['evidence_ids']) <= evidence_ids for r in answer['recommendations'])
                assert answer['facts']['data_source'] == 'emulation'
                assert len([path for path in calls if path == '/api/chat']) >= 1
                assert checked(client.get('/api/emulation')) == stand_before, 'Analysis changed the virtual stand'
                with app.state.service.sessions() as db:
                    commands = db.scalar(select(func.count()).select_from(RobotCommand))
                assert commands == 0
                print(json.dumps({
                    'status': 'passed', 'seconds': round(time.monotonic() - started, 1),
                    'database': 'disposable SQLite', 'model': answer['model'],
                    'empty_plant_answer_kind': empty['answer_kind'],
                    'empty_plant_recommendations': len(empty['recommendations']),
                    'source': answer['facts']['source_label'],
                    'metrics': answer['facts']['metrics'],
                    'reading_count': len(answer['facts']['readings']),
                    'finding_count': len(answer['facts']['findings']),
                    'analysis': answer['analysis'], 'recommendations': answer['recommendations'],
                    'physical_commands': commands,
                }, ensure_ascii=False))
    finally:
        local_ai.ollama = original


if __name__ == '__main__':
    main()
