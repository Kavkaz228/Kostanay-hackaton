from __future__ import annotations

import asyncio
import json
import logging
import os
import hashlib
import time
import threading
from pathlib import Path
from uuid import uuid4
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse, HTMLResponse

from .schemas import Advance, Control, Reset, ScenarioRequest, Source, StationChange, RobotConfig, MeasurementBatch
from .service import Service
from .version import APP_NAME, APP_VERSION
from .telemetry import ImportValidationError, template
from .robots import HEADER as ROBOT_HEADER
from .robots import FIELDS as ROBOT_FIELDS
from .manufacturing import QualityBatch, QUALITY_FIELDS
from .automation import router as automation_router
from .local_ai import router as ai_router
from .scada import router as scada_router
from .emulation import router as emulation_router
from .security import Security, router as security_router, actor_context
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import URL

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')


def create_app(database_url=None, runner_enabled=None):
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///./allur.db")
    if database_url is None and os.getenv('DATABASE_PASSWORD_FILE'):
        url = URL.create('postgresql+psycopg', username='allur', password=Path(os.environ['DATABASE_PASSWORD_FILE']).read_text().strip(), host='db', database='allur').render_as_string(hide_password=False)
    run = runner_enabled if runner_enabled is not None else os.getenv("RUNNER_ENABLED", "true").lower() == "true"

    @asynccontextmanager
    async def lifespan(application):
        application.state.service = Service(url)
        try:
            application.state.security = Security(application.state.service.sessions)
            if run:
                application.state.service.start()
            yield
        finally:
            application.state.service.close()

    application = FastAPI(title=APP_NAME, description="Модель производства и импорт измерений", version=APP_VERSION, lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    application.include_router(security_router)
    application.include_router(automation_router)
    application.include_router(ai_router)
    application.include_router(scada_router)
    application.include_router(emulation_router)
    heavy_slots = threading.BoundedSemaphore(4)
    detail_slots = asyncio.Semaphore(4)

    @application.middleware('http')
    async def access_control(request, call_next):
        started = time.monotonic()
        request_id = str(uuid4())
        context = actor_context.set(('anonymous', request.method + ' ' + request.url.path[:200]))
        acquired = False
        try:
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                origin = request.headers.get('origin')
                expected = os.getenv('PUBLIC_ORIGIN') or str(request.base_url).rstrip('/')
                if origin and origin.rstrip('/') != expected:
                    raise HTTPException(403, 'Источник запроса не разрешён')
            public = (request.url.path == '/api/health' and request.method == 'GET') or (request.url.path == '/api/auth/login' and request.method == 'POST')
            if not public:
                identity = await asyncio.to_thread(application.state.security.authenticate, request)
                request.state.identity = identity
                actor_context.set((identity['username'], request.method + ' ' + request.url.path[:200]))
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.url.path != '/api/auth/login':
                application.state.security.limited('write:' + request.state.identity['id'], 600)
            expensive = request.url.path in ('/api/import', '/api/import/preview', '/api/scenarios', '/api/robots/export', '/api/robots/analytics')
            if expensive and (request.method != 'GET' or request.url.path.startswith('/api/robots/')):
                acquired = heavy_slots.acquire(blocking=False)
                if not acquired:
                    raise HTTPException(429, 'Сервер обрабатывает другие расчёты. Повторите запрос.', headers={'Retry-After': '3'})
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=exc.headers)
        except Exception:
            logging.exception('Request failed request_id=%s', request_id)
            response = JSONResponse({'detail': 'Внутренняя ошибка. Сообщите администратору номер запроса.', 'request_id': request_id}, status_code=500)
        finally:
            if acquired:
                heavy_slots.release()
            actor_context.reset(context)
        response.headers['X-Request-ID'] = request_id
        response.headers['Cache-Control'] = 'no-store'
        logging.getLogger('allur.requests').info(json.dumps({'request_id': request_id, 'method': request.method, 'path': request.url.path[:200], 'status': response.status_code, 'duration_ms': round((time.monotonic()-started)*1000)}))
        return response

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        logging.exception('Database operation failed')
        return JSONResponse(status_code=503, content={'detail': 'Хранилище временно недоступно. Повторите запрос с тем же Idempotency-Key.'}, headers={'Retry-After': '5'})

    @application.exception_handler(ImportValidationError)
    async def import_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": exc.errors})

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Avoid echoing malformed NaN/Infinity or unserializable validation context.
        return JSONResponse(status_code=422, content={"detail": [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]})

    def service(request):
        return request.app.state.service

    @application.get('/api/docs', response_class=HTMLResponse, include_in_schema=False)
    def api_reference():
        import html
        paths = application.openapi()['paths']
        rows = ''.join('<tr><td>' + html.escape(method.upper()) + '</td><td>' + html.escape(path) + '</td><td>' + html.escape(info.get('summary', '')) + '</td></tr>' for path, methods in paths.items() for method, info in methods.items())
        return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Allur twin 2.0 API</title><body style="font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px"><h1>Allur twin 2.0 API</h1><p>Для интеграции создайте ключ в «Учётная запись → Ключи интеграций». Передавайте Authorization: Bearer &lt;ключ&gt; и Idempotency-Key: &lt;уникальный ID пакета&gt; в POST /api/robots/measurements. Тело: {"measurements": [...]}, до 1000 измерений. Повтор пакета с тем же ключом возвращает прежнюю квитанцию. Для действий пользователя нужны cookie сессии и X-CSRF-Token из /api/auth/me.</p><p><a href="/api/openapi.json">Полная спецификация OpenAPI: поля, схемы, ограничения</a></p><table cellpadding="8"><tr><th>Метод</th><th>Путь</th><th>Операция</th></tr>' + rows + '</table></body></html>'

    @application.get("/api/health")
    def health(request: Request):
        try:
            service(request).healthy()
        except Exception:
            logging.exception("Database health check failed")
            raise HTTPException(503, "База данных недоступна")
        return {"status": "ok", "database": "ok"}

    @application.get("/api/state")
    def state(request: Request):
        return service(request).state()

    @application.get('/api/dashboard')
    def dashboard(request: Request):
        return Response(service(request).dashboard(), media_type='application/json')

    @application.get('/api/admin/system')
    def system_status(request: Request):
        from sqlalchemy import func, select, text
        from .database import User, LoginSession, Observation
        current = service(request)
        with current.sessions() as db:
            database_bytes = db.scalar(text('SELECT pg_database_size(current_database())')) if current.database.dialect.name == 'postgresql' else None
            return {'version': APP_VERSION, 'storage_version': 2, 'users': db.scalar(select(func.count()).select_from(User)),
                'active_sessions': db.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.expires > time.time())),
                'robot_measurements': current.robot_count, 'database_bytes': database_bytes,
                'worker_alive': bool(current.runner_thread and current.runner_thread.is_alive()), 'last_worker_error': current.runner_error,
                'deployment': 'single-owner', 'secure_cookie': request.app.state.security.secure}

    @application.get("/api/history")
    def history(request: Request, limit: int = Query(120, ge=1, le=2000), run_id: str | None = None):
        return service(request).history(limit, run_id)

    @application.get("/api/incidents")
    def incidents(request: Request, include_archived: bool = False):
        return service(request).incidents(include_archived)

    @application.get('/api/incidents/export')
    def incident_export(request: Request):
        return Response(service(request).export_incidents().encode('utf-8-sig'), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="allur-incidents.csv"'})

    @application.post("/api/incidents/{incident_id}/acknowledge")
    def acknowledge(incident_id: str, request: Request):
        return service(request).acknowledge(incident_id)

    @application.post("/api/control")
    def control(body: Control, request: Request):
        return service(request).control(body.model_dump(exclude_none=True))

    @application.post("/api/advance")
    def advance(body: Advance, request: Request):
        return service(request).advance(body.seconds)

    @application.patch("/api/stations/{station_id}")
    def station(station_id: str, body: StationChange, request: Request):
        return service(request).configure(station_id, body.model_dump(exclude_none=True))

    @application.post("/api/reset")
    def reset(body: Reset, request: Request):
        return service(request).reset()

    @application.post("/api/source")
    def source(body: Source, request: Request):
        return service(request).switch_source(body.source, body.telemetry_kind)

    @application.post("/api/scenarios")
    def scenario(body: ScenarioRequest, request: Request):
        return service(request).scenario(body.name, body.horizon_minutes, [change.model_dump(exclude_none=True) for change in body.changes])

    @application.get("/api/scenarios")
    def scenarios(request: Request):
        return service(request).scenarios()

    @application.get("/api/export")
    def export(request: Request):
        return Response(service(request).export().encode("utf-8-sig"), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="allur-history.csv"'})

    @application.get('/api/robots/history')
    def robot_history(request: Request, robot_id: str = Query(..., min_length=1, max_length=128), limit: int = Query(2000, ge=1, le=20000), start: str | None = None, end: str | None = None, offset: int = Query(0, ge=0, le=2147483647)):
        return service(request).robot_history(robot_id, limit, start, end, offset)

    @application.get('/api/robots/config')
    def robot_config(request: Request, robot_id: str = Query(..., min_length=1, max_length=128)):
        return service(request).robot_config(robot_id)

    @application.put('/api/robots/config')
    def configure_robot(body: RobotConfig, request: Request, robot_id: str = Query(..., min_length=1, max_length=128)):
        return service(request).configure_robot(robot_id, body.model_dump())

    @application.get('/api/robots/analytics')
    def robot_analytics(request: Request, robot_id: str, start: str | None = None, end: str | None = None):
        return service(request).robot_analytics(robot_id, start, end)

    @application.get('/api/robots/detail')
    async def robot_detail(request: Request, robot_id: str = Query(..., min_length=1, max_length=128), start: str | None = None, end: str | None = None, offset: int = Query(0, ge=0, le=2147483647)):
        twin = service(request)
        key = ('detail', robot_id, start, end, offset)
        cached = twin.read_cache.get(key)
        if cached and time.monotonic() - cached[0] < 5:
            return Response(cached[1], media_type='application/json')
        # Bound memory for large analysis windows without occupying the HTTP
        # worker pool while waiting. Already computed views bypass the queue.
        async with detail_slots:
            cached = twin.read_cache.get(key)
            if cached and time.monotonic() - cached[0] < 5:
                value = cached[1]
            else:
                value = await asyncio.to_thread(twin.robot_detail, robot_id, start, end, offset)
        return Response(value, media_type='application/json')

    @application.post('/api/robots/measurements')
    def ingest_measurements(body: MeasurementBatch, request: Request):
        key = request.headers.get('idempotency-key')
        if key is not None and (not 8 <= len(key) <= 128 or not key.isascii()):
            raise HTTPException(422, 'Idempotency-Key: 8–128 ASCII символов')
        if request.state.identity['role'] in ('integration', 'gateway') and not key:
            raise HTTPException(422, 'Интеграция должна передать уникальный Idempotency-Key для каждого пакета')
        if request.state.identity['role'] == 'gateway' and any(r.robot_id != request.state.identity['robot_id'] for r in body.measurements):
            raise HTTPException(403, 'Шлюз может отправлять показания только своего робота')
        receipt = hashlib.sha256((request.state.identity['id'] + ':' + key).encode()).hexdigest() if key else None
        return service(request).ingest_measurements([row.model_dump() for row in body.measurements], receipt)

    @application.get('/api/imports')
    def imports(request: Request):
        return service(request).imports()

    @application.get('/api/robots/export')
    def robot_export(request: Request, robot_id: str | None = None, start: str | None = None, end: str | None = None):
        return StreamingResponse(service(request).stream_robots(robot_id, start, end), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="allur-robots.csv"'})

    @application.get('/api/import/robots-template')
    def robot_template():
        return Response((','.join(ROBOT_FIELDS) + '\r\n').encode('utf-8-sig'), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="allur-robots-template.csv"'})

    @application.get('/api/manufacturing')
    def manufacturing(request: Request, search: str = Query('', max_length=100), offset: int = Query(0, ge=0, le=2147483647)):
        return service(request).manufacturing(search, offset)

    @application.post('/api/quality/records')
    def quality_records(body: QualityBatch, request: Request):
        return service(request).quality_records([r.model_dump() for r in body.records])

    @application.get('/api/quality/template')
    def quality_template():
        return Response((','.join(QUALITY_FIELDS)+'\r\n').encode('utf-8-sig'), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="allur-quality-template.csv"'})

    @application.get('/api/quality/export')
    def quality_export(request: Request):
        return StreamingResponse(service(request).quality_export(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="allur-quality.csv"'})

    @application.get("/api/import/template")
    def import_template(request: Request):
        with service(request).lock:
            content = template(service(request).engine)
        return Response(content.encode("utf-8-sig"), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="allur-telemetry-template.csv"'})

    @application.post("/api/import")
    async def import_csv(request: Request, file: UploadFile = File(...)):
        content = await file.read(5 * 1024 * 1024 + 1)
        await file.close()
        return await asyncio.to_thread(service(request).import_csv, content, file.filename or 'CSV')

    @application.post('/api/import/preview')
    async def preview_csv(request: Request, file: UploadFile = File(...)):
        content = await file.read(5 * 1024 * 1024 + 1)
        await file.close()
        return await asyncio.to_thread(service(request).preview_csv, content)

    @application.get("/api/events")
    async def events(request: Request):
        async def stream():
            while not await request.is_disconnected():
                try:
                    await asyncio.to_thread(application.state.security.authenticate, request)
                except HTTPException:
                    break
                state = await asyncio.to_thread(service(request).state)
                yield "event: state\ndata: " + json.dumps(state, ensure_ascii=False, allow_nan=False) + "\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return application


app = create_app()

