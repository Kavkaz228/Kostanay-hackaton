"""Server-side revocable sessions; no client-trusted roles or plaintext credentials."""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from .database import User, LoginSession, AccessToken, Audit, GatewayKey

actor_context = ContextVar('actor', default=('system', 'background'))
COOKIE = 'allur_session'


def audit(session, action=None, detail=''):
    actor, operation = actor_context.get()
    if actor == 'system' and action is None:
        return
    session.add(Audit(timestamp=time.time(), actor=actor, action=action or operation, detail=detail[:500]))


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=131072, r=8, p=1, maxmem=256*1024*1024).hex()
    return f'scrypt${salt}${derived}'


def password_valid(password, stored):
    try:
        _, salt, _ = stored.split('$')
        return hmac.compare_digest(password_hash(password, salt), stored)
    except (ValueError, TypeError):
        return False


def check_password(password):
    if len(password) < 15 or len(password) > 128 or len(set(password)) < 6:
        raise HTTPException(422, 'Пароль: 15–128 символов, минимум 6 различных символов. Используйте уникальную парольную фразу.')


def public_user(user):
    return {k: getattr(user, k) for k in ('id', 'username', 'role', 'active', 'must_change')}


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(StrictBody):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class PasswordChange(StrictBody):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=15, max_length=128)


class NewUser(Login):
    role: str


class EditUser(StrictBody):
    role: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=15, max_length=128)


class NewToken(StrictBody):
    name: str = Field(min_length=1, max_length=80)
    days: int = Field(default=90, ge=1, le=365)


class Security:
    def __init__(self, sessions):
        self.sessions = sessions
        self.lock = threading.RLock()
        self.attempts = OrderedDict()
        self.session_cache = OrderedDict()
        self.cache_generation = 0
        # Bound simultaneous memory-hard password verification (128 MiB each).
        self.hash_slots = threading.BoundedSemaphore(2)
        self.secure = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
        with sessions.begin() as db:
            if db.scalar(select(User.id).limit(1)) is None:
                path = os.getenv('BOOTSTRAP_PASSWORD_FILE')
                password = Path(path).read_text().strip() if path else os.getenv('BOOTSTRAP_PASSWORD', '')
                if not password:
                    raise RuntimeError('First start requires BOOTSTRAP_PASSWORD_FILE. Run start.ps1.')
                check_password(password)
                db.add(User(id=str(uuid4()), username='admin', password=password_hash(password), role='admin', active=True, must_change=True))
                audit(db, 'bootstrap', 'Initial administrator created; password change required')
            db.execute(delete(LoginSession).where(LoginSession.expires < time.time()))

    def limited(self, key, maximum=8, window=60):
        with self.lock:
            now = time.monotonic()
            history = [t for t in self.attempts.pop(key, []) if now-t < window]
            self.attempts[key] = history + [now]
            while len(self.attempts) > 4096:
                self.attempts.popitem(last=False)
            if len(history) >= maximum:
                raise HTTPException(429, 'Слишком много попыток. Повторите позже.', headers={'Retry-After': str(window)})

    def authenticate(self, request):
        bearer = request.headers.get('authorization', '')
        with self.sessions() as db:
            if bearer.startswith('Bearer '):
                key = hashlib.sha256(bearer[7:].encode()).hexdigest()
                gateway = db.get(GatewayKey, key)
                if gateway and gateway.active and gateway.expires > time.time():
                    if request.method != 'POST' or request.url.path not in ('/api/gateway/next', '/api/gateway/result', '/api/robots/measurements'):
                        raise HTTPException(403, 'Ключ шлюза не разрешает этот запрос')
                    return {'username': 'gateway:'+gateway.robot_id, 'id': gateway.id, 'role': 'gateway', 'robot_id': gateway.robot_id, 'must_change': False}
                token = db.get(AccessToken, key)
                if not token or not token.active or token.expires <= time.time():
                    raise HTTPException(401, 'Ключ интеграции недействителен')
                if request.url.path not in ('/api/robots/measurements', '/api/scada/readings') or request.method != 'POST':
                    raise HTTPException(403, 'Ключ разрешает только приём измерений')
                return {'username': 'integration:' + token.name, 'id': token.id, 'role': 'integration', 'must_change': False}
        raw = request.cookies.get(COOKIE, '')
        key = hashlib.sha256(raw.encode()).hexdigest()
        now = time.time()
        with self.lock:
            cached = self.session_cache.get(key)
            generation = self.cache_generation
        if cached and cached[0] > now:
            result = cached[1]
        else:
            with self.sessions() as db:
                row = db.get(LoginSession, key) if raw else None
                user = db.get(User, row.user_id) if row and row.expires > now else None
                if not user or not user.active:
                    raise HTTPException(401, 'Войдите в систему')
                result = {**public_user(user), 'csrf': row.csrf, 'session_id': row.id}
                expiry = min(row.expires, now+20+secrets.randbelow(11))
            with self.lock:
                if generation != self.cache_generation:
                    return self.authenticate(request)
                self.session_cache[key] = (expiry, result)
                while len(self.session_cache) > 4096:
                    self.session_cache.popitem(last=False)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), result['csrf']):
                raise HTTPException(403, 'Сессия обновилась. Обновите страницу и повторите действие.')
            read_only_assistant = request.method == 'POST' and request.url.path == '/api/emulation/assistant'
            if result['role'] == 'viewer' and not request.url.path.startswith('/api/auth/') and not read_only_assistant:
                raise HTTPException(403, 'Роль наблюдателя не разрешает изменения')
        if request.url.path.startswith('/api/admin/') and result['role'] != 'admin':
            raise HTTPException(403, 'Требуется роль администратора')
        if result['must_change'] and request.url.path not in ('/api/auth/me', '/api/auth/password', '/api/auth/logout'):
            raise HTTPException(403, 'Сначала замените временный пароль')
        return result

    def invalidate(self):
        with self.lock:
            self.cache_generation += 1
            self.session_cache.clear()


