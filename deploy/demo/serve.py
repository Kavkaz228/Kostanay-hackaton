"""One-container hosting of Allur twin for an online demo (Render, Hugging Face Spaces or any Docker host).

The container serves the built web interface and the API on $PORT (7860 on Hugging Face), the
same split that nginx performs in the regular installation: /api goes to the application, every
other path is a static file. Authentication is unchanged: sessions, CSRF tokens, the origin check,
roles and rate limits all work as in the regular installation.

Start-up provisioning: the account for the jury is created from deployment secrets
(JURY_USERNAME, default "jury"; JURY_PASSWORD, required, 15+ characters). The password is chosen
by the person who deploys the demo and stored as a hosting secret, so it is not a temporary
password and the account does not ask for a change at the first login.

The demo database lives in /tmp and starts fresh after every restart of the container.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

# Hugging Face publishes the public host name in SPACE_HOST; the API compares the Origin header of
# write requests with this public address.
if not os.getenv('PUBLIC_ORIGIN') and os.getenv('SPACE_HOST'):
    os.environ['PUBLIC_ORIGIN'] = 'https://' + os.environ['SPACE_HOST'].split(',')[0].strip()
# Render publishes the public address in RENDER_EXTERNAL_URL.
if not os.getenv('PUBLIC_ORIGIN') and os.getenv('RENDER_EXTERNAL_URL'):
    os.environ['PUBLIC_ORIGIN'] = os.environ['RENDER_EXTERNAL_URL'].rstrip('/')
os.environ.setdefault('COOKIE_SECURE', 'true' if os.getenv('PUBLIC_ORIGIN', '').startswith('https://') else 'false')
os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/allur-twin-demo.db')
# The online demo has no local AI server: robot monitoring keeps working on its rules only.
os.environ.setdefault('MONITORING_AI_ENABLED', 'false')

import uvicorn  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from app.database import User, make_database  # noqa: E402
from app.security import actor_context, check_password, password_hash  # noqa: E402

log = logging.getLogger('allur.hosting')
STATIC_DIR = Path(os.getenv('STATIC_DIR', '/app/static'))
# Same response headers as docker/security-headers.conf in the regular installation.
SECURITY_HEADERS = [
    (b'x-content-type-options', b'nosniff'),
    (b'referrer-policy', b'same-origin'),
    (b'x-frame-options', b'DENY'),
    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                 b"connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
]


def provision_jury_account() -> None:
    username = os.getenv('JURY_USERNAME', 'jury').strip().lower()
    password = os.getenv('JURY_PASSWORD', '')
    role = os.getenv('JURY_ROLE', 'operator')
    if role not in ('operator', 'viewer'):
        sys.exit('JURY_ROLE must be "operator" or "viewer".')
    if not password:
        log.warning('JURY_PASSWORD is not set: no jury account was created.')
        return
    try:
        check_password(password)
    except HTTPException as error:
        sys.exit(f'JURY_PASSWORD does not meet the password policy: {error.detail}')
    engine, sessions = make_database(os.environ['DATABASE_URL'])
    try:
        with sessions.begin() as db:
            user = db.scalar(select(User).where(User.username == username))
            if user is None:
                db.add(User(id=str(uuid4()), username=username, password=password_hash(password), role=role, active=True, must_change=False))
            else:
                user.password, user.role, user.active, user.must_change = password_hash(password), role, True, False
    finally:
        engine.dispose()
    log.info('Jury account "%s" (%s) is ready.', username, role)


class WebApp(StaticFiles):
    """Static web interface with the index page as fallback, like `try_files ... /index.html`."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code == 404 and not path.startswith('assets/'):
                return await super().get_response('index.html', scope)
            return PlainTextResponse(str(error.detail), status_code=error.status_code)


def build_application():
    from app.main import app as api
    web = WebApp(directory=STATIC_DIR, html=True)

    async def application(scope, receive, send):
        if scope['type'] == 'lifespan':
            await api(scope, receive, send)
            return
        path = scope.get('path', '')
        target = api if path == '/api' or path.startswith('/api/') else web

        async def send_secured(message):
            if message['type'] == 'http.response.start':
                message['headers'] = [*message.get('headers', []), *SECURITY_HEADERS]
            await send(message)

        await target(scope, receive, send_secured)

    return api, application


def start_emulation(service) -> None:
    """Run the SCADA stand clock (x60) through the same command handler the operator buttons use."""
    import json
    from types import SimpleNamespace
    from app.emulation import Command, command, current
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(service=service)))
    try:
        view = json.loads(current(service)['view'])
        if not view.get('paused'):
            return
        command(Command(request_id=uuid4().hex, expected_revision=view['revision'], action='speed', value=60), request)
        view = json.loads(current(service)['view'])
        command(Command(request_id=uuid4().hex, expected_revision=view['revision'], action='play'), request)
    except Exception:
        log.exception('Could not start the SCADA stand clock')


def keep_model_running(api) -> None:
    """Show a working line to every visitor: warm up one model hour, run at x30, start a new shift at the end."""
    if os.getenv('AUTOPLAY', 'true').lower() != 'true':
        return
    for _ in range(600):
        service = getattr(api.state, 'service', None)
        if service is not None:
            break
        time.sleep(1)
    else:
        return
    actor_context.set(('demo-autoplay', 'background'))
    try:
        state = service.state()
        if state['source'] == 'simulation' and not state['running'] and state['sim_time'] < 600:
            service.advance(3600)
            service.control({'running': True, 'speed': 30})
    except Exception:
        log.exception('Could not start the demo model')
    start_emulation(service)
    while True:
        time.sleep(30)
        try:
            state = service.state()
            if state['source'] == 'simulation' and state['sim_time'] >= state['shift_duration']:
                service.reset()
                service.control({'running': True, 'speed': 30})
        except Exception:
            log.exception('Demo model check failed')


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    if not (STATIC_DIR / 'index.html').exists():
        sys.exit(f'Web interface not found in {STATIC_DIR}.')
    provision_jury_account()
    api, application = build_application()
    threading.Thread(target=keep_model_running, args=(api,), name='demo-autoplay', daemon=True).start()
    uvicorn.run(application, host='0.0.0.0', port=int(os.getenv('PORT', '7860')), proxy_headers=True, forwarded_allow_ips='*')


if __name__ == '__main__':
    main()
