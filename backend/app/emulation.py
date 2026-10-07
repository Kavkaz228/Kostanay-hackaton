"""Authenticated, persistent, shared virtual stand; never plant telemetry."""
import copy
import hashlib
import json
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from .database import Snapshot
from .security import audit
from .emulation_model import Stand, SEED, initial
from . import emulation_support as support

router = APIRouter(prefix='/api/emulation',tags=['Эмуляция SCADA'])
KEY = 'scada-emulation-v1'


def encoded(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()


def publication(data, persisted=True):
    """Immutable-by-convention read model published only after a successful commit.

    Each history is encoded once per tick, not once per browser request. Plant
    ingestion invalidates its own cache but cannot force re-reading this 600KB
    independent snapshot hundreds of times a second.
    """
    m = Stand(data)
    details = {c['id']:encoded({'id':c['id'],'sensors':[dict(**d,history=c['hist'].get(d['k'],[])) for d in m.defs(c)]}) for c in m.cs}
    return dict(data=data,view=encoded(m.view()),details=details,persisted=persisted)


def current(twin):
    saved = getattr(twin,'emulation_publication',None)
    if saved is None:
        with twin.lock:
            saved = getattr(twin,'emulation_publication',None)
            if saved is None:
                with twin.sessions() as db:
                    row = db.get(Snapshot,KEY)
                    saved = publication(row.data if row else initial(),persisted=row is not None)
                twin.emulation_publication = saved
    return saved


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    request_id: str = Field(min_length=16,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    expected_revision: int = Field(ge=0)
    action: Literal['pause','play','speed','advance','stop','restart','fault','ack','replace','stock','reset',
                    'order','order_all','channel','summary','notification_read','notification_read_all']
    target: str | None = Field(default=None,max_length=100)
    active: bool | None = None
    value: int | None = None

    @model_validator(mode='after')
    def arguments(self):
        allowed = {'fault':{'target','active'},'ack':{'target'},'replace':{'target'},'stock':{'target','value'},
                   'speed':{'value'},'advance':{'value'},'order':{'target'},
                   'channel':{'target','active'},'notification_read':{'target'}}.get(self.action,set())
        for k in ('target','active','value'):
            if (getattr(self,k) is not None) != (k in allowed): raise ValueError(f'Неверные параметры действия: {self.action}')
        if self.action=='speed' and self.value not in (1,60,600,3600): raise ValueError('Скорость: 1, 60, 600 или 3600')
        if self.action=='advance' and not 1<=self.value<=3600: raise ValueError('Шаг: от 1 до 3600 секунд')
        if self.action=='stock' and not 1<=self.value<=100: raise ValueError('Пополнение: от 1 до 100 деталей')
        if self.action=='channel' and self.target not in support.CHANNELS: raise ValueError('Неизвестная категория внутренних уведомлений')
        return self


@contextmanager
def transaction(twin):
    # Use the same advisory-lock-owning connection as other mutations. This
    # snapshot is independent of application/Observation/ScadaRecord/commands.
    with twin.lock:
        twin._check_owner()
        previous = current(twin)
        writer = sessionmaker(bind=twin.owner_connection,expire_on_commit=False) if twin.owner_connection is not None else twin.sessions
        with writer.begin() as db:
            data = copy.deepcopy(previous['data'])
            yield db,data
            ready = publication(data)
            if previous['persisted']: db.execute(update(Snapshot).where(Snapshot.key==KEY).values(data=data))
            else: db.add(Snapshot(key=KEY,data=data))
        twin.emulation_publication = ready


def tick(twin, elapsed):
    with twin.lock:
        if current(twin)['data']['paused']: return
        with transaction(twin) as (_, data):
            Stand(data).advance(min(2,max(0,elapsed))*data['speed'])


@router.get('')
def snapshot(request: Request):
    return Response(current(request.app.state.service)['view'],media_type='application/json')


@router.post('/commands')
def command(body: Command, request: Request):
    twin = request.app.state.service
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(),sort_keys=True).encode()).hexdigest()
    with transaction(twin) as (db,s):
        receipt = next((r for r in s['receipts'] if r['id']==body.request_id),None)
        if receipt:
            if receipt['fingerprint'] != fingerprint: raise HTTPException(409,'Код команды уже использован')
            return receipt['response']
        if body.expected_revision != s['revision']: raise HTTPException(409,'Другой оператор изменил эмуляцию. Обновите состояние и повторите действие.')
        m = Stand(s)
        a, target = body.action,body.target
        if a in ('fault',) and target not in {f['id'] for f in SEED['faults']}: raise HTTPException(404,'Сценарий не найден')
        if a=='replace' and target not in m.by: raise HTTPException(404,'Узел не найден')
        if a=='stock' and (target not in SEED['parts'] or SEED['parts'][target].get('service')): raise HTTPException(404,'Деталь не найдена')
        if a=='order' and (target not in SEED['parts'] or SEED['parts'][target].get('service')): raise HTTPException(404,'Материальная деталь не найдена')
        try:
            if a=='pause': s['paused']=True
            elif a=='play': s['paused']=False
            elif a=='speed': s['speed']=body.value
            elif a=='stop': s['line']='stop'
            elif a=='advance':
                if not s['paused']: raise ValueError('Поставьте время эмуляции на паузу перед ручным шагом.')
                m.advance(body.value)
            elif a=='restart':
                if s['alarms']: raise ValueError('Устраните причины и квитируйте аварии перед пуском линии.')
                m.signals(0,True)
                m.evaluate()
                if s['alarms']:
                    # Save newly found alarms; a failed restart must not erase them.
                    m.log('alarm','Пуск заблокирован проверкой датчиков')
                else: s['line']='run'
            elif a=='fault': m.fault(target,body.active)
            elif a=='ack':
                alarm = next((v for v in s['alarms'] if v['id']==target),None)
                if not alarm: raise HTTPException(404,'Авария не найдена')
                if m.by[alarm['component_id']]['lv'].get(alarm['key'])=='alarm': raise ValueError('Причина аварии не устранена.')
                s['alarms'].remove(alarm)
            elif a=='replace': m.replace(target)
            elif a=='stock':
                pending=sum(o['quantity'] for o in s['orders'] if o['part']==target and o['status']=='in_transit')
                if s['stock'][target]+pending+body.value>10000: raise ValueError('Предельный учебный запас с поставками — 10 000 деталей.')
                s['stock'][target]+=body.value
                support.stock_check(m)
            elif a=='order': support.place_order(m,target)
            elif a=='order_all':
                catalog=support.parts_view(m)
                for part in catalog:
                    if part['deficit']: support.place_order(m,part['id'],catalog)
            elif a=='channel': s['channels'][target]=body.active
            elif a=='summary': support.notify(m,'summary','info',support.summary(m)['text'])
            elif a=='notification_read':
                item=next((n for n in s['notifications'] if str(n['id'])==target),None)
                if item is None: raise HTTPException(404,'Уведомление не найдено')
                item.update(read=True,read_at=m.clock())
            elif a=='notification_read_all':
                for item in s['notifications']:
                    if not item['read']: item.update(read=True,read_at=m.clock())
            elif a=='reset':
                receipts, revision = s['receipts'],s['revision']
                s.clear()
                s.update(initial(),receipts=receipts,revision=revision)
                m=Stand(s)
        except ValueError as exc: raise HTTPException(409,str(exc)) from exc
        s['revision'] += 1
        label = {'pause':'Пауза времени','play':'Запуск времени','speed':f'Скорость времени ×{body.value}',
                 'advance':f'Рассчитано {body.value} секунд','stop':'Остановка виртуальной линии',
                 'restart':'Пуск виртуальной линии' if s['line']=='run' else 'Пуск заблокирован',
                 'fault':'Изменение сценария неисправности','ack':'Авария квитирована',
                 'replace':'Обслуживание узла','stock':f'Учебный склад: добавлено {body.value}',
                 'order':'Создан учебный заказ','order_all':'Сформированы учебные заказы по дефициту',
                 'channel':'Настройка категории внутренних уведомлений',
                 'summary':'Сводка сохранена во внутреннем журнале',
                 'notification_read':'Уведомление просмотрено','notification_read_all':'Уведомления просмотрены',
                 'reset':'Эмуляция возвращена в исходное состояние'}[a]
        m.log('action',label+(f' · {target}' if target else ''))
        response = dict(revision=s['revision'],line=s['line'],paused=s['paused'],alarms=len(s['alarms']))
        s['receipts'].append(dict(id=body.request_id,fingerprint=fingerprint,response=response))
        del s['receipts'][:-256]
        audit(db,'emulation.'+a,target or '')
    return response


@router.get('/components/{identity}')
def component(identity: str, request: Request):
    data = current(request.app.state.service)['details'].get(identity)
    if data is None: raise HTTPException(404,'Узел не найден')
    return Response(data,media_type='application/json')


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    question: str = Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def nonempty(self):
        self.question = self.question.strip()
        if not self.question or any(ord(c)<32 and c not in '\n\t' for c in self.question):
            raise ValueError('Введите непустой вопрос без управляющих символов')
        return self


@router.post('/assistant')
def assistant(body: Question, request: Request):
    # Rules and suggestions only: do not acquire a write transaction, advance
    # the stand, place an order, or use a physical equipment gateway.
    return support.answer(Stand(current(request.app.state.service)['data']),body.question)
