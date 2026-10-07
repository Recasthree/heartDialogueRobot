import asyncio
import io
import json
from pathlib import Path
import socket
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
from PIL import Image, ImageDraw, ImageFont

from app.attachments.store import AttachmentStore, AttachmentPatch, AttachmentError, scope_key
from app.attachments.worker import parse
from app.config import Settings, ROOT
from app.main import create_app
from app.models import EvidenceItem
from app.tools.contracts import ToolArgs, ToolContext, ToolPlan
from app.tools.gateway import ToolGateway
from app.tools.network import BlockedURL, NetworkEvidence, PublicNetworkBackend, valid_url

pytestmark = pytest.mark.asyncio


async def test_medication_name_without_numbers_still_requires_confirmation(tmp_path):
    store = AttachmentStore(tmp_path, tmp_path)
    item = store.create('阿司匹林'.encode(), 'medication.txt', 'scope')
    await store.parse(item['id'], 'scope')
    with pytest.raises(AttachmentError, match='confirmation_required'):
        store.context([item['id']], 'scope')


async def test_delete_cancels_actual_mcp_parser_worker(running_app, monkeypatch):
    app, client = running_app
    started = asyncio.Event()
    class SlowWorker:
        returncode = None
        killed = False
        async def communicate(self):
            started.set()
            await asyncio.Event().wait()
        def kill(self):
            self.killed = True
            self.returncode = -1
        async def wait(self):
            return self.returncode
    worker = SlowWorker()
    monkeypatch.setattr('app.attachments.store.asyncio.create_subprocess_exec', AsyncMock(return_value=worker))
    response = await client.post('/api/v1/attachments', files={'file': ('slow.txt', b'research')}, data={'session_id':'cancel'})
    aid = response.json()['id']
    await asyncio.wait_for(started.wait(), 10)
    deleted = await client.delete('/api/v1/attachments/'+aid, params={'session_id':'cancel'})
    assert deleted.status_code == 200 and worker.killed
    assert not app.state.services.attachments._file(aid).exists()
    assert (await client.get('/api/v1/attachments/'+aid, params={'session_id':'cancel'})).status_code == 404


@pytest.fixture
async def running_app(tmp_path):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    cfg = Settings(data_dir=str(tmp_path), knowledge_root=str(tmp_path/'empty'), llm_backend='mock',
        embedding_backend='deterministic', chroma_mode='disabled', redis_url='', patient_education_enabled=False,
        mcp_base_url=f'http://127.0.0.1:{port}/mcp/', tool_timeout_seconds=8)
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(400):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError('server_not_started')
            await asyncio.sleep(.05)
        assert server.started
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=30, trust_env=False) as client:
            yield app, client
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


def ev(eid='e1', text='房颤常见检查包括心电图，检查用于记录心律。'):
    return EvidenceItem(evidence_id=eid, source_id=eid, source_type='clinical_guideline', title='房颤检查',
                        content=text, is_demo=False, metadata={'partition':'evidence'})


@pytest.mark.parametrize('question,goal', [('房颤的常见检查有哪些？','diagnosis'), ('心律不齐的症状是什么','symptoms'), ('心率不齐的症状是什么','symptoms'), ('心率不齐的症状是什么，要用什么药缓解？','symptoms')])
async def test_patient_education_fetches_real_mcp_before_specialist_papers(running_app, question, goal):
    from dataclasses import replace
    from app.tools.patient_sources import education_links, education_metadata
    app, client = running_app
    s = app.state.services
    s.gateway.settings = replace(s.gateway.settings, patient_education_enabled=True)
    url = education_links(question)[0]['url']
    content = ('Atrial fibrillation diagnosis uses an electrocardiogram (ECG), Holter monitoring and blood tests. '
               if goal == 'diagnosis' else 'Arrhythmia symptoms include palpitations, dizziness, shortness of breath, fainting and tiredness. ')
    public = EvidenceItem(evidence_id='nhlbi', source_id=url, url=url, source_type='web_reference',
        title='Atrial fibrillation diagnosis' if goal == 'diagnosis' else 'Arrhythmias symptoms',
        content=content * 2, is_demo=False, metadata={'external':True, **education_metadata(url)})
    s.gateway.backend.network.fetch = AsyncMock(return_value={'status':'available','evidence':[public.model_dump()]})
    s.gateway.backend.network.pubmed = AsyncMock(side_effect=AssertionError('no need for specialist papers'))
    s.llm.plan_tools = AsyncMock(side_effect=AssertionError('host-owned discovery needs no planner'))
    response = await client.post('/api/v1/chat', json={'message':question, 'network_mode':'auto'})
    body = response.json()
    assert response.status_code == 200 and body['support_mode'] != 'refusal', body
    assert any(c['url'] == url for c in body['citations'])
    assert '信息来源于网络' in body['answer']
    if '用什么药' in question:
        assert 'medication_part_declined' in body['safety_flags']
        assert '关于用药' in body['answer']
    assert any(t['tool']=='fetch_evidence' and t['transport']=='mcp_http' for t in body['tool_summary'])
    s.llm.plan_tools.assert_not_called()
    s.gateway.backend.network.pubmed.assert_not_called()
    s.gateway.backend.network.fetch.reset_mock()
    await client.post('/api/v1/chat', json={'message':question, 'network_mode':'off'})
    s.gateway.backend.network.fetch.assert_not_called()


