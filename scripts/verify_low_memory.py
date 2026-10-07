"""Local acceptance. Only --live authorizes up to TEN paid HTTP attempts, shared by every node."""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import sys
import time

import httpx
import psutil
import uvicorn

from app.config import Settings, ROOT
from app.main import create_app
from app.retrieval.index_manager import load_index
from app.tools.translation import glossary_plan


QUESTIONS=['房颤的检查方法有哪些，我要不要早上空腹去','房颤的常见检查有哪些？']


async def main(args):
    output=ROOT/'data/runtime/verification'/('lowmem-'+time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    report={'paid_http_attempts':0,'queries':[],'started_at':time.time()}
    cfg=Settings.from_env()
    start=time.perf_counter(); retriever=load_index(cfg)
    if not retriever.lexical_ready: raise RuntimeError('full_lexical_index_not_ready; no paid request attempted')
    report['index_load_seconds']=round(time.perf_counter()-start,3)
    report['index_version']=retriever.index_version
    report['documents']=len(retriever.documents)
    report['partition_counts']={s:retriever.repo.count(s) for s in ['evidence','benchmark','test']}
    report['rss_after_load_bytes']=psutil.Process().memory_info().rss
    report['encoder_loaded_after_startup']=getattr(retriever.embedding,'_model',None) is not None
    for query in QUESTIONS:
        start=time.perf_counter(); candidates,status=await retriever.candidates(query)
        report['queries'].append({'question':query,'duration_ms':round((time.perf_counter()-start)*1000,2),'status':status,
            'bilingual_plan':glossary_plan(query),'candidates':[r.model_dump() for r in candidates[:5]]})
    if retriever.vector_store: await retriever.vector_store.close()
    if args.live:
        sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        cfg=replace(cfg,deepseek_max_retries=0,mcp_base_url=f'http://127.0.0.1:{port}/mcp/',llm_backend='deepseek')
        app=create_app(cfg)
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
        startup=time.perf_counter()
        task=asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(400):
                if server.started: break
                if task.done(): await task
                await asyncio.sleep(.05)
            if not server.started: raise RuntimeError('acceptance_server_not_started')
            report['app_startup_seconds']=round(time.perf_counter()-startup,3)
            s=app.state.services
            s.llm.request_budget=10
            report['answers']=[]
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=200,trust_env=False) as client:
                report['ready']=(await client.get('/api/v1/ready')).json()
                for index,query in enumerate(QUESTIONS):
                    if s.llm.total_requests>=10: break
                    start=time.perf_counter()
                    result=await client.post('/api/v1/chat',json={'message':query,'network_mode':'auto' if index==0 else 'off',
                        'session_id':output.name+'-'+str(index)})
                    value=result.json()
                    report['answers'].append({'question':query,'response':value,'elapsed_seconds':round(time.perf_counter()-start,3)})
                    report['paid_http_attempts']=s.llm.total_requests
                    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                    print(json.dumps({'question':query,'support_mode':value.get('support_mode'),'calls':s.llm.total_requests},ensure_ascii=False),flush=True)
            assert s.llm.total_requests<=10
        finally:
            if app.state.services: report['paid_http_attempts']=app.state.services.llm.total_requests
            server.should_exit=True
            await asyncio.wait_for(task,20)
            sock.close()
    if args.browser:
        replay=report
        if args.replay_report:
            replay=json.loads(Path(args.replay_report).read_text(encoding='utf-8'))
            report['replayed_from']=str(Path(args.replay_report).resolve())
        if str(ROOT/'.runtime-deps') not in sys.path: sys.path.append(str(ROOT/'.runtime-deps'))
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser=await p.chromium.launch(channel='chrome',headless=True)
            page=await browser.new_page(viewport={'width':1440,'height':1100})
            errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
            await page.goto('http://127.0.0.1:8000/',wait_until='networkidle')
            if replay.get('answers'):
                answer=next((row['response'] for row in replay['answers'] if row['response'].get('citations')),replay['answers'][0]['response'])
                await page.evaluate('async data => { showEvidence(data); addMessage("assistant", "验收回答（已存档）", data.answer, false); await showTrace(data.trace_id); }',answer)
            await page.screenshot(path=str(output/'desktop.png'),full_page=True)
            await page.set_viewport_size({'width':390,'height':844})
            await page.screenshot(path=str(output/'mobile.png'),full_page=True)
            report['browser']={'errors':errors,'horizontal_overflow':await page.evaluate('document.documentElement.scrollWidth > innerWidth')}
            await browser.close()
    report['finished_at']=time.time()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(output/'report.json'),'documents':report['documents'],'paid_http_attempts':report['paid_http_attempts']},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--live',action='store_true'); parser.add_argument('--browser',action='store_true')
    parser.add_argument('--replay-report',help='Replay an existing answer for browser inspection without another paid request')
    asyncio.run(main(parser.parse_args()))
