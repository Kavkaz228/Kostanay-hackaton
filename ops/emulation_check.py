"""Isolated installation setup/readback for emulation performance and recovery QA."""
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from uuid import uuid4
import httpx
from sqlalchemy import delete, select
from sqlalchemy.engine import URL
from app.database import make_database, User, LoginSession


def main():
    if os.getenv('LOAD_TEST_ALLOWED') != 'isolated-only':
        raise SystemExit('Use only the isolated test installation.')
    url=URL.create('postgresql+psycopg',username='allur',password=Path('/run/secrets/database_password').read_text().strip(),host='db',database='allur').render_as_string(hide_password=False)
    engine,sessions=make_database(url)
    raw,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    identity=hashlib.sha256(raw.encode()).hexdigest()
    with sessions.begin() as db:
        user=db.scalar(select(User).where(User.username=='admin'))
        if not user or user.must_change: raise SystemExit('Initialize the disposable installation with browser setup first.')
        db.add(LoginSession(id=identity,user_id=user.id,csrf=csrf,expires=time.time()+600))
    try:
        with httpx.Client(base_url='http://web:8080',cookies={'allur_session':raw},headers={'X-CSRF-Token':csrf},timeout=30) as client:
            def state():
                r=client.get('/api/emulation');r.raise_for_status();return r.json()
            def cmd(action,**args):
                r=client.post('/api/emulation/commands',json=dict(request_id=uuid4().hex,expected_revision=state()['revision'],action=action,**args))
                r.raise_for_status()
            mode=os.getenv('EMULATION_CHECK','start')
            if mode=='start':
                cmd('reset');cmd('speed',value=3600);cmd('play')
                print('Isolated emulation running at x3600.')
            elif mode=='prepare_full':
                # Exercise the public command API, not direct snapshot writes.
                # Only the disposable stand changes; plant data is unrelated.
                cmd('reset')
                cmd('order',target='VAC-CUP')
                cmd('channel',target='stock',active=False)
                cmd('fault',target='f10',active=True)
                cmd('summary')
                notice=next(n for n in state()['notifications'] if n['channel']=='summary')
                cmd('notification_read',target=str(notice['id']))
                cmd('pause')
                d=state()
                orders=[o for o in d['orders'] if o['part']=='VAC-CUP' and o['status']=='in_transit']
                alarms=[a for a in d['alarms'] if a['component_id']=='CV-pos']
                assert d['paused'] and d['line']=='stop','Recovery stand must be paused with a latched alarm'
                assert len(orders)==1 and orders[0]['quantity']>0,'Expected a pending training purchase order'
                assert d['channels']['stock'] is False,'Notification preference did not persist'
                assert alarms and alarms[0]['trips']>=1,'Expected a saved fault trip'
                assert any(n['channel']=='summary' and n['read'] and n['read_at'] for n in d['notifications']),'Expected a read summary notice'
                assert all(not n['external_delivery'] for n in d['notifications']),'Stand notices must stay internal'
                Path('/results/emulation-before-restart.json').write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8')
                print(json.dumps({'mode':mode,'paused':d['paused'],'revision':d['revision'],
                    'components':len(d['components']),'orders':len(d['orders']),
                    'pending_order_quantity':sum(o['quantity'] for o in orders),
                    'notifications':len(d['notifications']),'read_notifications':sum(n['read'] for n in d['notifications']),
                    'alarms':len(d['alarms']),'fault_trips':alarms[0]['trips'],'channels':d['channels']}))
            elif mode=='save':
                cmd('pause');d=state()
                Path('/results/emulation-before-restart.json').write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8')
                print(json.dumps({'paused':d['paused'],'model_hours':d['t'],'components':len(d['components'])}))
            elif mode=='verify':
                before=json.loads(Path('/results/emulation-before-restart.json').read_text(encoding='utf-8'))
                assert state()==before,'Durable emulator state differs after API restart'
                print('PASS: entire emulation state preserved across PostgreSQL/API container restart.')
            else: raise SystemExit('Unknown check mode')
    finally:
        with sessions.begin() as db: db.execute(delete(LoginSession).where(LoginSession.id==identity))
        engine.dispose()


if __name__=='__main__':main()