router = APIRouter()


def security(request):
    return request.app.state.security


@router.post('/api/auth/login')
def login(body: Login, request: Request, response: Response):
    auth = security(request)
    username = body.username.strip().casefold()
    auth.limited('user:' + username)
    auth.limited('ip:' + (request.client.host if request.client else 'unknown'), 60)
    if not auth.hash_slots.acquire(blocking=False):
        raise HTTPException(429, 'Сервер проверяет другие входы. Повторите через несколько секунд.', headers={'Retry-After': '3'})
    try:
        with auth.sessions.begin() as db:
            user = db.scalar(select(User).where(User.username == username))
            # Same expensive calculation even for nonexistent users.
            valid = password_valid(body.password, user.password) if user else bool(password_hash(body.password)) and False
            if not valid or not user.active:
                audit(db, 'login_failed', 'Invalid credentials')
                failure = True
            else:
                failure = False
                raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                db.execute(delete(LoginSession).where(LoginSession.expires <= time.time()))
                # Bound per-account sessions, retaining at most ten devices.
                existing = db.scalars(select(LoginSession).where(LoginSession.user_id == user.id).order_by(LoginSession.expires.desc())).all()
                for row in existing[9:]:
                    db.delete(row)
                db.add(LoginSession(id=hashlib.sha256(raw.encode()).hexdigest(), user_id=user.id, csrf=csrf, expires=time.time()+8*3600))
                actor_context.set((user.username, 'login'))
                audit(db)
                result = {**public_user(user), 'csrf': csrf}
        if failure:
            raise HTTPException(401, 'Неверное имя пользователя или пароль')
        auth.invalidate()
        response.set_cookie(COOKIE, raw, max_age=8*3600, httponly=True, secure=auth.secure, samesite='strict', path='/')
        response.headers['Cache-Control'] = 'no-store'
        return result
    finally:
        auth.hash_slots.release()


@router.get('/api/auth/me')
def me(request: Request):
    return {k: v for k, v in request.state.identity.items() if k != 'session_id'}


@router.post('/api/auth/logout')
def logout(request: Request, response: Response):
    with security(request).sessions.begin() as db:
        db.execute(delete(LoginSession).where(LoginSession.id == request.state.identity['session_id']))
        audit(db)
    security(request).invalidate()
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


