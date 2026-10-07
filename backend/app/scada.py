"""SCADA stand capabilities backed by PostgreSQL and measured data.

Life estimates are engineering calculations with explicit assumptions, not a
trained failure predictor. Nothing here writes to a physical controller or sends
purchase orders. All mutations share the installation's single-writer lease.
"""
import copy
import csv
import hashlib
import io
import json
import math
import time
from datetime import datetime, timedelta, timezone, date
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select, func

from .database import ScadaRecord
from .scada_schema import (AssetBody, ComponentBody, Reading, ReadingBatch, PartBody,
    StockBody, OfferBody, OrderBody, OrderConfirm, ReceiveBody, TaskBody, TaskComplete, CalendarBody, ComponentUpdate,
    AssetOperationUpdate, Operation)
from .security import audit
from .robots import SENSORS
from .automation import operation_of

router = APIRouter(prefix='/api/scada', tags=['Оборудование, ТО и склад'])
COUNTERS = ('runtime_hours', 'total_cycles', 'distance_km')
DEFAULT_CALENDAR = CalendarBody().model_dump(mode='json')
LOCAL_TZ = timezone(timedelta(hours=5))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today():
    return datetime.now(LOCAL_TZ).date()


def get(db, kind, key):
    row = db.get(ScadaRecord, (kind, key))
    if not row: raise HTTPException(404, f'Запись {kind}/{key} не найдена')
    return row


def records(db, kind):
    return list(db.scalars(select(ScadaRecord).where(ScadaRecord.kind == kind).order_by(ScadaRecord.id)))


def add(db, kind, key, data, owner=''):
    row = ScadaRecord(kind=kind, id=key, owner=owner, created_at=time.time(), data=data)
    db.add(row)
    db.flush()
    return row


def create_once(db, kind, key, data, owner=''):
    existing = db.get(ScadaRecord, (kind, key))
    if existing:
        if existing.data.get('request') != data:
            raise HTTPException(409, 'Идентификатор уже использован для другой записи')
        return existing, False
    if kind in ('asset','component','part','offer') and db.scalar(select(func.count()).select_from(ScadaRecord).where(ScadaRecord.kind == kind)) >= 10000:
        raise HTTPException(409, 'Достигнут предел 10 000 записей справочника в установке')
    return add(db, kind, key, {**data, 'request':data}, owner), True


def calendar(db):
    row = db.get(ScadaRecord, ('settings','calendar'))
    return row.data if row else DEFAULT_CALENDAR


def work_date(start, days, cal):
    if days > 3660: return None
    current = start
    holidays = set(cal['holidays'])
    remaining = max(0, math.ceil(days))
    # Bounded even with sparse workweeks or a long holiday list.
    for _ in range(30000):
        if current.weekday() in cal['weekdays'] and current.isoformat() not in holidays:
            if remaining <= 0: return current.isoformat()
        current += timedelta(days=1)
        if current.weekday() in cal['weekdays'] and current.isoformat() not in holidays:
            remaining -= 1
    return None


def level(rule, value, previous):
    sign = 1 if rule['direction'] == 'high' else -1
    x, warning, alarm = sign*value, sign*rule['warning'], sign*rule['alarm']
    h = rule['hysteresis']
    if x >= alarm or (previous == 'alarm' and x >= alarm-h): return 'alarm'
    if x >= warning or (previous in ('warning','alarm') and x >= warning-h): return 'warning'
    return 'ok'


def alarm_update(db, component, data, metric, state, value, label, stamp):
    states = data.setdefault('states', {})
    states[metric] = state
    ids = data.setdefault('active_alarms', {})
    row = db.get(ScadaRecord, ('alarm', ids[metric])) if metric in ids else None
    if state in ('alarm','warning'):
        if row:
            row.data = {**row.data, 'severity':state, 'value':value, 'last_seen':stamp, 'recovered_at':None}
        else:
            key = str(uuid4())
            row = add(db, 'alarm', key, {'id':key, 'component_id':component.id, 'asset_id':component.owner,
                'component_name':data['name'], 'metric':metric, 'label':label, 'severity':state,
                'value':value, 'opened_at':stamp, 'last_seen':stamp, 'acknowledged_at':None,
                'acknowledged_by':None, 'recovered_at':None, 'status':'open'}, component.id)
            ids[metric] = row.id
    elif row:
        row.data = {**row.data, 'recovered_at':stamp, 'status':'closed' if row.data['acknowledged_at'] else 'recovered'}
        if row.data['status'] == 'closed': ids.pop(metric, None)