async def test_patient_source_failure_falls_back_to_pubmed(running_app):
    from dataclasses import replace
    app, client = running_app
    s = app.state.services
    s.gateway.settings = replace(s.gateway.settings, patient_education_enabled=True)
    s.gateway.backend.network.fetch = AsyncMock(side_effect=httpx.ConnectError('offline source'))
    public = ev('p1')
    public.url = 'https://pubmed.ncbi.nlm.nih.gov/123/'
    s.gateway.backend.network.pubmed = AsyncMock(return_value={'status':'available','evidence':[public.model_dump()]})
    body = (await client.post('/api/v1/chat',json={'message':'房颤的常见检查有哪些？','network_mode':'auto'})).json()
    assert body['support_mode'] != 'refusal', body
    assert any(t['tool']=='fetch_evidence' and t['status']=='unavailable' for t in body['tool_summary'])
    assert any(t['tool']=='search_pubmed' and t['status']=='available' for t in body['tool_summary'])
    assert '服务当前不可用' not in body['answer']


async def test_pubmed_requests_relevance_sort_and_education_metadata(tmp_path):
    from app.tools.patient_sources import SOURCES
    seen = []
    def handler(request):
        seen.append(request)
        if 'esearch' in request.url.path:
            return httpx.Response(200, json={'esearchresult':{'idlist':[]}})
        return httpx.Response(200, headers={'content-type':'text/html'}, text='''<html><head>
        <title>Arrhythmias Symptoms | NHLBI</title><meta property="article:modified_time" content="2022-03-24"/>
        </head><body><nav>ignore navigation</nav><main>Arrhythmia symptoms include palpitations,
        dizziness and difficulty breathing. Some arrhythmias do not cause noticeable symptoms.</main></body></html>''')
    net = NetworkEvidence(Settings(data_dir=str(tmp_path), knowledge_root=str(tmp_path/'empty')),
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await net.pubmed('arrhythmia symptoms')
        assert seen[0].url.params['sort'] == 'relevance'
        data = await net.fetch(SOURCES[('arrhythmia','symptoms')], 'scope')
        row = data['evidence'][0]
        assert row['metadata']['source_category'] == 'patient_education'
        assert row['metadata']['document_date'] == '2022-03-24'
        assert not row['medical_truth_verified'] and 'ignore navigation' not in row['content']
    finally:
        await net.close()


async def test_actual_mcp_discovery_and_local_query(running_app):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    import httpx2
    app, client = running_app
    s = app.state.services
    await s.retriever.add([ev()])
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+s.gateway.token}, trust_env=False) as http:
        async with Client(streamable_http_client(s.settings.mcp_base_url, http_client=http)) as mcp:
            discovered = await mcp.list_tools()
            names = {t.name for t in discovered.tools}
            assert {'ocr_pages','search_local','fetch_evidence'} <= names
            invalid = await mcp.call_tool('search_local', {'arguments':{'query':'房颤'}, 'capability':'invented'})
            assert invalid.is_error
    ctx = ToolContext('test')
    result = await s.gateway.call('search_local', {'query':'房颤检查'}, ctx)
    assert result['evidence'][0]['evidence_id'] == 'e1'
    assert ctx.trace[0]['transport'] == 'mcp_http', ctx.trace
    assert s.gateway.last_status == 'available'
    denied = await client.post('/mcp/', json={})
    assert denied.status_code == 401


