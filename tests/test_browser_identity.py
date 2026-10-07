import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.browser import BrowserStore, USER_COOKIE
from app.config import Settings
from app.main import create_app
from app.attachments.store import scope_key

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def browser_app(tmp_path):
    app = create_app(Settings(llm_backend='mock', chroma_mode='disabled', redis_url='',
        embedding_backend='deterministic', agentic_enabled=False, network_enabled=False,
        data_dir=str(tmp_path), knowledge_root=str(tmp_path/'empty'), evaluation_dir=str(tmp_path/'evals')))
    async with app.router.lifespan_context(app):
        yield app


async def test_cookie_identity_persists_and_is_not_research_cookie(browser_app):
    async with AsyncClient(transport=ASGITransport(app=browser_app), base_url='http://test') as client:
        first = await client.get('/api/v1/browser')
        cookie = first.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Max-Age=7776000' in cookie
        token = client.cookies.get(USER_COOKIE)
        assert token != first.json()['user_id']
        assert (await client.get('/api/v1/browser')).json() == first.json()
        fresh_store = BrowserStore(browser_app.state.services.settings.data_dir)
        assert fresh_store.resolve(token) == first.json()['user_id']
        client.cookies.set('hd_research', 'prefs')
        cleared = await client.delete('/api/v1/browser')
        assert cleared.json()['research_cookie_unchanged'] is True
        assert not cleared.json()['server_history_deleted']
        assert client.cookies.get('hd_research') == 'prefs'
        assert not fresh_store.resolve(token)
        assert (await client.get('/api/v1/browser')).json()['user_id'] != first.json()['user_id']


async def test_browser_history_isolation_redaction_and_chat_scope(browser_app):
    transport = ASGITransport(app=browser_app)
    async with AsyncClient(transport=transport, base_url='http://test') as a, AsyncClient(transport=transport, base_url='http://test') as b:
        owner = (await a.get('/api/v1/browser')).json()['user_id']
        await b.get('/api/v1/browser')
        session = (await a.post('/api/v1/browser/sessions', json={})).json()['id']
        answer = await a.post('/api/v1/chat', json={'session_id':session, 'message':'我叫张三，电话13812345678，房颤检查有哪些？', 'user_id':'spoofed', 'network_mode':'off'})
        assert answer.status_code == 200
        history = (await a.get('/api/v1/browser/sessions/'+session)).json()
        assert len(history['turns']) == 1
        assert '13812345678' not in json.dumps(history, ensure_ascii=False)
        assert '张三' not in json.dumps(history, ensure_ascii=False)
        assert '房颤' in history['title']
        assert (await b.get('/api/v1/browser/sessions')).json() == []
        assert (await b.get('/api/v1/browser/sessions/'+session)).status_code == 404
        assert (await b.post('/api/v1/chat', json={'session_id':session, 'user_id':owner, 'message':'继续'})).status_code == 404
        trace = answer.json()['trace_id']
        assert (await a.get('/api/v1/traces/'+trace)).status_code == 200
        assert (await b.get('/api/v1/traces/'+trace)).status_code == 404
        a.cookies.clear()
        assert (await a.post('/api/v1/chat', json={'session_id':session,'user_id':owner,'message':'继续'})).status_code == 401
        assert (await a.get('/api/v1/traces/'+trace)).status_code == 404


async def test_reset_revokes_old_cookie_and_preserves_stored_records(browser_app):
    async with AsyncClient(transport=ASGITransport(app=browser_app), base_url='http://test') as client:
        owner = (await client.get('/api/v1/browser')).json()['user_id']
        token = client.cookies.get(USER_COOKIE)
        sid = (await client.post('/api/v1/browser/sessions', json={})).json()['id']
        await client.post('/api/v1/chat', json={'session_id':sid,'message':'胸痛，大汗','network_mode':'off'})
        await client.delete('/api/v1/browser')
        await client.get('/api/v1/browser')
        assert (await client.get('/api/v1/browser/sessions')).json() == []
        store = browser_app.state.services.browser
        assert len(store.history(owner,sid)['turns']) == 1
        store.append(owner,sid,'late result',{'answer':'not saved'})
        assert len(store.history(owner,sid)['turns']) == 1
        client.cookies.clear();client.cookies.set(USER_COOKIE,token)
        assert (await client.get('/api/v1/browser/sessions')).status_code == 401


