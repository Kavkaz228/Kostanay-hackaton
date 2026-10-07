"""Test fixture creates a real database session, never a runtime authentication bypass."""
import hashlib
import os
import secrets
import time
from fastapi.testclient import TestClient as BaseClient
from sqlalchemy import select
from app.database import LoginSession, User

os.environ.setdefault('BOOTSTRAP_PASSWORD', 'Test bootstrap phrase 2026!')


class TestClient(BaseClient):
    __test__ = False

    def __enter__(self):
        super().__enter__()
        with self.app.state.service.sessions.begin() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            user.must_change = False
            raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.add(LoginSession(id=hashlib.sha256(raw.encode()).hexdigest(), user_id=user.id, csrf=csrf, expires=time.time()+3600))
        self.cookies.set('allur_session', raw)
        self.headers['X-CSRF-Token'] = csrf
        return self