async def test_upload_confirm_chat_and_emergency(running_app):
    app, client = running_app
    s = app.state.services
    await s.retriever.add([ev()])
    upload = await client.post('/api/v1/attachments', files={'file':('report.txt','检验报告\n心率 72 次/分\n报告用于房颤检查研究'.encode())}, data={'session_id':'s'})
    assert upload.status_code == 202, upload.text
    aid = upload.json()['id']
    for _ in range(100):
        item = (await client.get('/api/v1/attachments/'+aid,params={'session_id':'s'})).json()
        if item['status'] not in {'queued','parsing'}:
            break
        await asyncio.sleep(.1)
    assert item['status'] == 'ready', item
    assert item['pages'][0]['requires_confirmation']
    blocked = await client.post('/api/v1/chat',json={'session_id':'s','message':'房颤检查有哪些','attachment_ids':[aid],'network_mode':'off'})
    assert blocked.status_code == 409
    emergency = await client.post('/api/v1/chat',json={'session_id':'s','message':'胸痛，大汗','attachment_ids':[aid]})
    assert emergency.json()['is_emergency'] and emergency.json()['model_calls'] == 0
    assert emergency.json()['tool_summary'] == []
    confirmed = await client.patch('/api/v1/attachments/'+aid,params={'session_id':'s'},json={'version':1,'confirmed_pages':[1]})
    assert confirmed.status_code == 200
    good = await client.post('/api/v1/chat',json={'session_id':'s','message':'房颤检查有哪些','attachment_ids':[aid],'network_mode':'off'})
    assert good.status_code == 200, good.text
    assert good.json()['attachment_context_refs'][0]['attachment_id'] == aid
    assert good.json()['memory_status'] == 'attachment_not_auto_saved'
    trace = (await client.get('/api/v1/traces/'+good.json()['trace_id'])).text
    assert '心率 72' not in trace and '检验报告' not in trace
    assert (await client.get('/api/v1/attachments/'+aid,params={'session_id':'other'})).status_code == 404


async def test_revision_expiry_and_only_registered_cleanup(tmp_path):
    store = AttachmentStore(tmp_path/'attachments', ROOT/'data/runtime/ocr-models')
    item = store.create('血压 120/80'.encode(),'report.txt','scope')
    await store.parse(item['id'],'scope')
    store.patch(item['id'],'scope',AttachmentPatch(version=1,confirmed_pages=[1]))
    revised = store.patch(item['id'],'scope',AttachmentPatch(version=1,page_texts={1:'血压 121/80'},confirmed_pages=[1]))
    assert revised['version'] == 2 and not revised['pages'][0]['confirmed']
    with pytest.raises(AttachmentError,match='confirmation'):
        store.context([item['id']],'scope')
    with pytest.raises(AttachmentError,match='version_conflict'):
        store.patch(item['id'],'scope',AttachmentPatch(version=1,confirmed_pages=[1]))
    unrelated = store.root/'translation.partial'
    unrelated.write_text('keep')
    with store.connect() as conn:
        conn.execute('UPDATE attachments SET expires=0 WHERE id=?',(item['id'],))
    assert await store.cleanup() == 1
    assert unrelated.read_text() == 'keep'
    assert not store._file(item['id']).exists()
    await store.close()


@pytest.mark.parametrize('url', ['file:///C:/Windows/system.ini','http://127.0.0.1/','http://169.254.169.254/','http://[::1]/','http://localhost/','https://user:pass@example.org/','https://example.org:8000/'])
async def test_ssrf_schemes_and_addresses(url):
    with pytest.raises(BlockedURL):
        valid_url(url)


async def test_dns_rebinding_is_pinned_and_private_rejected(monkeypatch):
    backend = PublicNetworkBackend()
    connected = AsyncMock(return_value='stream')
    backend.backend.connect_tcp = connected
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a:[(2,1,6,'',('93.184.216.34',443))])
    assert await backend.connect_tcp('example.org',443) == 'stream'
    assert connected.call_args.args[0] == '93.184.216.34'
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(BlockedURL):
        await backend.connect_tcp('example.org',443)


async def test_public_doh_cannot_disable_private_ip_check():
    backend=PublicNetworkBackend('public_doh')
    backend.resolve=AsyncMock(return_value=['10.0.0.5'])
    with pytest.raises(BlockedURL):
        await backend.connect_tcp('some.example',443)


