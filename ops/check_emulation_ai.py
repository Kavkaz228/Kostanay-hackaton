"""Real local-model smoke check, explicitly restricted to an isolated installation."""
import json
import os
import time
from pathlib import Path
import httpx

if os.getenv('LOAD_TEST_ALLOWED') != 'isolated-only':
    raise SystemExit('Run only in the isolated verification Compose project')

with httpx.Client(base_url='http://web:8080', timeout=180) as client:
    login=client.post('/api/auth/login',json={'username':'admin','password':'Isolated E2E password 2026!'})
    login.raise_for_status()
    csrf=login.json()['csrf']
    status=client.get('/api/ai/status').json()
    assert status['ready'] and status['local_only']
    before=client.get('/api/automation').json()['commands']
    started=time.monotonic()
    response=client.post('/api/ai/analyze',headers={'X-CSRF-Token':csrf},json={
        'question':'Это учебный стенд. Назови выбранный робот и один конкретный узел, требующий внимания, по переданным показаниям. Не выполняй никаких действий. Ответь кратко.',
        'data_source':'emulation','robot_id':'R2','allow_command':False})
    response.raise_for_status()
    result=response.json()
    assert result['local_only'] and result['data_source']=='emulation'
    assert result['analysis'] and result['proposal'] is None and result['action']=='none'
    assert result['maintenance_facts'] is None
    assert result['emulation_facts']['selected_robot']=='R2'
    assert all(c['robot']=='R2' for c in result['emulation_facts']['components'])
    assert client.get('/api/automation').json()['commands']==before
    report={'elapsed_seconds':round(time.monotonic()-started,2),'status':status,'response':result}
    Path('/results/emulation-ai-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'elapsed_seconds':report['elapsed_seconds'],'model':status['model'],
                     'data_source':result['data_source'],'analysis':result['analysis'],'reason':result['reason'],
                     'proposal':result['proposal'],'physical_commands_unchanged':True},ensure_ascii=False,indent=2))