def wear_step(config, state, reading):
    """Only differences between observed cumulative counters count as use."""
    previous = None if state.get('baseline_pending') else state.get('last')
    gaps = []
    if not previous: return state.get('wear_pct'), None, ['Нужны два последовательных показания счётчика']
    delta = {}
    for key in COUNTERS:
        current, old = reading.get(key), previous.get(key)
        if current is not None and old is not None:
            if current < old: raise HTTPException(409, f'{key} уменьшился. Зарегистрируйте обслуживание со сбросом счётчиков.')
            delta[key] = current-old
    model, rated = config['life_model'], config['rated_life']
    if model == 'none': return state.get('wear_pct'), None, ['Модель ресурса не настроена']
    if model == 'calendar': return state.get('wear_pct'), None, []
    key = {'cycles':'total_cycles','distance':'distance_km'}.get(model,'runtime_hours')
    if key not in delta: return state.get('wear_pct'), None, [f'Нет последовательных показаний {key}']
    factor = 1
    metrics = reading['metrics']
    if model in ('thermal','reducer','chain'):
        needed = {'thermal':['temperature_c'], 'reducer':['load_ratio','speed_ratio'], 'chain':['load_ratio','lubrication_pct']}[model]
        if any(k not in metrics or k not in previous['metrics'] for k in needed):
            return state.get('wear_pct'), None, ['Для расчёта нужны '+', '.join(needed)+' в обоих показаниях']
        mean = {k:(metrics[k]+previous['metrics'][k])/2 for k in needed}
        if model == 'thermal': factor = 2**((mean['temperature_c']-config['reference_temperature_c'])/10)
        elif model == 'reducer': factor = mean['speed_ratio']*mean['load_ratio']**(10/3)
        else: factor = mean['load_ratio']**2*(1+8*(1-mean['lubrication_pct']/100))
    increment = delta[key]*factor/rated*100
    hours = delta.get('runtime_hours')
    rate = increment/hours if hours is not None and hours > 0 else None
    wear = state.get('wear_pct')
    if wear is None: gaps.append('Исходный износ неизвестен; нужна подтверждённая оценка или замена')
    return min(1000000, wear+increment) if wear is not None else None, rate, gaps


def observe(db, reading, origin='api'):
    value = reading.model_dump(mode='json') if isinstance(reading, Reading) else reading
    key = value['record_id']
    prior = db.get(ScadaRecord, ('reading',key))
    if prior:
        if prior.data['reading'] != value: raise HTTPException(409, 'record_id уже использован для других показаний')
        return False
    component = get(db,'component',value['component_id'])
    data = copy.deepcopy(component.data)
    stamp = value['timestamp']
    last = data.get('last')
    if data.get('last_service_at') and datetime.fromisoformat(stamp) <= datetime.fromisoformat(data['last_service_at']):
        raise HTTPException(409,'После обслуживания нужны показания, снятые позже времени выполнения работы')
    if last and datetime.fromisoformat(stamp) <= datetime.fromisoformat(last['timestamp']):
        raise HTTPException(409, 'Показания узла должны идти строго по времени; для повтора используйте тот же record_id')
    wear, rate, gaps = wear_step(data, data, value)
    # Missing intervals cannot silently produce an optimistic lifetime estimate.
    if last and not data.get('baseline_pending') and data['life_model'] not in ('none','calendar') and gaps and data.get('wear_pct') is not None:
        data['resource_incomplete'] = True
    data.update(last=value, wear_pct=wear, wear_rate_per_hour=rate, gaps=gaps, origin=origin, baseline_pending=False)
    for rule in data['thresholds']:
        if rule['metric'] in value['metrics']:
            state = level(rule,value['metrics'][rule['metric']],data.get('states',{}).get(rule['metric'],'ok'))
            alarm_update(db,component,data,rule['metric'],state,value['metrics'][rule['metric']],rule['label'],stamp)
    if wear is not None and data['life_model'] not in ('none','calendar'):
        alarm_update(db,component,data,'resource','alarm' if wear>=100 else 'warning' if wear>=80 else 'ok',wear,'Расчётный износ, %',stamp)
    component.data = data
    add(db,'reading',key,{'reading':value,'origin':origin},component.id)
    return True


def robot_bridge(db, rows):
    """Explicit mapping only: an aggregate robot temperature is not every axis."""
    assets = {a.id:a.data for a in records(db,'asset') if a.data.get('robot_id')}
    mapped = {}
    for c in records(db,'component'):
        if c.owner in assets and c.data['robot_metric_map']:
            mapped.setdefault(assets[c.owner]['robot_id'],[]).append(c)
    count = 0
    for row in rows:
        for component in mapped.get(row['robot_id'],[]):
            metrics = {dest:row[source] for source,dest in component.data['robot_metric_map'].items() if row.get(source) is not None}
            stamp = datetime.fromisoformat(row['timestamp'])
            last = component.data.get('last')
            if not metrics or stamp.timestamp()>time.time()+5 or (last and stamp<=datetime.fromisoformat(last['timestamp'])): continue
            key = 'robot:'+hashlib.sha256((component.id+':'+row['timestamp']).encode()).hexdigest()
            try:
                reading = Reading(record_id=key,component_id=component.id,timestamp=stamp,metrics=metrics)
            except ValidationError:
                continue
            count += observe(db,reading,'robot:'+row['robot_id'])
    return count


