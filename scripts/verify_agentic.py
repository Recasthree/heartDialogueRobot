"""Reproducible isolated browser / public network / budgeted model smoke.

Uses real HTTP MCP; never rebuilds or mutates the shared corpus index.
"""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import time

import httpx
import uvicorn

from app.config import ROOT, Settings
from app.main import create_app
from app.models import EvidenceItem


async def main(args):
    run_id = time.strftime('%Y%m%d-%H%M%S')
    output = ROOT / 'data/runtime/verification' / ('agentic-' + run_id)
    output.mkdir(parents=True, exist_ok=True)
    original = Settings.from_env()
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    cfg=replace(original, data_dir=str(output/'runtime'), evaluation_dir=str(output/'evaluations'),
        embedding_backend='deterministic', chroma_mode='disabled', redis_url='',
        llm_backend=original.llm_backend if args.live else 'mock',
        mcp_base_url=f'http://127.0.0.1:{port}/mcp/', network_enabled=True,
        deepseek_max_retries=0, deepseek_timeout_seconds=40, request_timeout_seconds=150)
    app=create_app(cfg)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False,log_level='warning'))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    report={'run_id':run_id,'llm_backend':cfg.llm_backend,'model_budget':10,'checks':[]}
    try:
        for _ in range(600):
            if server.started:break
            if task.done():await task
            await asyncio.sleep(.05)
        if not server.started:raise RuntimeError('startup_timeout')
        s=app.state.services
        s.llm.request_budget=10
        # Existing real source, with its original trust fields. No synthetic clinical facts.
        source=Path(cfg.knowledge_root)/'knowledge/evidence_items.jsonl'
        selected=[]
        with source.open(encoding='utf-8') as stream:
            for line in stream:
                row=json.loads(line)
                if len(row.get('content','')) > 100 and 'ventricular' in row.get('content','').lower():
                    selected.append(EvidenceItem.model_validate({**row,'is_demo':False,
                        'metadata':{**row.get('metadata',{}),'partition':'evidence'}}))
                if len(selected)==5:break
        await s.retriever.add(selected)
        report['fixture_source_ids']=[e.evidence_id for e in selected]
        if args.network:
            for name, query in [('search_pubmed','atrial fibrillation guideline'),('search_drug_labels','aspirin')]:
                from app.tools.contracts import ToolContext
                ctx=ToolContext('smoke',network=True)
                result=await s.gateway.call(name,{'query':query},ctx,'公开医学接口连通性验证')
                report['checks'].append({'name':name,'status':result.get('status'),
                    'evidence_count':len(result.get('evidence',[])),'trace':ctx.trace})
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=180,trust_env=False) as client:
            request={'message':'What does this guideline say about ventricular arrhythmias? Cite the provided local sources only.','network_mode':'off'}
            start=time.perf_counter();response=await client.post('/api/v1/chat',json=request)
            report['checks'].append({'name':'local_cited_answer','http_status':response.status_code,
                'duration_ms':round((time.perf_counter()-start)*1000),'result':response.json()})
            if args.safety:
                response=await client.post('/api/v1/evaluations/run',json={'suite':'safety','mode':'full',
                    'max_cases':500,'use_llm_judge':False,'background':False})
                value=response.json()
                safety=value.get('suites',{}).get('safety',{})
                report['checks'].append({'name':'strict_safety','http_status':response.status_code,
                    'run_id':value.get('run_id'),'total':safety.get('total'),'passed':safety.get('passed'),
                    'model_calls':value.get('model_calls'),
                    'failed_ids':[r['case_id'] for r in safety.get('results',[]) if not r['passed']]})
            if args.live and s.llm.total_requests < 8:
                response=await client.post('/api/v1/chat',json={'message':'请联网查找 aspirin 的说明书注意事项，给出来源，不提供个人用药方案。','network_mode':'auto'})
                report['checks'].append({'name':'drug_network_route','http_status':response.status_code,'result':response.json()})
            if args.browser:
                await browser(client,port,output,report,network=args.network)
        report['model_requests']=getattr(s.llm,'total_requests',0)
        assert report['model_requests']<=10
    except Exception as exc:
        report['error']=type(exc).__name__
        raise
    finally:
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        server.should_exit=True
        await asyncio.wait_for(task,20)
        sock.close()
        print(json.dumps({'report':str(output/'report.json'),'model_requests':report.get('model_requests',0),'error':report.get('error')},ensure_ascii=True))


async def browser(client,port,output,report,network=False):
    # Reuse the existing optional browser-test package without shadowing core dependencies.
    import importlib.util
    if importlib.util.find_spec('playwright') is None and (ROOT/'.runtime-deps/playwright').exists():
        import sys
        sys.path.append(str(ROOT/'.runtime-deps'))
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='chrome',headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1100})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        await page.goto(f'http://127.0.0.1:{port}/')
        await page.locator('#runtimeStatus').filter(has_text='MCP').wait_for()
        await page.set_input_files('#attachmentInput',{'name':'research-report.txt','mimeType':'text/plain','buffer':'研究报告\n心率 72 次/分\n请核对检查时间'.encode()})
        await page.get_by_role('button',name='确认本页内容',exact=True).wait_for(timeout=60000)
        await page.screenshot(path=str(output/'desktop-attachment.png'),full_page=True)
        await page.get_by_role('button',name='确认本页内容',exact=True).click()
        await page.locator('#attachmentList summary').filter(has_text='可用于本轮').wait_for()
        await page.locator('#networkMode').uncheck()
        await page.locator('#messageInput').fill('胸痛，大汗')
        await page.locator('#sendChat').click()
        await page.locator('#requestState').filter(has_text='本轮完成').wait_for(timeout=30000)
        await page.screenshot(path=str(output/'desktop-response.png'),full_page=True)
        if network:
            await page.locator('#networkMode').check()
            await page.locator('#messageInput').fill('阿司匹林 aspirin warnings 联网')
            async with page.expect_response(lambda r: r.url.endswith('/api/v1/chat'), timeout=150000) as pending:
                await page.locator('#sendChat').click()
            response = await pending.value
            value = await response.json()
            await page.locator('#requestState').filter(has_text='本轮完成').wait_for(timeout=30000)
            await page.locator('#tracePanel').evaluate('(el) => el.open = true')
            report['checks'].append({'name':'browser_attachment_network_citations', 'result':value})
            await page.screenshot(path=str(output/'desktop-network-citations.png'),full_page=True)
            assert value.get('attachment_context_refs') and value.get('citations'), value.get('degradation_reasons')
            assert any(t['tool']=='search_drug_labels' and t['status']=='available' for t in value['tool_summary'])
        await page.set_viewport_size({'width':390,'height':844})
        await page.screenshot(path=str(output/'mobile.png'),full_page=True)
        overflow=await page.evaluate('document.documentElement.scrollWidth > innerWidth')
        report['checks'].append({'name':'browser','console_errors':errors,'horizontal_overflow':overflow,
            'attachment_confirmation':True,'emergency_response':await page.locator('#messages').inner_text()})
        assert not errors and not overflow
        await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--network',action='store_true');parser.add_argument('--browser',action='store_true');parser.add_argument('--safety',action='store_true')
    args=parser.parse_args()
    if args.live and args.safety:
        parser.error('Safety audit is offline Mock only; keep the metered smoke budget separate.')
    asyncio.run(main(args))
