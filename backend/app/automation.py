"""Observed robot state and an operator-approved, expiring physical command queue."""
import hashlib
import secrets
import time
from datetime import datetime, timezone
from uuid import uuid4
import os
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select, or_
from typing import Literal

from .database import RobotCommand, GatewayKey
from .schemas import StrictModel
from .security import audit


class CommandBody(StrictModel):
    robot_id: str = Field(min_length=1, max_length=128)
    action: Literal['hold', 'resume', 'set_speed_percent']
    speed_percent: float | None = Field(default=None, ge=1, le=100)
    reason: str = Field(min_length=3, max_length=1000)


class Approval(StrictModel):
    confirm: Literal[True]


class GatewayCreate(StrictModel):
    robot_id: str = Field(min_length=1, max_length=128)
    days: int = Field(default=30, ge=1, le=90)


class CommandResult(StrictModel):
    command_id: str
    lease: str = Field(min_length=20, max_length=128)
    result: Literal['succeeded', 'failed', 'uncertain']
    detail: str = Field(max_length=500)


class Reconciliation(StrictModel):
    confirm: Literal[True]
    result: Literal['succeeded', 'failed']
    evidence: str = Field(min_length=20, max_length=500)


def operation_of(row):
    if row.get('operation'): return row['operation']
    section = row.get('line_section', '').casefold()
    for operation, words in [('welding', ('weld', 'свар')), ('painting', ('paint', 'окрас', 'покрас')), ('assembly', ('assembl', 'сбор'))]:
        if any(word in section for word in words): return operation
    return 'unknown'


def physical_enabled():
    return os.getenv('PHYSICAL_CONTROL_ENABLED', 'false').lower() == 'true'


def robot_snapshot(twin, robot_id=None):
    with twin.lock:
        latest = {r['robot_id']: r.copy() for r in twin.robot_rows}
        rows = list(latest.values())
        if robot_id:
            if robot_id not in latest: raise HTTPException(404, 'Нет наблюдений этого робота')
            rows = [latest[robot_id]]
        now = datetime.now(timezone.utc)
        for row in rows:
            age = (now-datetime.fromisoformat(row['timestamp'])).total_seconds()
            interval = twin.robot_configs.get(row['robot_id'], {}).get('expected_interval_seconds', 60)
            row.update(operation=operation_of(row), age_seconds=round(age, 1), connection='future' if age < -5 else 'stale' if age > interval*3 else 'fresh')
        return rows


def validate_command(twin, body, db):
    if not physical_enabled(): raise HTTPException(409, 'Физическое управление выключено. Нужны настроенный шлюз и пусконаладка контроллера.')
    row = robot_snapshot(twin, body['robot_id'])[0]
    if not -5 <= row['age_seconds'] <= 15: raise HTTPException(409, 'Для команды нужны показания не старше 15 секунд и синхронизированные часы')
    if row.get('controller_mode') != 'automatic': raise HTTPException(409, 'Контроллер должен сообщать автоматический режим')
    if body['action'] != 'hold' and (row.get('safety_state') != 'normal' or row['cycle_status'].casefold() in ('fault', 'error', 'alarm')):
        raise HTTPException(409, 'Защита или состояние контроллера запрещают запуск/скорость')
    if body['action'] != 'hold':
        from .scada import check_robot_interlock
        check_robot_interlock(db, body['robot_id'])
    if not db.scalar(select(GatewayKey.id).where(GatewayKey.robot_id == body['robot_id'], GatewayKey.active.is_(True), GatewayKey.expires > time.time()).limit(1)):
        raise HTTPException(409, 'Для робота не зарегистрирован действующий ключ шлюза')
    if body['action'] == 'set_speed_percent' and body.get('speed_percent') is None: raise HTTPException(422, 'Укажите скорость')
    if body['action'] != 'set_speed_percent' and body.get('speed_percent') is not None: raise HTTPException(422, 'Параметр скорости не относится к этой команде')