def component_view(row, cal):
    data = {k:v for k,v in row.data.items() if k != 'request'}
    last = data.get('last')
    age = (time.time()-datetime.fromisoformat(last['timestamp']).timestamp()) if last else None
    connection = 'missing' if age is None else 'stale' if age>data['expected_interval_seconds']*3 else 'fresh'
    wear, rate = data.get('wear_pct'), data.get('wear_rate_per_hour')
    gaps = list(data.get('gaps',[]))
    if data['life_model'] == 'calendar':
        installed = datetime.fromisoformat(data.get('last_service_at') or data['commissioned_at'])
        days = max(0,(datetime.now(timezone.utc)-installed).total_seconds()/86400)
        wear = min(1000000,(0 if data.get('last_service_at') else (data['initial_wear_pct'] or 0))+days/data['rated_life']*100)
        rate = None
    elif data.get('resource_incomplete'):
        gaps.append('В истории есть пропуск для расчёта износа; остаточный ресурс не подтверждён')
    remaining = max(0,(100-wear)/rate) if wear is not None and rate and rate>0 and not data.get('resource_incomplete') else None
    forecast_day = datetime.fromisoformat(last['timestamp']).astimezone(LOCAL_TZ).date() if last else today()
    due_date = work_date(forecast_day,remaining/cal['hours_per_day'],cal) if remaining is not None else None
    if connection == 'stale' and remaining is not None:
        gaps.append('Оценка относится к последнему показанию; данные устарели')
    if data['life_model']=='calendar':
        days_left = max(0,(100-wear)/100*data['rated_life'])
        due_date = (today()+timedelta(days=min(36500,math.ceil(days_left)))).isoformat() if days_left<=36500 else None
    states = list(data.get('states',{}).values())
    if wear is not None and data['life_model']!='none': states.append('alarm' if wear>=100 else 'warning' if wear>=80 else 'ok')
    severity = 'alarm' if 'alarm' in states else 'warning' if 'warning' in states else 'ok' if states and last else 'unknown'
    if connection != 'fresh' and severity=='ok': severity='unknown'
    return {**data,'wear_pct':wear,'remaining_hours':remaining,'due_date':due_date,'gaps':list(dict.fromkeys(gaps)),
        'connection':connection,'age_seconds':age,'severity':severity,
        'forecast_basis':'Инженерная оценка по регламенту и последнему интервалу; не вероятность отказа'}


def robot_operations(twin):
    # Robot rows are compacted and ordered by timestamp by the telemetry service.
    # Copy the resolved mapping under its lock; no polling request mutates it.
    with twin.lock:
        return {row['robot_id']:operation_of(row) for row in twin.robot_rows}


def asset_view(data, robots):
    result = {k:v for k,v in data.items() if k != 'request'}
    configured = data.get('operation')
    if configured in ('welding','painting','assembly','unknown'):
        operation, source = configured, 'explicit'
    else:
        operation = robots.get(data.get('robot_id'),'unknown')
        source = 'robot'
        if operation not in ('welding','painting','assembly'):
            operation = operation_of({'line_section':data.get('section','')})
            source = 'section' if operation!='unknown' else 'unknown'
    result.update(operation=operation,operation_configured=configured,operation_source=source,
                  revision=data.get('revision',1))
    return result


def due_for_part(component):
    return (component['severity']=='alarm' or
            component['wear_pct'] is not None and component['wear_pct']>=80 or
            component['due_date'] and component['due_date']<=(today()+timedelta(days=90)).isoformat())


def snapshot(twin):
    def read():
        robots = robot_operations(twin)
        with twin.sessions() as db:
            cal = calendar(db)
            assets = [asset_view(a.data,robots) for a in records(db,'asset')]
            components = [component_view(c,cal) for c in records(db,'component')]
            parts = [{k:v for k,v in p.data.items() if k!='request'} for p in records(db,'part')]
            offers = [{k:v for k,v in o.data.items() if k!='request'} for o in records(db,'offer')]
            active_orders = [r.data for r in records(db,'order') if r.data['status'] in ('draft','ordered')]
            alarms = list(db.scalars(select(ScadaRecord).where(ScadaRecord.kind=='alarm').order_by(ScadaRecord.created_at.desc()).limit(500)))
            # Unresolved alarms are never pushed out by the archive window.
            active_ids = {key for c in components for key in c.get('active_alarms',{}).values()}
            known = {a.id for a in alarms}
            if active_ids-known:
                alarms += list(db.scalars(select(ScadaRecord).where(ScadaRecord.kind=='alarm',ScadaRecord.id.in_(active_ids-known))))
            for p in parts:
                due = [c for c in components if c.get('part_id')==p['id'] and due_for_part(c)]
                p['needed'] = sum(c['part_quantity'] for c in due)
                p['on_order'] = sum(o['quantity']-o['received'] for o in active_orders if o['part_id']==p['id'] and o['status']=='ordered')
                p['deficit'] = max(0,p['needed']+p['minimum']-p['stock']-p['on_order'])
                deadlines = [c['due_date'] for c in due if c['due_date']]
                if any(c['severity']=='alarm' for c in due): deadlines.append(today().isoformat())
                p['needed_by'] = min(deadlines) if deadlines else None
                candidates = [o for o in offers if o['part_id']==p['id'] and o['valid_until']>=today().isoformat()]
                suggestions = []
                for currency in sorted({o['currency'] for o in candidates}):
                    group = [o for o in candidates if o['currency']==currency]
                    on_time = [o for o in group if not p['needed_by'] or (work_date(today(),o['lead_workdays'],cal) or '9999')<=p['needed_by']]
                    best = min(on_time,key=lambda o:(o['price_minor'],o['lead_workdays'],o['id'])) if on_time else min(group,key=lambda o:(o['lead_workdays'],o['price_minor'],o['id']))
                    suggestions.append({'offer_id':best['id'],'supplier':best['supplier'],'currency':currency,
                        'price_minor':best['price_minor'],'lead_workdays':best['lead_workdays'],
                        'reason':'Минимальная цена среди успевающих предложений' if on_time else 'Самый ранний срок; к расчётной дате не успевает'})
                p['recommendations'] = suggestions
            all_tasks = records(db,'task')
            recent_tasks = sorted(all_tasks,key=lambda r:r.created_at,reverse=True)[:500]
            task_ids = {r.id for r in recent_tasks}
            recent_tasks += [r for r in all_tasks if r.data['status']=='planned' and r.id not in task_ids]
            tasks = [r.data for r in recent_tasks]
            asset_map = {a['id']:a for a in assets}
            for a in assets:
                own = [c for c in components if c['asset_id']==a['id']]
                a['component_count'] = len(own)
                levels = [c['severity'] for c in own]
                a['severity'] = 'alarm' if 'alarm' in levels else 'warning' if 'warning' in levels else 'unknown' if not own or 'unknown' in levels else 'ok'
            return {'assets':assets,'components':components,'parts':parts,'offers':offers,'orders':active_orders,
                'alarms':[a.data for a in alarms], 'tasks':tasks,'calendar':cal,'updated_at':now_iso(),
                'limits':{'archived_alarms':500,'recent_tasks':500}, 'source':'measured',
                'totals':{'assets':len(asset_map),'components':len(components),'alarms':sum(a.data['status']!='closed' for a in alarms),'shortages':sum(p['deficit']>0 for p in parts)}}
    return twin.cached('scada-snapshot',read,2)


