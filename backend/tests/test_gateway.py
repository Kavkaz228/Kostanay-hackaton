"""A real loopback HTTP controller fixture tests transport and fail-closed behaviour.
This fixture is test-only and is never shipped as a connected factory controller.
"""
import sys
import json
import time
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
sys.path.insert(0,'/gateway')
from bridge import Bridge


@pytest.fixture
def controller():
    state={'writes':0,'remote':True,'safety':'normal','lose_ack':False,'redirect':False,'redirected':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,body,status=200):
            self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(body).encode())
        def do_GET(self):
            assert self.headers['Authorization']=='Bearer controller-secret'
            if self.path == '/redirected': state['redirected'] += 1
            elif state['redirect']:
                self.send_response(302); self.send_header('Location','/redirected'); self.end_headers(); return
            self.reply({'remote_control_enabled':state['remote'],'measurement':{'robot_id':'R-1','timestamp':datetime.now(timezone.utc).isoformat(),'controller_mode':'automatic','safety_state':state['safety'],'cycle_status':'In_Progress','joint_temperature_c':42}})
        def do_POST(self):
            value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['writes']+=1
            if state['lose_ack']:self.reply({'error':'transport interrupted after write'},503)
            else:self.reply({'command_id':value['command_id'],'result':'succeeded','hardware_acknowledged':True,'detail':'Fixture controller acknowledged'})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield state,'http://127.0.0.1:'+str(server.server_port)
    server.shutdown();server.server_close();thread.join()


def config(url):
    return {'api_url':'http://127.0.0.1:1','controller_url':url,'robot_id':'R-1','max_speed_percent':25,'allow_resume':False,'allow_plain_http':True}


def command(action='hold',speed=None):
    return {'id':str(uuid4()),'robot_id':'R-1','action':action,'speed_percent':speed,'expires_at':time.time()+20,'lease':'a'*32}


def stored(bridge,c):
    return json.loads(bridge.db.execute('SELECT data FROM outbox WHERE key=?',(c['id'],)).fetchone()[0])


def test_real_http_write_is_acknowledged(controller,tmp_path):
    state,url=controller
    bridge=Bridge(config(url),'api-secret','controller-secret',str(tmp_path/'gateway.sqlite'))
    c=command();bridge.execute(c)
    assert state['writes']==1 and stored(bridge,c)['result']=='succeeded'
    bridge.db.close()


def test_redirects_never_forward_credentials_or_plant_data(controller,tmp_path,monkeypatch):
    from app import local_ai
    from fastapi import HTTPException
    state,url=controller
    state['redirect']=True
    bridge=Bridge(config(url),'api-secret','controller-secret',str(tmp_path/'gateway.sqlite'))
    c=command();bridge.execute(c)
    assert state['redirected']==0 and state['writes']==0 and stored(bridge,c)['result']=='failed'
    # The AI HTTP client uses the same no-redirect policy.
    from urllib.request import Request
    from urllib.error import HTTPError
    with pytest.raises(HTTPError):
        local_ai.opener.open(Request(url+'/telemetry',headers={'Authorization':'Bearer controller-secret'}),timeout=2)
    assert state['redirected']==0
    bridge.db.close()


def test_no_write_without_local_permission_or_over_speed(controller,tmp_path):
    state,url=controller
    bridge=Bridge(config(url),'api-secret','controller-secret',str(tmp_path/'gateway.sqlite'))
    for c in (command('resume'),command('set_speed_percent',26)):
        bridge.execute(c);assert stored(bridge,c)['result']=='failed'
    state['remote']=False
    c=command();bridge.execute(c)
    assert stored(bridge,c)['result']=='failed' and state['writes']==0
    bridge.db.close()


def test_lost_ack_and_restart_never_resend_to_hardware(controller,tmp_path):
    state,url=controller
    path=str(tmp_path/'gateway.sqlite')
    bridge=Bridge(config(url),'api-secret','controller-secret',path)
    state['lose_ack']=True
    c=command();bridge.execute(c)
    assert stored(bridge,c)['result']=='uncertain' and state['writes']==1
    interrupted=command();bridge.save(interrupted['id'],'executing',interrupted);bridge.db.close()
    restored=Bridge(config(url),'api-secret','controller-secret',path)
    assert stored(restored,interrupted)['result']=='uncertain' and state['writes']==1
    restored.db.close()