def public_command(row):
    return {'id': row.id, 'robot_id': row.robot_id, 'status': row.status, 'created_at': row.created_at,
            **{k: v for k, v in row.data.items() if k not in ('lease_hash', 'gateway_id')}}


def expire_commands(db):
    for row in db.scalars(select(RobotCommand).where(RobotCommand.status.in_(['proposed', 'approved', 'dispatched']))):
        if row.data['expires_at'] <= time.time():
            row.status = 'uncertain' if row.status == 'dispatched' else 'expired'
            row.data = {**row.data, 'result_detail': 'Истёк срок. Выданная шлюзу команда не повторяется автоматически.'}


def propose(twin, body, actor, origin='operator'):
    robot_snapshot(twin, body['robot_id'])
    if (body['action'] == 'set_speed_percent') != (body.get('speed_percent') is not None):
        raise HTTPException(422, 'Скорость нужна только для set_speed_percent')
    with twin._mutation() as db:
        expire_commands(db)
        row = RobotCommand(id=str(uuid4()), robot_id=body['robot_id'], status='proposed', created_at=time.time(), data={**body, 'origin': origin, 'proposed_by': actor, 'expires_at': time.time()+300})
        db.add(row)
        return public_command(row)


router = APIRouter()


@router.get('/api/automation')
def automation(request: Request):
    twin = request.app.state.service
    def read():
        # Expiry is also enforced transactionally at every command transition.
        with twin.sessions() as db:
            latest = select(RobotCommand.id).order_by(RobotCommand.created_at.desc()).limit(100)
            # Pending physical outcomes must remain visible beyond the history window.
            query = select(RobotCommand).where(or_(RobotCommand.id.in_(latest), RobotCommand.status.in_(['approved', 'dispatched', 'uncertain']))).order_by(RobotCommand.created_at.desc())
            commands = [public_command(r) for r in db.scalars(query)]
        for c in commands:
            if c['status'] in ('proposed', 'approved', 'dispatched') and c['expires_at'] <= time.time():
                c['status'] = 'uncertain' if c['status'] == 'dispatched' else 'expired'
        return json.dumps({'robots': robot_snapshot(twin), 'commands': commands, 'physical_enabled': physical_enabled(), 'updated_at': twin.updated_at}, ensure_ascii=False, allow_nan=False).encode()
    return Response(twin.cached('automation', read), media_type='application/json')


@router.post('/api/automation/commands')
def command(body: CommandBody, request: Request):
    return propose(request.app.state.service, body.model_dump(), request.state.identity['username'])


@router.post('/api/automation/commands/{command_id}/approve')
def approve(command_id: str, body: Approval, request: Request):
    twin = request.app.state.service
    with twin._mutation() as db:
        expire_commands(db)
        row = db.get(RobotCommand, command_id)
        if not row: raise HTTPException(404, 'Команда не найдена')
        if row.status != 'proposed': raise HTTPException(409, 'Команда уже обработана или срок истёк')
        validate_command(twin, row.data, db)
        if db.scalar(select(RobotCommand.id).where(RobotCommand.robot_id == row.robot_id, RobotCommand.status.in_(['approved', 'dispatched', 'uncertain']))):
            raise HTTPException(409, 'У робота есть незавершённая команда. Сначала сверяйте состояние контроллера.')
        row.status = 'approved'
        row.data = {**row.data, 'approved_by': request.state.identity['username'], 'expires_at': time.time()+30}
        return public_command(row)


@router.post('/api/automation/commands/{command_id}/cancel')
def cancel(command_id: str, request: Request):
    with request.app.state.service._mutation() as db:
        row = db.get(RobotCommand, command_id)
        if not row: raise HTTPException(404, 'Команда не найдена')
        if row.status not in ('proposed', 'approved'): raise HTTPException(409, 'Выданную шлюзу команду отменить в очереди нельзя')
        row.status = 'cancelled'
        return public_command(row)


