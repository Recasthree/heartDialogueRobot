"""Anonymous browser identity and redacted chat history, separate from research UI preferences.

This is local browser isolation, not account authentication. The cookie is an opaque
capability; only its hash is persisted. Research preferences never grant API access.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .privacy import redact_payload, redact_text

USER_COOKIE = "hd_user"
COOKIE_TTL = 90 * 86400


class BrowserStore:
    def __init__(self, root):
        self.path = Path(root) / "browser-history.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS identities(token_hash TEXT PRIMARY KEY, user_id TEXT UNIQUE, expires REAL);
              CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, owner TEXT, title TEXT, case_id TEXT,
                created REAL, updated REAL);
              CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner,updated);
              CREATE TABLE IF NOT EXISTS turns(id INTEGER PRIMARY KEY, session TEXT, question TEXT,
                response TEXT, created REAL, trace_id TEXT);
              CREATE INDEX IF NOT EXISTS turns_session ON turns(session,id);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def resolve(self, token):
        if not token or len(token) > 128:
            return None
        with self.connection() as db:
            row = db.execute('SELECT user_id FROM identities WHERE token_hash=? AND expires>?',
                             (self.digest(token), time.time())).fetchone()
        return row['user_id'] if row else None

    def issue(self):
        token, user = secrets.token_urlsafe(32), 'browser-' + uuid4().hex
        with self.connection() as db:
            db.execute('INSERT INTO identities VALUES(?,?,?)', (self.digest(token), user, time.time()+COOKIE_TTL))
        return token, user

    def revoke(self, token):
        with self.connection() as db:
            db.execute('DELETE FROM identities WHERE token_hash=?', (self.digest(token),))

    def create_session(self, owner, case_id=None):
        now = time.time()
        row = dict(id='web-' + uuid4().hex, owner=owner, title='新对话', case_id=case_id, created=now, updated=now)
        with self.connection() as db:
            db.execute('INSERT INTO sessions VALUES(:id,:owner,:title,:case_id,:created,:updated)', row)
        return {k: v for k, v in row.items() if k != 'owner'}

    def sessions(self, owner):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT id,title,case_id,created,updated FROM sessions WHERE owner=? ORDER BY updated DESC LIMIT 200', (owner,))]

    def session(self, owner, session):
        with self.connection() as db:
            row = db.execute('SELECT id,title,case_id,created,updated FROM sessions WHERE owner=? AND id=?', (owner, session)).fetchone()
        if not row:
            raise HTTPException(404, 'browser_session_not_found')
        return dict(row)

    def history(self, owner, session):
        result = self.session(owner, session)
        with self.connection() as db:
            # Bound the UI history per session. Stored earlier turns are not deleted.
            rows = db.execute('SELECT question,response,created FROM turns WHERE session=? ORDER BY id DESC LIMIT 100', (session,)).fetchall()
        result['turns'] = [dict(question=r['question'], response=json.loads(r['response']), created=r['created']) for r in reversed(rows)]
        return result

    def append(self, owner, session, question, response, temporary=False):
        now = time.time()
        question = redact_text(question)[0]
        # This server-issued UUID is an ownership key, not patient prose. A
        # random "-ba..." segment must not be mistaken for a hospital number.
        trace_id = response.get('trace_id')
        if temporary:
            question = '使用临时附件的问题（不持久保存正文）'
            response = {k: response.get(k) for k in ['session_id', 'trace_id', 'is_emergency']}
            response.update(answer='该轮使用了临时附件，回答正文不持久保存。', temporary=True,
                            citations=[], tool_summary=[], degradation_reasons=[])
        else:
            response = redact_payload(response)
        if isinstance(trace_id,str) and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',trace_id):
            response['trace_id'] = trace_id
        with self.connection() as db:
            # A reset concurrent with a running chat cannot resurrect the old identity.
            if not db.execute('SELECT 1 FROM identities WHERE user_id=? AND expires>?', (owner, now)).fetchone():
                return
            self.session(owner, session)
            db.execute('INSERT INTO turns(session,question,response,created,trace_id) VALUES(?,?,?,?,?)',
                       (session, question, json.dumps(response, ensure_ascii=False), now, response.get('trace_id')))
            db.execute("UPDATE sessions SET title=CASE WHEN title='新对话' THEN ? ELSE title END,updated=? WHERE id=? AND owner=?",
                       (question[:46], now, session, owner))

    def owns_trace(self, owner, trace):
        with self.connection() as db:
            return bool(db.execute('SELECT 1 FROM turns t JOIN sessions s ON t.session=s.id WHERE s.owner=? AND t.trace_id=?', (owner, trace)).fetchone())

    def is_browser_trace(self, trace):
        with self.connection() as db:
            return bool(db.execute('SELECT 1 FROM turns WHERE trace_id=?', (trace,)).fetchone())


def same_origin(request):
    origin = request.headers.get('origin')
    if (origin and origin.rstrip('/') != str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'same_origin_required')


def browser_user(request, required=False):
    token = request.cookies.get(USER_COOKIE)
    if token:
        user = request.app.state.services.browser.resolve(token)
        if not user:
            raise HTTPException(401, 'browser_identity_expired')
        if request.method not in {'GET', 'HEAD'}:
            same_origin(request)
        return user
    if required or request.headers.get('x-browser-client') == '1':
        raise HTTPException(401, 'browser_cookie_required')
    return None


class NewSession(BaseModel):
    case_id: str | None = Field(default=None, max_length=200)


def browser_router():
    router = APIRouter(prefix='/api/v1/browser')

    @router.get('')
    async def bootstrap(request: Request, response: Response):
        store = request.app.state.services.browser
        user = store.resolve(request.cookies.get(USER_COOKIE))
        if not user:
            token, user = store.issue()
            response.set_cookie(USER_COOKIE, token, max_age=COOKIE_TTL, httponly=True,
                                secure=request.url.scheme == 'https', samesite='strict', path='/')
        response.headers['Cache-Control'] = 'no-store'
        return {'user_id': user, 'label': '访客 ' + user[-6:], 'cookie_ttl_days': 90}

    @router.delete('')
    async def reset(request: Request, response: Response):
        same_origin(request)
        token = request.cookies.get(USER_COOKIE)
        if token:
            request.app.state.services.browser.revoke(token)
        response.delete_cookie(USER_COOKIE, path='/', httponly=True, samesite='strict', secure=request.url.scheme == 'https')
        response.headers['Cache-Control'] = 'no-store'
        return {'cleared': True, 'research_cookie_unchanged': True, 'server_history_deleted': False}

    @router.get('/sessions')
    async def sessions(request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return request.app.state.services.browser.sessions(browser_user(request, True))

    @router.post('/sessions', status_code=201)
    async def create(body: NewSession, request: Request):
        user = browser_user(request, True)
        s = request.app.state.services
        if body.case_id and not s.cases.get(body.case_id):
            raise HTTPException(404, 'case_not_found')
        return s.browser.create_session(user, body.case_id)

    @router.get('/sessions/{session}')
    async def history(session: str, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return request.app.state.services.browser.history(browser_user(request, True), session)

    return router
