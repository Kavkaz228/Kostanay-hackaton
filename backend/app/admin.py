"""Recovery CLI for server administrators with Docker access; password read from stdin."""
import getpass
import sys
from pathlib import Path
import os
from sqlalchemy import select, delete
from sqlalchemy.engine import URL
from .database import make_database, User, LoginSession
from .security import password_hash, check_password, audit, actor_context


def main():
    if len(sys.argv) == 3 and sys.argv[1] == 'import-case':
        from .service import Service
        from .manufacturing import parse_case
        path = Path(sys.argv[2])
        content = path.read_bytes()
        parse_case(content)  # Validate before taking ownership of the database.
        url = URL.create('postgresql+psycopg', username='allur', password=Path(os.environ['DATABASE_PASSWORD_FILE']).read_text().strip(), host='db', database='allur').render_as_string(hide_password=False)
        actor_context.set(('server-administrator', 'import-case'))
        service = Service(url)
        try:
            result = service.import_case(content, path.name)
            print(result['message'], 'Rows:', result['imported'])
        finally:
            service.close()
        return
    if len(sys.argv) != 3 or sys.argv[1] != 'reset-password':
        raise SystemExit('Usage: python -m app.admin reset-password USERNAME (password on stdin) | import-case FILE (stop API first)')
    username = sys.argv[2].casefold()
    password = getpass.getpass('New temporary password: ') if sys.stdin.isatty() else sys.stdin.readline().rstrip('\r\n')
    check_password(password)
    url = URL.create('postgresql+psycopg', username='allur', password=Path(os.environ['DATABASE_PASSWORD_FILE']).read_text().strip(), host='db', database='allur').render_as_string(hide_password=False)
    engine, sessions = make_database(url)
    try:
        with sessions.begin() as db:
            user = db.scalar(select(User).where(User.username == username))
            if not user:
                raise SystemExit('User not found')
            user.password, user.must_change, user.active = password_hash(password), True, True
            db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
            actor_context.set(('server-administrator', 'password-recovery'))
            audit(db, detail=username)
        print('Temporary password set; sessions revoked; change required on next login.')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