async def test_redirect_and_response_limit(tmp_path):
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'location':'http://127.0.0.1/private'})
    net=NetworkEvidence(Settings(data_dir=str(tmp_path),knowledge_root=str(tmp_path/'empty')),client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(BlockedURL):
        await net.request('https://www.fda.gov/public')
    assert len(calls)==1
    await net.close()


async def test_network_off_and_budget_and_invalid_node(running_app):
    app,_=running_app
    gateway=app.state.services.gateway
    gateway.backend.network.pubmed=AsyncMock(side_effect=AssertionError('no external access'))
    ctx=ToolContext('scope',network=False,max_calls=1)
    assert (await gateway.call('search_pubmed',{'query':'房颤'},ctx))['error']=='network_disabled'
    assert (await gateway.call('ocr_pages',{'attachment_id':'x'},ctx))['error']=='tool_not_allowed_at_node'
    await gateway.call('lookup_ddi',{'drug_a':'a','drug_b':'b'},ctx)
    assert (await gateway.call('search_local',{'query':'q'},ctx))['error']=='tool_budget_exhausted'
    gateway.backend.network.pubmed.assert_not_called()


async def test_controlled_network_evidence_loop_and_single_rerank(running_app):
    app,client=running_app
    s=app.state.services
    public=ev('external')
    public.url='https://pubmed.ncbi.nlm.nih.gov/123/'
    public.medical_truth_verified=True  # Malicious/untrusted tool cannot grant trust.
    s.gateway.backend.network.pubmed=AsyncMock(return_value={'status':'available','evidence':[public.model_dump()]})
    s.retriever.reranker.rank=AsyncMock(side_effect=lambda q,e:e)
    result=await client.post('/api/v1/chat',json={'message':'房颤检查有哪些','network_mode':'auto'})
    assert result.status_code==200,result.text
    body=result.json()
    assert body['citations'],body
    assert not body['citations'][0]['medical_truth_verified']
    assert body['retrieval_status']=='network_only'
    assert any(t['tool']=='search_pubmed' and t['transport']=='mcp_http' for t in body['tool_summary'])
    assert s.retriever.reranker.rank.await_count==1
    assert len(body['tool_summary'])<=8


async def test_weak_local_dialogue_forces_network_and_marks_source(running_app):
    app,client=running_app
    s=app.state.services
    dialogue=ev('dialogue', '有人说心律不齐可能会心慌，但这只是问答经验。')
    dialogue.source_type='huatuo_cardio'
    await s.retriever.add([dialogue])
    public=EvidenceItem(evidence_id='pubmed', source_id='pmid', source_type='pubmed_abstract',
        title='Atrial fibrillation diagnosis', content='Electrocardiography and ambulatory monitoring are used in the evaluation of atrial fibrillation.',
        url='https://pubmed.ncbi.nlm.nih.gov/123/', is_demo=False,
        metadata={'partition':'session_evidence','external':True})
    s.gateway.backend.network.pubmed=AsyncMock(return_value={'status':'available','evidence':[public.model_dump()]})
    s.llm.plan_tools=AsyncMock(return_value={'decision':'ready','missing':[],'actions':[]})
    result=await client.post('/api/v1/chat',json={'message':'房颤的常见检查有哪些？','network_mode':'auto'})
    body=result.json()
    assert any(t['tool']=='search_pubmed' and t['status']=='available' for t in body['tool_summary'])
    assert any(c['is_network'] for c in body['citations']), body
    assert '信息来源于网络' in body['answer']


async def test_invalid_deepseek_rerank_degrades_without_chat_500(running_app):
    app, client = running_app
    s = app.state.services
    await s.retriever.add([ev()])

    class InvalidReranker:
        async def rank(self, query, evidence):
            raise ValueError('invalid rerank result')

    s.retriever.current.reranker = InvalidReranker()
    response = await client.post('/api/v1/chat', json={
        'message': '房颤的常见检查有哪些？', 'network_mode': 'off'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['answer']
    assert any('rerank_fallback:ValueError' in reason for reason in body['degradation_reasons'])


async def test_network_evidence_is_retained_when_local_qa_fills_shortlist(running_app):
    app, client = running_app
    s = app.state.services
    for index in range(20):
        item = ev('local-' + str(index), '房颤检查包括心电图，但这是本地问答资料。')
        item.source_type = 'huatuo_cardio'
        await s.retriever.add([item])
    public = EvidenceItem(evidence_id='pubmed', source_id='pmid', source_type='pubmed_abstract',
        title='Arrhythmia symptoms',
        content='Arrhythmia may cause palpitations, an irregular heartbeat, dizziness, or shortness of breath.',
        url='https://pubmed.ncbi.nlm.nih.gov/123/', is_demo=False,
        metadata={'partition':'session_evidence','external':True})
    s.gateway.backend.network.pubmed = AsyncMock(return_value={
        'status':'available', 'evidence':[public.model_dump()]})
    s.llm.plan_tools = AsyncMock(return_value={'decision':'ready','missing':[],'actions':[]})
    response = await client.post('/api/v1/chat', json={
        'message': '心律不齐的症状是什么样的', 'network_mode': 'auto'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(c['is_network'] for c in body['citations']), body
    assert '信息来源于网络' in body['answer']


async def test_network_failure_is_visible_in_refusal(running_app):
    app, client = running_app
    s = app.state.services
    s.gateway.backend.network.pubmed = AsyncMock(side_effect=RuntimeError('network_down'))
    response = await client.post('/api/v1/chat', json={
        'message': '心律不齐的症状是什么样的', 'network_mode': 'auto'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['support_mode'] == 'refusal'
    assert '联网检索' in body['answer'] and '不可用' in body['answer']
    assert 'network_evidence_unavailable' in body['uncertainty']


async def test_real_cpu_ocr_and_scanned_pdf(tmp_path):
    model_dir=ROOT/'data/runtime/ocr-models'
    assert (model_dir/'manifest.json').exists(), 'Run python -m scripts.setup_ocr'
    image=Image.new('RGB',(1000,300),'white')
    draw=ImageDraw.Draw(image)
    font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',44)
    draw.text((30,40),'Heart rate 72 bpm',font=font,fill='black')
    draw.text((30,130),'Blood pressure 120/80',font=font,fill='black')
    png=tmp_path/'scan.png';image.save(png)
    pdf=tmp_path/'scan.pdf';image.save(pdf,'PDF')
    for path in [png,pdf]:
        parsed=await asyncio.to_thread(parse,path,model_dir)
        assert parsed['pages'][0]['method']=='ocr'
        assert '72' in parsed['pages'][0]['text'],parsed
        assert parsed['pages'][0]['blocks'][0]['bbox']


async def test_text_pdf_skips_ocr_and_encrypted_pdf_rejected(tmp_path,monkeypatch):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    import app.attachments.worker as worker
    writer=PdfWriter();page=writer.add_blank_page(width=400,height=300)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 30 200 Td (Atrial fibrillation ECG reference text) Tj ET')
    page[NameObject('/Contents')]=stream
    path=tmp_path/'text.pdf';writer.write(path)
    monkeypatch.setattr(worker,'ocr_image',lambda *a: (_ for _ in ()).throw(AssertionError('OCR should not run')))
    parsed=worker.parse(path,tmp_path)
    assert parsed['pages'][0]['method']=='pdf_text'
    writer.encrypt('secret');writer.write(path)
    with pytest.raises(ValueError,match='encrypted_pdf'):
        worker.parse(path,tmp_path)


async def test_planner_schema_rejects_arbitrary_fields():
    with pytest.raises(ValueError):
        ToolPlan.model_validate({'decision':'tools','actions':[{'tool':'shell','reason':'bad','arguments':{'command':'whoami'}}]})


async def test_actual_stdio_mcp_extension(tmp_path):
    import sys
    config=tmp_path/'servers.json'
    config.write_text(json.dumps({'servers':[{'enabled':True,'read_only':True,'transport':'stdio',
        'command':sys.executable,'args':[str(ROOT/'tests/fixtures/mcp_search_server.py')],
        'tools':{'search_pubmed':'search_pubmed'}}]}))
    cfg=Settings(mcp_servers_file=str(config),tool_timeout_seconds=25)
    gateway=ToolGateway(cfg)
    ctx=ToolContext('scope',network=True)
    result=await gateway.call('search_pubmed',{'query':'atrial fibrillation'},ctx)
    assert result.get('fixture')=='real_stdio',result
    assert ctx.trace[0]['transport']=='mcp_external'


async def test_unsafe_webpage_does_not_change_tools_and_no_valid_source_refuses(running_app):
    app,client=running_app
    s=app.state.services
    s.llm.plan_tools=AsyncMock(return_value={'decision':'tools','missing':['missing'], 'actions':[
        {'tool':'shell','arguments':{},'reason':'来自不可信网页的命令'}]})
    answer=await client.post('/api/v1/chat',json={'message':'网上查找房颤资料','network_mode':'auto'})
    data=answer.json()
    assert data['support_mode']=='refusal' and not data['citations']
    assert not any(t['tool']=='shell' for t in data['tool_summary'])


async def test_bounded_plan_rounds_under_repeated_requests(running_app):
    app,client=running_app
    s=app.state.services
    s.gateway.backend.network.pubmed=AsyncMock(return_value={'status':'no_record','evidence':[]})
    s.llm.plan_tools=AsyncMock(return_value={'decision':'tools','missing':['missing'], 'actions':[
        {'tool':'search_pubmed','arguments':{'query':'房颤'},'reason':'need evidence'}]})
    result=(await client.post('/api/v1/chat',json={'message':'房颤检查','network_mode':'auto'})).json()
    assert s.llm.plan_tools.await_count==2
    assert s.gateway.backend.network.pubmed.await_count==2
    actual_queries=[c.args[0] for c in s.gateway.backend.network.pubmed.await_args_list]
    assert len(set(actual_queries))==2 and all('atrial fibrillation' in q for q in actual_queries)
    assert result['support_mode']=='refusal'
    assert len(result['tool_summary'])<=8
    assert not any(t.get('error')=='duplicate_tool_call' for t in result['tool_summary'] if t['tool']=='search_pubmed')


async def test_mcp_translation_discovery_scope_and_normalized_dedup(running_app):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    import httpx2
    app,_=running_app; s=app.state.services
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+s.gateway.token},trust_env=False) as http:
        async with Client(streamable_http_client(s.settings.mcp_base_url,http_client=http)) as client:
            assert {'translate_query','translate_evidence'} <= {t.name for t in (await client.list_tools()).tools}
    ctx=ToolContext('s',network=True)
    result=await s.gateway.call('translate_query',{'query':'房颤检查需要空腹吗'},ctx)
    assert result['method']=='glossary' and ctx.trace[-1]['transport']=='mcp_http'
    assert any('fasting' in v['query'] for v in result['variants'])
    denied=await s.gateway.call('translate_evidence',{'evidence_ids':['invented']},ctx)
    assert denied['error']=='evidence_translation_scope_denied'
    s.gateway.backend.network.pubmed=AsyncMock(return_value={'status':'no_record'})
    await s.gateway.call('search_pubmed',{'query':'atrial  fibrillation'},ctx)
    duplicated=await s.gateway.call('search_pubmed',{'query':'Atrial Fibrillation'},ctx)
    assert duplicated['error']=='duplicate_tool_call' and s.gateway.backend.network.pubmed.await_count==1
    blocked=await s.gateway.call('translate_query',{'query':'房颤'},ToolContext('offline'))
    assert blocked['error']=='network_disabled'


async def test_composite_question_has_distinct_local_goals(running_app):
    app,client=running_app; s=app.state.services
    await s.retriever.add([ev()])
    s.gateway.backend.execute=AsyncMock(wraps=s.gateway.backend.execute)
    result=(await client.post('/api/v1/chat',json={'message':'房颤的检查方法有哪些，我要不要早上空腹去','network_mode':'off'})).json()
    queries=[c.args[1].query for c in s.gateway.backend.execute.await_args_list if c.args[0]=='search_local']
    assert len(queries)==2 and len(set(queries))==2
    assert any('检查方法' in q for q in queries) and any('空腹' in q for q in queries)
    assert result['citations'] and any('空腹' in gap for gap in result['evidence_gaps'])
    assert not any(t['tool'].startswith('translate_') for t in result['tool_summary'])


async def test_parser_caps_and_unknown_files(tmp_path):
    store=AttachmentStore(tmp_path/'attachments',ROOT/'data/runtime/ocr-models')
    for data in [b'MZ\x00\x00', b'', b'\x01bad']:
        with pytest.raises(AttachmentError):
            store.create(data,'fake.png','scope')
    from pypdf import PdfWriter
    writer=PdfWriter()
    for _ in range(21): writer.add_blank_page(width=200,height=200)
    path=tmp_path/'large.pdf';writer.write(path)
    with pytest.raises(ValueError,match='page_limit'):
        parse(path,tmp_path)
    await store.close()


async def test_explicit_case_fact_save_is_redacted_and_not_automatic(running_app):
    app,client=running_app
    case=(await client.post('/api/v1/cases',json={'notes':'original'})).json()
    s=app.state.services
    scope=scope_key('s',case['case_id'])
    item=s.attachments.create('心率 72 次/分\n电话 13800138000'.encode(),'research.txt',scope)
    await s.attachments.parse(item['id'],scope)
    params={'session_id':'s','case_id':case['case_id']}
    body={'version':1,'target_case_id':case['case_id'],'facts':['心率 72 次/分','电话 13800138000']}
    blocked=await client.post('/api/v1/attachments/'+item['id']+'/save-facts',params=params,json=body)
    assert blocked.status_code==409
    assert s.cases.get(case['case_id']).notes=='original'
    s.attachments.patch(item['id'],scope,AttachmentPatch(version=1,confirmed_pages=[1]))
    saved=await client.post('/api/v1/attachments/'+item['id']+'/save-facts',params=params,json=body)
    assert saved.status_code==200,saved.text
    notes=s.cases.get(case['case_id']).notes
    assert '心率 72' in notes and '13800138000' not in notes


async def test_mcp_failure_only_native_local_fallback(tmp_path):
    from app.tools.gateway import ToolBackend
    from app.retrieval.hybrid import HybridRetriever
    from app.tools.drug_interaction import DrugInteractionTool
    cfg=Settings(data_dir=str(tmp_path),knowledge_root=str(tmp_path/'empty'),mcp_enabled=False)
    gateway=ToolGateway(cfg)
    store=AttachmentStore(tmp_path/'uploads',cfg.ocr_model_dir)
    retriever=HybridRetriever();await retriever.add([ev()])
    gateway.backend=ToolBackend(cfg,retriever,store,DrugInteractionTool())
    gateway._remote=AsyncMock(side_effect=TimeoutError())
    ctx=ToolContext('scope',network=True)
    local=await gateway.call('search_local',{'query':'房颤检查'},ctx)
    assert local['evidence'] and ctx.trace[-1]['transport']=='native_fallback'
    remote=await gateway.call('search_pubmed',{'query':'房颤'},ctx)
    assert remote['status']=='unavailable' and not remote['evidence']
    await gateway.backend.network.close();await store.close()


async def test_mixed_pdf_keeps_page_numbers(tmp_path):
    from pypdf import PdfWriter, PdfReader
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer=PdfWriter();page=writer.add_blank_page(width=400,height=300)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 30 200 Td (Atrial fibrillation patient research report) Tj ET');page[NameObject('/Contents')]=stream
    image=Image.new('RGB',(900,200),'white');ImageDraw.Draw(image).text((30,50),'Heart rate 72 bpm',font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',40),fill='black')
    scan=tmp_path/'scan.pdf';image.save(scan,'PDF');writer.append(PdfReader(scan));path=tmp_path/'mixed.pdf';writer.write(path)
    result=await asyncio.to_thread(parse,path,ROOT/'data/runtime/ocr-models')
    assert [(p['page'],p['method']) for p in result['pages']]==[(1,'pdf_text'),(2,'ocr')]


async def test_attachment_patient_gate_does_not_treat_reference_as_emergency(running_app):
    app,client=running_app
    s=app.state.services
    scope=scope_key('s')
    item=s.attachments.create('主诉：现在胸痛伴大汗'.encode(),'report.txt',scope)
    await s.attachments.parse(item['id'],scope)
    s.attachments.patch(item['id'],scope,AttachmentPatch(version=1,confirmed_pages=[1]))
    answer=(await client.post('/api/v1/chat',json={'session_id':'s','message':'解释附件','attachment_ids':[item['id']],'network_mode':'off'})).json()
    assert answer['is_emergency'] and answer['model_calls']==0 and not answer['tool_summary']
    s.attachments.patch(item['id'],scope,AttachmentPatch(version=1,purpose='reference'))
    s.attachments.patch(item['id'],scope,AttachmentPatch(version=2,confirmed_pages=[1]))
    answer=(await client.post('/api/v1/chat',json={'session_id':'s','message':'解释这份指南','attachment_ids':[item['id']],'network_mode':'off'})).json()
    assert not answer['is_emergency']