def scoped_snapshot(twin, operation):
    data = snapshot(twin)
    if operation is None:
        return data
    assets = [a for a in data['assets'] if a['operation']==operation]
    asset_ids = {a['id'] for a in assets}
    components = [c for c in data['components'] if c['asset_id'] in asset_ids]
    component_ids = {c['id'] for c in components}
    tasks = [t for t in data['tasks'] if t['component_id'] in component_ids]
    part_ids = {c['part_id'] for c in components if c.get('part_id')}
    # A planned task freezes the part to consume at creation. It remains
    # relevant even if the component is later configured with another SKU.
    part_ids.update(t['part_id'] for t in tasks if t.get('part_id'))
    # Stocks and orders belong to the installation. Filtering by relevant part
    # IDs is useful, but inventing a per-line allocation would be incorrect.
    parts = [{**p,'scope_needed':sum(c['part_quantity'] for c in components
                                    if c.get('part_id')==p['id'] and due_for_part(c)),
              'inventory_scope':'shared'} for p in data['parts'] if p['id'] in part_ids]
    alarms = [a for a in data['alarms'] if a['component_id'] in component_ids]
    return {**data,'assets':assets,'components':components,'parts':parts,'alarms':alarms,'tasks':tasks,
        'part_catalog':[{'id':p['id'],'name':p['name']} for p in data['parts']],
        'offers':[o for o in data['offers'] if o['part_id'] in part_ids],
        'orders':[o for o in data['orders'] if o['part_id'] in part_ids],
        'scope':{'operation':operation,'inventory':'shared',
                 'inventory_note':'Остатки, минимум, поставки, общая потребность и дефицит относятся ко всей установке; scope_needed — потребность выбранного участка.'},
        'totals':{'assets':len(assets),'components':len(components),
                  'alarms':sum(a['status']!='closed' for a in alarms),'shortages':sum(p['deficit']>0 for p in parts)}}


def ai_context(twin):
    data = snapshot(twin)
    components = sorted(data['components'],key=lambda c:({'alarm':0,'warning':1,'unknown':2,'ok':3}[c['severity']],c['due_date'] or '9999'))
    return {'totals':data['totals'],'components':[{k:c[k] for k in ('id','name','asset_id','severity','connection','wear_pct','remaining_hours','due_date','gaps')} for c in components[:12]],
        'shortages':[{k:p[k] for k in ('id','name','stock','needed','on_order','deficit','recommendations')} for p in data['parts'] if p['deficit']][:10],
        'limits':'До 12 приоритетных узлов и 10 дефицитов. Оценка ресурса расчётная; закупки не отправлены поставщику.'}


def check_robot_interlock(db, robot_id):
    assets = {a.id for a in records(db,'asset') if a.data.get('robot_id')==robot_id}
    if not assets: return
    for c in records(db,'component'):
        if c.owner not in assets: continue
        for key in c.data.get('active_alarms',{}).values():
            alarm = db.get(ScadaRecord,('alarm',key))
            if alarm and alarm.data['severity']=='alarm' and alarm.data['status']!='closed':
                raise HTTPException(409, 'У связанного узла есть авария SCADA. Устраните причину и подтвердите событие.')
        if c.data['life_model']=='calendar' and component_view(c,DEFAULT_CALENDAR)['wear_pct']>=100:
            raise HTTPException(409, 'Календарный ресурс связанного узла исчерпан. Требуется обслуживание.')


def calendar_alarm(db, row):
    if row.data['life_model']!='calendar': return
    wear=component_view(row,DEFAULT_CALENDAR)['wear_pct']
    severity='alarm' if wear>=100 else 'warning' if wear>=80 else 'ok'
    if row.data.get('states',{}).get('resource')!=severity:
        data=copy.deepcopy(row.data)
        alarm_update(db,row,data,'resource',severity,wear,'Календарный ресурс, %',now_iso())
        row.data=data