@router.post('/api/auth/password')
def change_password(body: PasswordChange, request: Request, response: Response):
    check_password(body.new_password)
    auth = security(request)
    auth.limited('password:' + request.state.identity['id'], 5)
    if not auth.hash_slots.acquire(blocking=False):
        raise HTTPException(429, 'Повторите через несколько секунд')
    try:
        with auth.lock, auth.sessions.begin() as db:
            user = db.get(User, request.state.identity['id'])
            if not password_valid(body.current_password, user.password):
                raise HTTPException(403, 'Текущий пароль неверен')
            if body.current_password == body.new_password:
                raise HTTPException(422, 'Новый пароль должен отличаться')
            user.password, user.must_change = password_hash(body.new_password), False
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
            audit(db, detail='All sessions revoked')
        auth.invalidate()
        response.delete_cookie(COOKIE, path='/')
        return {'ok': True, 'message': 'Пароль изменён. Войдите заново.'}
    finally:
        auth.hash_slots.release()


@router.get('/api/admin/users')
def users(request: Request):
    with security(request).sessions() as db:
        return [public_user(u) for u in db.scalars(select(User).order_by(User.username))]


@router.post('/api/admin/users')
def new_user(body: NewUser, request: Request):
    auth = security(request)
    username = body.username.strip().casefold()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.@-]{2,79}', username) or body.role not in ('admin', 'operator', 'viewer'):
        raise HTTPException(422, 'Логин: 3–80 латинских символов, цифр, _.@-; роль: admin/operator/viewer')
    check_password(body.password)
    auth.limited('create-user', 30)
    with auth.hash_slots, auth.lock, auth.sessions.begin() as db:
        if db.scalar(select(User.id).where(User.username == username)):
            raise HTTPException(409, 'Такой пользователь уже существует')
        user = User(id=str(uuid4()), username=username, password=password_hash(body.password), role=body.role, active=True, must_change=True)
        db.add(user)
        audit(db, detail=username + ':' + body.role)
        return public_user(user)


@router.patch('/api/admin/users/{user_id}')
def edit_user(user_id: str, body: EditUser, request: Request):
    auth = security(request)
    if user_id == request.state.identity['id']:
        raise HTTPException(409, 'Свою роль и доступ меняет другой администратор. Пароль меняется в профиле.')
    if body.role is not None and body.role not in ('admin', 'operator', 'viewer'):
        raise HTTPException(422, 'Неизвестная роль')
    if body.password:
        check_password(body.password)
    with auth.hash_slots, auth.lock, auth.sessions.begin() as db:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(404, 'Пользователь не найден')
        if body.role is not None:
            user.role = body.role
        if body.active is not None:
            user.active = body.active
        if body.password:
            user.password, user.must_change = password_hash(body.password), True
        db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
        audit(db, detail=user.username + ': sessions revoked')
        result = public_user(user)
    auth.invalidate()
    return result


@router.get('/api/admin/tokens')
def tokens(request: Request):
    with security(request).sessions() as db:
        return [{'id': t.id, 'name': t.name, 'expires': t.expires, 'active': t.active} for t in db.scalars(select(AccessToken))]


@router.post('/api/admin/tokens')
def new_token(body: NewToken, request: Request):
    raw = 'at_' + secrets.token_urlsafe(40)
    with security(request).sessions.begin() as db:
        token = AccessToken(id=hashlib.sha256(raw.encode()).hexdigest(), name=body.name, expires=time.time()+body.days*86400, active=True)
        db.add(token)
        audit(db, detail=body.name)
        return {'token': raw, 'id': token.id, 'expires': token.expires}


@router.delete('/api/admin/tokens/{token_id}')
def revoke_token(token_id: str, request: Request):
    with security(request).sessions.begin() as db:
        token = db.get(AccessToken, token_id)
        if not token:
            raise HTTPException(404, 'Ключ не найден')
        token.active = False
        audit(db, detail=token.name)
        return {'ok': True}


@router.get('/api/admin/audit')
def audit_log(request: Request, before: int = 2147483647):
    with security(request).sessions() as db:
        return [{k: getattr(r, k) for k in ('id', 'timestamp', 'actor', 'action', 'detail')} for r in db.scalars(select(Audit).where(Audit.id < before).order_by(Audit.id.desc()).limit(200))]
