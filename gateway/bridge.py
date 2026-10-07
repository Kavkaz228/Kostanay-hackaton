"""Durable HTTP plant gateway. Connect only to a commissioned controller adapter.

The controller adapter must implement idempotent POST /commands keyed by command_id,
and return a terminal result only after hardware acknowledgement. No motion is simulated.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.parse import urlparse
from uuid import uuid4

log = logging.getLogger('allur.gateway')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Bridge:
    def __init__(self, config, api_key, controller_key, database):
        self.config, self.api_key, self.controller_key = config, api_key, controller_key
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        for field in ('api_url', 'controller_url'):
            parsed = urlparse(config[field])
            if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
                raise ValueError('Invalid URL configuration')
            if parsed.scheme != 'https' and not (parsed.scheme == 'http' and config.get('allow_plain_http') is True):
                raise ValueError('Use HTTPS; allow_plain_http is only for an isolated trusted network')
        if not config.get('robot_id') or not 1 <= config.get('max_speed_percent', 0) <= 100:
            raise ValueError('Set robot_id and commissioned max_speed_percent')
        self.db = sqlite3.connect(database)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS outbox (key TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL)')
        self.db.commit()
        # A process may have stopped after writing to hardware, before recording
        # its acknowledgement. Never repeat that physical write after restart.
        for key, data in self.db.execute("SELECT key,data FROM outbox WHERE kind='executing'").fetchall():
            command = json.loads(data)
            self.save(key, 'result', self.result(command, 'uncertain', 'Шлюз перезапущен во время выполнения. Сверьте контроллер; повтор не отправлялся.'))

    def save(self, key, kind, value):
        self.db.execute('INSERT INTO outbox(key,kind,data) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET kind=excluded.kind,data=excluded.data', (key,kind,json.dumps(value,ensure_ascii=False)))
        self.db.commit()

    def remove(self, key):
        self.db.execute('DELETE FROM outbox WHERE key=?',(key,));self.db.commit()

    def call(self, target, path, payload=None, request_id=None):
        key = self.api_key if target == 'api' else self.controller_key
        headers = {'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'}
        if request_id: headers['Idempotency-Key'] = request_id
        body = None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
        request = Request(self.config[target+'_url'].rstrip('/')+path, data=body, headers=headers)
        with self.opener.open(request, timeout=8) as reply:
            data = reply.read(1024*1024+1)
        if len(data)>1024*1024: raise ValueError('Adapter response exceeds limit')
        return json.loads(data)

    def observation(self):
        response = self.call('controller','/telemetry')
        row = response['measurement']
        if row['robot_id'] != self.config['robot_id']: raise ValueError('Controller returned a different robot')
        stamp = datetime.fromisoformat(row['timestamp'].replace('Z','+00:00'))
        if stamp.tzinfo is None: raise ValueError('Controller timestamp lacks timezone')
        return response

    @staticmethod
    def result(command, result, detail):
        return {'command_id':command['id'],'lease':command['lease'],'result':result,'detail':detail[:500]}

    def execute(self, command):
        # Commit intent to disk before any possible hardware write.
        self.save(command['id'],'executing',command)
        write_started = False
        try:
            if command['robot_id'] != self.config['robot_id'] or command['expires_at'] <= time.time():
                raise ValueError('Команда просрочена или относится к другому роботу')
            observed = self.observation()
            row = observed['measurement']
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(row['timestamp'].replace('Z','+00:00'))).total_seconds()
            if not -2<=age<=5 or row.get('controller_mode') != 'automatic' or observed.get('remote_control_enabled') is not True:
                raise ValueError('Нет свежего разрешения дистанционного управления')
            action=command['action']
            if action not in ('hold','resume','set_speed_percent'): raise ValueError('Неизвестное действие')
            if action!='hold' and (row.get('safety_state')!='normal' or row.get('cycle_status','').casefold() in ('fault','alarm','error')):
                raise ValueError('Аппаратная защита или ошибка запрещают действие')
            if action=='resume' and self.config.get('allow_resume') is not True:
                raise ValueError('Запуск не разрешён регламентом шлюза')
            if action=='set_speed_percent' and (type(command.get('speed_percent')) not in (int,float) or not 1<=command['speed_percent']<=self.config['max_speed_percent']):
                raise ValueError('Скорость вне пусконаладочного диапазона')
            payload={'command_id':command['id'],'robot_id':command['robot_id'],'action':action,'speed_percent':command.get('speed_percent'),'expires_at':command['expires_at']}
            write_started=True
            response=self.call('controller','/commands',payload,command['id'])
            if response.get('command_id')!=command['id'] or response.get('result') not in ('succeeded','failed') or response.get('hardware_acknowledged') is not True:
                raise ValueError('Нет однозначного подтверждения контроллера')
            result=self.result(command,response['result'],str(response.get('detail','Контроллер подтвердил результат')))
        except Exception as exc:
            # A failed HTTP response after a write never proves that hardware did nothing.
            result=self.result(command,'uncertain' if write_started else 'failed',str(exc))
        self.save(command['id'],'result',result)

    def cycle(self):
        for key,kind,data in self.db.execute('SELECT key,kind,data FROM outbox').fetchall():
            payload=json.loads(data)
            if kind=='measurement': self.call('api','/api/robots/measurements',payload,key)
            elif kind=='result': self.call('api','/api/gateway/result',payload)
            else: raise RuntimeError('Unresolved local execution state')
            self.remove(key)
        observed=self.observation()
        payload={'measurements':[observed['measurement']]}
        # Same observation timestamp is not sent twice across gateway restarts.
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        self.db.execute('CREATE TABLE IF NOT EXISTS last_measurement (id INTEGER PRIMARY KEY, digest TEXT NOT NULL)')
        prior=self.db.execute('SELECT digest FROM last_measurement WHERE id=1').fetchone()
        if not prior or prior[0]!=digest:
            key='measurement-'+digest
            self.save(key,'measurement',payload)
            self.call('api','/api/robots/measurements',payload,key)
            self.db.execute('INSERT INTO last_measurement(id,digest) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET digest=excluded.digest',(digest,))
            self.db.execute('DELETE FROM outbox WHERE key=?',(key,));self.db.commit()
        command=self.call('api','/api/gateway/next',{})['command']
        if command:
            self.execute(command)
            pending=json.loads(self.db.execute('SELECT data FROM outbox WHERE key=?',(command['id'],)).fetchone()[0])
            self.call('api','/api/gateway/result',pending)
            self.remove(command['id'])


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    config=json.loads(Path(os.environ.get('GATEWAY_CONFIG','/config/gateway.json')).read_text())
    bridge=Bridge(config,Path('/run/secrets/gateway_key').read_text().strip(),Path('/run/secrets/controller_key').read_text().strip(),'/data/gateway.sqlite')
    while True:
        try:
            bridge.cycle();Path('/data/last-success').write_text(str(time.time()))
        except Exception as exc:
            # No payloads, URLs with credentials, tokens or headers in logs.
            log.error('Gateway cycle failed: %s',type(exc).__name__)
        time.sleep(max(1,min(10,config.get('poll_seconds',2))))


if __name__=='__main__': main()
