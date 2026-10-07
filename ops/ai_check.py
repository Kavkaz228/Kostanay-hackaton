"""Explicit real-model smoke check for the disposable installation only."""
import json
import time
from pathlib import Path
import httpx

with httpx.Client(base_url='http://web:8080', timeout=175) as client:
    response = client.post('/api/auth/login', json={'username': 'admin', 'password': 'Isolated E2E password 2026!'})
    response.raise_for_status()
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    with Path('/samples/case-test-data.docx').open('rb') as document:
        response = client.post('/api/import', files={'file': ('case-test-data.docx', document)})
    response.raise_for_status()
    status = client.get('/api/ai/status'); status.raise_for_status()
    assert status.json()['ready'], status.text
    started = time.monotonic()
    response = client.post('/api/ai/analyze', json={'question': 'По документу кейса сравни сумму месячного плана по моделям с общим ориентиром. Укажи обе суммы и разницу в штуках. Достаточно ли данных для фактического OEE? Не предлагай команды роботам.', 'allow_command': False})
    response.raise_for_status()
    answer = response.json()
    assert answer['local_only'] and answer['proposal'] is None and answer['action'] == 'none', answer
    report = {'model_status': status.json(), 'elapsed_seconds': round(time.monotonic()-started, 2), 'answer': answer}
    Path('/results/ai-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
