"""Explicit isolated-installation smoke check; never changes a customer password."""
import json
import os
import time
from pathlib import Path
import httpx

if os.getenv('LOAD_TEST_ALLOWED')!='isolated-only':
    raise SystemExit('Run only in the isolated verification Compose project')
with httpx.Client(base_url='http://web:8080',timeout=180) as client:
    login=client.post('/api/auth/login',json={'username':'admin','password':'Isolated E2E password 2026!'})
    login.raise_for_status()
    csrf=login.json()['csrf']
    status=client.get('/api/ai/status').json()
    assert status['ready'] and status['local_only']
    started=time.monotonic()
    response=client.post('/api/ai/analyze',headers={'X-CSRF-Token':csrf},json={
        'question':'Кратко назови подтверждённые дефициты запчастей из maintenance_and_stock и укажи, какие действия ещё должен выполнить оператор. Не выполняй команды.',
        'allow_command':False})
    response.raise_for_status()
    result=response.json()
    assert result['local_only'] and result['analysis'] and result['proposal'] is None
    report={'elapsed_seconds':round(time.monotonic()-started,2),'status':status,'response':result}
    Path('/results/scada-ai-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