def reconcile_clock(twin):
    with twin.sessions() as db:
        changed=[]
        for row in records(db,'component'):
            if row.data['life_model']=='calendar':
                wear=component_view(row,DEFAULT_CALENDAR)['wear_pct']
                state='alarm' if wear>=100 else 'warning' if wear>=80 else 'ok'
                if row.data.get('states',{}).get('resource')!=state: changed.append(row.id)
    if changed:
        with twin._mutation() as db:
            for key in changed: calendar_alarm(db,get(db,'component',key))


@router.get('')
def overview(request: Request, operation: Operation | None = None):
    twin=request.app.state.service
    key='scada-json' if operation is None else ('scada-json-operation',operation)
    encoded=twin.cached(key,lambda:json.dumps(scoped_snapshot(twin,operation),ensure_ascii=False,allow_nan=False).encode(),2)
    return Response(encoded,media_type='application/json')


@router.post('/assets')
def create_asset(body: AssetBody, request: Request):
    twin=request.app.state.service
    with twin._mutation() as db:
        config=body.model_dump(mode='json')
        row=db.get(ScadaRecord,('asset',body.id))
        if row:
            # Preserve replay of pre-upgrade requests and the original create
            # request after a later reassignment. A replay must never undo edits.
            original={**row.data.get('request',{}),'operation':row.data.get('request',{}).get('operation')}
            if original!=config: raise HTTPException(409,'Идентификатор уже использован для другой записи')
        else:
            row,_=create_once(db,'asset',body.id,config)
            row.data={**row.data,'revision':1}
        return asset_view(row.data,robot_operations(twin))


@router.put('/assets/{asset_id}/operation')
def update_asset_operation(asset_id: str, body: AssetOperationUpdate, request: Request):
    twin=request.app.state.service
    payload={'asset_id':asset_id,**body.model_dump(mode='json')}
    with twin._mutation() as db:
        receipt=db.get(ScadaRecord,('asset_operation',body.request_id))
        if receipt:
            if receipt.data['request']!=payload: raise HTTPException(409,'Идентификатор изменения уже использован')
            return receipt.data['response']
        row=get(db,'asset',asset_id)
        revision=row.data.get('revision',1)
        if revision!=body.expected_revision:
            raise HTTPException(409,'Привязка оборудования уже изменена. Обновите страницу.')
        row.data={**row.data,'operation':body.operation,'revision':revision+1}
        result=asset_view(row.data,robot_operations(twin))
        add(db,'asset_operation',body.request_id,{'request':payload,'response':result},asset_id)
        audit(db,'scada.asset.operation',f'{asset_id}: {body.operation or "auto"}')
        return result


@router.post('/components')
def create_component(body: ComponentBody, request: Request):
    with request.app.state.service._mutation() as db:
        asset = get(db,'asset',body.asset_id)
        if body.part_id: get(db,'part',body.part_id)
        if body.robot_metric_map and (not asset.data.get('robot_id') or set(body.robot_metric_map)-SENSORS):
            raise HTTPException(422,'Для связи нужны robot_id оборудования и известные числовые поля телеметрии робота')
        row,created = create_once(db,'component',body.id,body.model_dump(mode='json'),body.asset_id)
        if created:
            row.data = {**row.data,'revision':1,'wear_pct':body.initial_wear_pct,'last':None,'states':{},'active_alarms':{},'resource_incomplete':False}
            calendar_alarm(db,row)
            latest = [r for r in request.app.state.service.robot_rows if r['robot_id']==asset.data.get('robot_id')]
            if latest: robot_bridge(db,[latest[-1]])
        return component_view(row,calendar(db))


@router.put('/components/{component_id}')
def update_component(component_id: str, body: ComponentUpdate, request: Request):
    with request.app.state.service._mutation() as db:
        row=get(db,'component',component_id)
        config=body.config.model_dump(mode='json')
        if row.data['revision']!=body.expected_revision: raise HTTPException(409,'Настройки уже изменились. Обновите страницу перед сохранением.')
        if config['id']!=row.id or config['asset_id']!=row.owner: raise HTTPException(422,'Код и принадлежность узла менять нельзя')
        asset=get(db,'asset',row.owner)
        if config['part_id']: get(db,'part',config['part_id'])
        if config['robot_metric_map'] and (not asset.data.get('robot_id') or set(config['robot_metric_map'])-SENSORS): raise HTTPException(422,'Проверьте связь с роботом и имена его датчиков')
        has_history=db.scalar(select(ScadaRecord.id).where(ScadaRecord.kind=='reading',ScadaRecord.owner==row.id).limit(1))
        has_tasks=db.scalar(select(ScadaRecord.id).where(ScadaRecord.kind=='task',ScadaRecord.owner==row.id).limit(1))
        life_keys=('life_model','rated_life','initial_wear_pct','reference_temperature_c','commissioned_at')
        if (has_history or has_tasks) and any(config[k]!=row.data[k] for k in life_keys):
            raise HTTPException(409,'После появления истории параметры расчёта ресурса фиксируются. Для другого узла/регламента заведите отдельный код; история не пересчитывается задним числом.')
        removed=set(row.data.get('active_alarms',{}))-{t['metric'] for t in config['thresholds']}-{'resource'}
        if removed: raise HTTPException(409,'Нельзя убрать контроль показателя с незакрытым событием')
        data=copy.deepcopy(row.data)
        data.update(config,request=config,revision=body.expected_revision+1)
        if not has_history and not has_tasks: data['wear_pct']=config['initial_wear_pct']
        row.data=data
        calendar_alarm(db,row)
        return component_view(row,calendar(db))