@router.post('/api/admin/gateways')
def create_gateway(body: GatewayCreate, request: Request):
    robot_snapshot(request.app.state.service, body.robot_id)
    raw = secrets.token_urlsafe(40)
    row = GatewayKey(id=hashlib.sha256(raw.encode()).hexdigest(), robot_id=body.robot_id, expires=time.time()+body.days*86400, active=True)
    with request.app.state.service._mutation() as db:
        db.add(row)
        audit(db, detail='Gateway enrolled: '+body.robot_id)
    return {'id': row.id, 'robot_id': row.robot_id, 'expires': row.expires, 'token': raw}


@router.get('/api/admin/gateways')
def gateways(request: Request):
    with request.app.state.service.sessions() as db:
        return [{'id': r.id, 'robot_id': r.robot_id, 'expires': r.expires, 'active': r.active} for r in db.scalars(select(GatewayKey).order_by(GatewayKey.expires.desc()).limit(1000))]


@router.delete('/api/admin/gateways/{key}')
def revoke_gateway(key: str, request: Request):
    with request.app.state.service._mutation() as db:
        row = db.get(GatewayKey, key)
        if not row: raise HTTPException(404, 'Ключ не найден')
        row.active = False
    return {'ok': True}


@router.post('/api/gateway/next')
def next_command(request: Request):
    identity = request.state.identity
    if identity['role'] != 'gateway': raise HTTPException(403, 'Нужен отдельный ключ шлюза')
    twin = request.app.state.service
    with twin.sessions() as db:
        if not db.scalar(select(RobotCommand.id).where(RobotCommand.robot_id == identity['robot_id'], RobotCommand.status == 'approved').limit(1)):
            return {'command': None}
    with twin._mutation() as db:
        expire_commands(db)
        row = db.scalar(select(RobotCommand).where(RobotCommand.robot_id == identity['robot_id'], RobotCommand.status == 'approved').order_by(RobotCommand.created_at))
        if not row: return {'command': None}
        validate_command(twin, row.data, db)
        lease = secrets.token_urlsafe(32)
        row.status = 'dispatched'
        row.data = {**row.data, 'lease_hash': hashlib.sha256(lease.encode()).hexdigest(), 'gateway_id': identity['id'], 'expires_at': time.time()+30}
        return {'command': {**public_command(row), 'lease': lease}}


@router.post('/api/gateway/result')
def command_result(body: CommandResult, request: Request):
    identity = request.state.identity
    if identity['role'] != 'gateway': raise HTTPException(403, 'Нужен ключ шлюза')
    with request.app.state.service._mutation() as db:
        row = db.get(RobotCommand, body.command_id)
        if not row or row.robot_id != identity['robot_id'] or row.data.get('gateway_id') != identity['id'] or row.data.get('lease_hash') != hashlib.sha256(body.lease.encode()).hexdigest():
            raise HTTPException(403, 'Команда не принадлежит этому шлюзу')
        if row.status not in ('dispatched', 'uncertain'):
            if row.status == body.result and row.data.get('result_detail') == body.detail: return public_command(row)
            raise HTTPException(409, 'Результат уже сохранён')
        row.status = body.result
        row.data = {**row.data, 'result_detail': body.detail, 'completed_at': time.time()}
        return public_command(row)


@router.post('/api/admin/commands/{command_id}/reconcile')
def reconcile(command_id: str, body: Reconciliation, request: Request):
    with request.app.state.service._mutation() as db:
        expire_commands(db)
        row = db.get(RobotCommand, command_id)
        if not row: raise HTTPException(404, 'Команда не найдена')
        if row.status != 'uncertain': raise HTTPException(409, 'Сверка нужна только при неизвестном результате')
        row.status = body.result
        row.data = {**row.data, 'result_detail': body.evidence, 'reconciled_by': request.state.identity['username'], 'completed_at': time.time()}
        audit(db, detail='Physical command reconciled '+command_id+': '+body.evidence)
        return public_command(row)