async def test_server_trace_uuid_survives_hospital_number_redaction(browser_app):
    store = browser_app.state.services.browser
    _, owner = store.issue()
    session = store.create_session(owner,None)['id']
    trace = '4cbab917-6f3f-458b-baa7-afe581966caf'
    store.append(owner,session,'电话13812345678',{'trace_id':trace,'answer':'住院号 BA123456'})
    saved = store.history(owner,session)['turns'][0]
    assert saved['response']['trace_id'] == trace and store.owns_trace(owner,trace)
    assert '13812345678' not in saved['question'] and 'BA123456' not in saved['response']['answer']


async def test_cookie_origin_and_missing_cookie_guards(browser_app):
    async with AsyncClient(transport=ASGITransport(app=browser_app), base_url='http://test') as client:
        assert (await client.get('/api/v1/browser/sessions')).status_code == 401
        assert (await client.post('/api/v1/chat',headers={'X-Browser-Client':'1'},json={'message':'hello'})).status_code == 401
        await client.get('/api/v1/browser')
        assert (await client.delete('/api/v1/browser',headers={'Origin':'https://evil.invalid'})).status_code == 403
        assert (await client.post('/api/v1/browser/sessions',json={},headers={'Origin':'https://evil.invalid'})).status_code == 403


async def test_browser_attachment_case_scope_and_no_attachment_history(browser_app):
    async with AsyncClient(transport=ASGITransport(app=browser_app), base_url='http://test') as client:
        owner = (await client.get('/api/v1/browser')).json()['user_id']
        sid = (await client.post('/api/v1/browser/sessions',json={})).json()['id']
        assert (await client.get('/api/v1/attachments/unknown',params={'session_id':sid,'case_id':'other'})).status_code == 403
        assert (await client.post('/api/v1/chat',json={'session_id':sid,'case_id':'other','message':'test'})).status_code == 403
        store = browser_app.state.services.browser
        store.append(owner,sid,'report raw',{'answer':'private OCR text','citations':[{'snippet':'report'}]},temporary=True)
        history = json.dumps(store.history(owner,sid),ensure_ascii=False)
        assert 'private OCR text' not in history and 'report raw' not in history
        assert '不持久保存' in history


async def test_emergency_browser_path_still_zero_model_calls(browser_app):
    async with AsyncClient(transport=ASGITransport(app=browser_app),base_url='http://test') as client:
        await client.get('/api/v1/browser')
        data = (await client.post('/api/v1/chat',json={'message':'胸痛，大汗','attachment_ids':['pending'],'network_mode':'off'})).json()
        assert data['is_emergency'] and data['model_calls'] == 0
        assert not data['tool_summary'] and '120' in data['answer']


async def test_attachment_cookie_owner_is_injected_and_other_browser_rejected(browser_app):
    transport = ASGITransport(app=browser_app)
    async with AsyncClient(transport=transport,base_url='https://test') as a, AsyncClient(transport=transport,base_url='https://test') as b:
        response = await a.get('/api/v1/browser')
        assert 'Secure' in response.headers['set-cookie']
        owner = response.json()['user_id']
        await b.get('/api/v1/browser')
        sid = (await a.post('/api/v1/browser/sessions',json={})).json()['id']
        item = browser_app.state.services.attachments.create(b'printed report','report.txt',scope_key(sid,None,owner),'patient_context')
        path = '/api/v1/attachments/'+item['id']
        assert (await a.get(path,params={'session_id':sid,'user_id':'ignored-spoof'})).status_code == 200
        assert (await b.get(path,params={'session_id':sid,'user_id':owner})).status_code == 404
        a.cookies.clear()
        assert (await a.get(path,params={'session_id':sid,'user_id':owner})).status_code == 401