@router.post('/assets/{asset_id}/template')
def asset_template(asset_id: str, request: Request):
    """Equipment topology from the supplied stand, with unknown measurements/life."""
    with request.app.state.service._mutation() as db:
        asset=get(db,'asset',asset_id)
        if asset.data['kind']=='robot':
            items=[(f'J{i}-{kind}',f'{name} J{i}',f'J{i}',kind) for i in range(1,7)
                for kind,name in [('motor','Серводвигатель'),('reducer','Редуктор'),('brake','Тормоз')]]
            items += [('cable-low','Кабельный пакет J1–J3','cable','cable'),('cable-up','Кабельный пакет J4–J6','cable','cable'),
                ('battery','Батарея энкодеров','ctrl','battery'),('fan','Вентилятор шкафа','ctrl','fan'),('tool','Оснастка станции','tool','tool')]
        elif asset.data['kind']=='conveyor':
            items=[('motor','Двигатель привода','drive','motor'),('reducer','Редуктор привода','drive','reducer'),('oil','Масло редуктора','drive','other'),
                ('brake','Тормоз привода','drive','brake'),('vfd','Частотный преобразователь','drive','other'),('sprockets','Звёздочки привода','drive','other'),
                ('chain','Тяговая цепь','chain','chain'),('slats','Пластины настила','chain','other'),('rollers','Опорные ролики','chain','other'),
                ('lubricator','Лубрикатор цепи','chain','lubricator'),('tension','Натяжная станция','take','other'),
                ('encoder','Энкодер слежения','track','sensor'),('position','Датчики позиции','track','sensor')]
        else: raise HTTPException(422,'Для этого оборудования добавьте узлы вручную')
        prefix=asset_id if len(asset_id)<70 else hashlib.sha256(asset_id.encode()).hexdigest()[:32]
        added=0
        for suffix,name,node,kind in items:
            body=ComponentBody(id=prefix+'-'+suffix,asset_id=asset_id,name=name,node=node,kind=kind)
            existing=db.get(ScadaRecord,('component',body.id))
            if existing:
                if existing.owner!=asset_id: raise HTTPException(409,'Код типового узла уже принадлежит другому оборудованию')
                continue
            row,_=create_once(db,'component',body.id,body.model_dump(mode='json'),asset_id)
            row.data={**row.data,'revision':1,'wear_pct':None,'last':None,'states':{},'active_alarms':{},'resource_incomplete':False}
            added+=1
        return {'created':added}


@router.post('/readings')
def ingest(body: ReadingBatch, request: Request):
    with request.app.state.service._mutation() as db:
        added = sum(observe(db,r) for r in body.readings)
        return {'imported':added,'duplicates':len(body.readings)-added}


CSV_FIELDS = ['record_id','component_id','timestamp',*COUNTERS,'metrics_json']


@router.get('/template')
def csv_template():
    return Response(('\ufeff'+','.join(CSV_FIELDS)+'\r\n').encode(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="allur-components-template.csv"'})


@router.post('/import')
async def import_csv(request: Request, file: UploadFile = File(...)):
    content = await file.read(5*1024*1024+1)
    if len(content)>5*1024*1024: raise HTTPException(413,'CSV превышает 5 МБ')
    try:
        reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')),strict=True)
        if reader.fieldnames != CSV_FIELDS: raise ValueError('Столбцы должны соответствовать шаблону узлов')
        readings = []
        for raw in reader:
            if len(readings)>=1000: raise ValueError('Не более 1000 показаний за импорт')
            if None in raw or None in raw.values(): raise ValueError('Количество столбцов не совпадает с заголовком')
            row = {k:raw[k] for k in ('record_id','component_id','timestamp')}
            row.update({k:(int(raw[k]) if k=='total_cycles' else float(raw[k])) if raw[k] else None for k in COUNTERS})
            row['metrics'] = json.loads(raw['metrics_json'] or '{}')
            readings.append(Reading.model_validate(row))
        if not readings: raise ValueError('CSV не содержит показаний')
    except (UnicodeError,ValueError,csv.Error) as exc:
        raise HTTPException(422,'Неверный CSV узлов: '+str(exc)[:700]) from exc
    import asyncio
    return await asyncio.to_thread(ingest,ReadingBatch(readings=readings),request)


@router.get('/history')
def history(request: Request, component_id: str, offset: int=Query(0,ge=0,le=2147483647), limit: int=Query(100,ge=1,le=1000)):
    with request.app.state.service.sessions() as db:
        get(db,'component',component_id)
        q = select(ScadaRecord).where(ScadaRecord.kind=='reading',ScadaRecord.owner==component_id)
        total = db.scalar(select(func.count()).select_from(q.subquery()))
        rows = [r.data for r in db.scalars(q.order_by(ScadaRecord.created_at.desc(),ScadaRecord.id).offset(offset).limit(limit))]
        return {'total':total,'rows':rows,'offset':offset}


@router.get('/export')
def export_readings(request: Request, component_id: str):
    with request.app.state.service.sessions() as db: get(db,'component',component_id)
    def stream():
        buffer=io.StringIO(); writer=csv.DictWriter(buffer,fieldnames=CSV_FIELDS)
        writer.writeheader(); yield '\ufeff'+buffer.getvalue(); buffer.seek(0); buffer.truncate()
        with request.app.state.service.sessions() as db:
            q=select(ScadaRecord).where(ScadaRecord.kind=='reading',ScadaRecord.owner==component_id).order_by(ScadaRecord.created_at,ScadaRecord.id)
            for record in db.scalars(q).yield_per(500):
                row=record.data['reading']
                writer.writerow({k:row.get(k) for k in CSV_FIELDS if k!='metrics_json'}|{'metrics_json':json.dumps(row['metrics'],ensure_ascii=False)})
                yield buffer.getvalue(); buffer.seek(0); buffer.truncate()
    return StreamingResponse(stream(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="allur-component-history.csv"'})


@router.post('/alarms/{alarm_id}/acknowledge')
def acknowledge(alarm_id: str, request: Request):
    with request.app.state.service._mutation() as db:
        row=get(db,'alarm',alarm_id)
        if row.data['acknowledged_at']: return row.data
        row.data={**row.data,'acknowledged_at':now_iso(),'acknowledged_by':request.state.identity['username'],
            'status':'closed' if row.data['recovered_at'] else 'acknowledged'}
        if row.data['status']=='closed':
            c=get(db,'component',row.owner); data=copy.deepcopy(c.data); data.get('active_alarms',{}).pop(row.data['metric'],None); c.data=data
        return row.data


@router.put('/calendar')
def configure_calendar(body: CalendarBody, request: Request):
    with request.app.state.service._mutation() as db:
        row=db.get(ScadaRecord,('settings','calendar'))
        if row: row.data=body.model_dump(mode='json')
        else: add(db,'settings','calendar',body.model_dump(mode='json'))
        return body


@router.post('/parts')
def create_part(body: PartBody, request: Request):
    with request.app.state.service._mutation() as db:
        row,created=create_once(db,'part',body.id,body.model_dump())
        if created: row.data={**row.data,'stock':0}
        return {k:v for k,v in row.data.items() if k!='request'}


def stock_move(db, key, part_id, quantity, reason, source, payload):
    existing=db.get(ScadaRecord,('movement',key))
    if existing:
        if existing.data['request']!=payload or existing.data['source']!=source: raise HTTPException(409,'Идентификатор складской операции уже занят')
        return existing.data,False
    part=get(db,'part',part_id)
    after=part.data['stock']+quantity
    if after<0: raise HTTPException(409,'Недостаточно деталей на складе')
    if after>10**12: raise HTTPException(422,'Превышен допустимый складской остаток')
    part.data={**part.data,'stock':after}
    data={'id':key,'part_id':part_id,'quantity':quantity,'balance':after,'reason':reason,'source':source,'timestamp':now_iso(),'request':payload}
    add(db,'movement',key,data,part_id)
    return data,True


@router.post('/stock')
def adjust_stock(body: StockBody, request: Request):
    with request.app.state.service._mutation() as db:
        return stock_move(db,body.record_id,body.part_id,body.quantity,body.reason,'operator',body.model_dump())[0]


@router.get('/journal')
def journal(request: Request, kind: str=Query('movement',pattern='^(movement|order|task|alarm)$'),
            offset: int=Query(0,ge=0,le=2147483647), operation: Operation | None=None):
    robots=robot_operations(request.app.state.service) if operation is not None else {}
    with request.app.state.service.sessions() as db:
        q=select(ScadaRecord).where(ScadaRecord.kind==kind)
        if operation is not None:
            assets={a.id for a in records(db,'asset') if asset_view(a.data,robots)['operation']==operation}
            components=[c for c in records(db,'component') if c.owner in assets]
            component_ids={c.id for c in components}
            owners=component_ids if kind in ('task','alarm') else {c.data['part_id'] for c in components if c.data.get('part_id')}
            if kind in ('movement','order'):
                own_tasks=db.scalars(select(ScadaRecord).where(ScadaRecord.kind=='task',ScadaRecord.owner.in_(component_ids)))
                owners.update(t.data['part_id'] for t in own_tasks if t.data.get('part_id'))
            q=q.where(ScadaRecord.owner.in_(owners))
        return {'total':db.scalar(select(func.count()).select_from(q.subquery())),
            'rows':[{k:v for k,v in r.data.items() if k!='request'} for r in db.scalars(q.order_by(ScadaRecord.created_at.desc(),ScadaRecord.id).offset(offset).limit(100))],'offset':offset}


@router.post('/offers')
def create_offer(body: OfferBody, request: Request):
    with request.app.state.service._mutation() as db:
        get(db,'part',body.part_id)
        row,_=create_once(db,'offer',body.id,body.model_dump(mode='json'),body.part_id)
        return {k:v for k,v in row.data.items() if k!='request'}


@router.post('/orders')
def create_order(body: OrderBody, request: Request):
    with request.app.state.service._mutation() as db:
        existing=db.get(ScadaRecord,('order',body.id))
        if existing:
            if existing.data['request']!=body.model_dump(): raise HTTPException(409,'Номер заявки уже занят')
            return existing.data
        offer=get(db,'offer',body.offer_id)
        if offer.data['valid_until']<today().isoformat(): raise HTTPException(409,'Предложение поставщика просрочено')
        if offer.data['price_minor']*body.quantity>9_000_000_000_000_000:
            raise HTTPException(422,'Сумма заявки превышает допустимую точность денежного учёта')
        row,_=create_once(db,'order',body.id,body.model_dump(),offer.data['part_id'])
        row.data={**row.data,'part_id':offer.data['part_id'],'supplier':offer.data['supplier'],
            'price_minor':offer.data['price_minor'],'currency':offer.data['currency'],'lead_workdays':offer.data['lead_workdays'],
            'offer_valid_until':offer.data['valid_until'],'status':'draft','received':0,'created_at':now_iso(),'eta':None}
        return row.data


@router.post('/orders/{order_id}/confirm')
def confirm_order(order_id: str, body: OrderConfirm, request: Request):
    with request.app.state.service._mutation() as db:
        row=get(db,'order',order_id)
        if row.data['status']=='ordered' and row.data.get('reference')==body.reference: return row.data
        if row.data['status']!='draft': raise HTTPException(409,'Подтверждается только новая заявка')
        if row.data['offer_valid_until']<today().isoformat(): raise HTTPException(409,'Предложение просрочено; создайте заявку по актуальной цене')
        row.data={**row.data,'status':'ordered','reference':body.reference,'confirmed_at':now_iso(),
            'eta':work_date(today(),row.data['lead_workdays'],calendar(db))}
        return row.data


@router.post('/orders/{order_id}/receive')
def receive_order(order_id: str, body: ReceiveBody, request: Request):
    with request.app.state.service._mutation() as db:
        row=get(db,'order',order_id)
        payload={'order_id':order_id,**body.model_dump()}
        existing=db.get(ScadaRecord,('movement',body.record_id))
        if existing:
            if existing.data['source']!='purchase' or existing.data['request']!=payload: raise HTTPException(409,'Идентификатор приёмки уже занят')
            return row.data
        if row.data['status']!='ordered' or body.quantity>row.data['quantity']-row.data['received']:
            raise HTTPException(409,'Приёмка превышает открытый остаток подтверждённой заявки')
        stock_move(db,body.record_id,row.data['part_id'],body.quantity,body.reference,'purchase',payload)
        received=row.data['received']+body.quantity
        row.data={**row.data,'received':received,'status':'received' if received==row.data['quantity'] else 'ordered','last_received_at':now_iso()}
        return row.data


@router.post('/orders/{order_id}/cancel')
def cancel_order(order_id: str, request: Request):
    with request.app.state.service._mutation() as db:
        row=get(db,'order',order_id)
        if row.data['status']=='received': raise HTTPException(409,'Полученная заявка не отменяется')
        row.data={**row.data,'status':'cancelled','cancelled_at':now_iso()}
        return row.data


@router.post('/tasks')
def create_task(body: TaskBody, request: Request):
    with request.app.state.service._mutation() as db:
        get(db,'component',body.component_id)
        existing=db.get(ScadaRecord,('task',body.id))
        if existing:
            if existing.data['request']!=body.model_dump(mode='json'): raise HTTPException(409,'Номер работы уже занят')
            return existing.data
        if any(r.data['status']=='planned' for r in db.scalars(select(ScadaRecord).where(ScadaRecord.kind=='task',ScadaRecord.owner==body.component_id))):
            raise HTTPException(409,'Для этого узла уже запланировано обслуживание')
        row,_=create_once(db,'task',body.id,body.model_dump(mode='json'),body.component_id)
        component=get(db,'component',body.component_id)
        row.data={**row.data,'status':'planned','created_at':now_iso(),'part_id':component.data['part_id'],'part_quantity':component.data['part_quantity']}
        return row.data


@router.post('/tasks/{task_id}/complete')
def complete_task(task_id: str, body: TaskComplete, request: Request):
    with request.app.state.service._mutation() as db:
        task=get(db,'task',task_id)
        if task.data['status']=='done':
            if task.data['completion']!=body.model_dump(): raise HTTPException(409,'Работа уже закрыта с другим актом')
            return task.data
        if task.data['status']!='planned': raise HTTPException(409,'Работа отменена')
        component=get(db,'component',task.owner); data=copy.deepcopy(component.data)
        if task.data['part_id']:
            stock_move(db,'task:'+hashlib.sha256(task_id.encode()).hexdigest(),task.data['part_id'],-task.data['part_quantity'],body.evidence,'maintenance',{'task_id':task_id,**body.model_dump()})
        stamp=now_iso()
        data.update(wear_pct=0,wear_rate_per_hour=None,resource_incomplete=False,last_service_at=stamp,gaps=[],baseline_pending=True)
        # Keep actual sensors and their alarms; replacing a part does not invent recovery.
        if body.reset_counters: data['last']=None
        alarm_update(db,component,data,'resource','ok',0,'Расчётный износ, %',stamp)
        component.data=data
        task.data={**task.data,'status':'done','completed_at':stamp,'completed_by':request.state.identity['username'],'completion':body.model_dump()}
        return task.data


@router.post('/tasks/{task_id}/cancel')
def cancel_task(task_id: str, request: Request):
    with request.app.state.service._mutation() as db:
        row=get(db,'task',task_id)
        if row.data['status']=='done': raise HTTPException(409,'Выполненная работа не отменяется')
        row.data={**row.data,'status':'cancelled'}
        return row.data
