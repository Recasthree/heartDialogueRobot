"""Real MCP + production-index smoke; shared ten-attempt model budget.

The first question traverses the full graph. The second checks public evidence
and source-bound generation/review directly, avoiding another costly planning
cycle. The report labels these scopes explicitly.
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

from app.config import Settings, ROOT
from app.main import create_app
from app.models import EvidenceItem
from app.core.graph import citation_errors
from app.providers import CALL_CONTEXT
from app.tools.contracts import ToolContext
from app.tools.translation import glossary_plan


async def main():
    output = ROOT / 'data/runtime/verification' / ('network-recovery-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    cfg = replace(Settings.from_env(), deepseek_max_retries=0,
                  mcp_base_url=f'http://127.0.0.1:{port}/mcp/')
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    report = {'model_request_budget': 10, 'checks': []}
    services = None
    try:
        for _ in range(1200):
            if server.started: break
            if task.done(): await task; raise RuntimeError('startup_failed')
            await asyncio.sleep(.05)
        if not server.started: raise RuntimeError('startup_timeout')
        services = app.state.services
        services.llm.request_budget = 10
        question = '房颤的常见检查有哪些？'
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=200, trust_env=False) as client:
            response = await client.post('/api/v1/chat', json={'message': question,
                'network_mode': 'auto', 'session_id': output.name})
            value = response.json()
            report['checks'].append({'scope': 'full_graph_http_mcp', 'question': question,
                'http_status': response.status_code, 'response': value})
        print(json.dumps({'scope': 'full_graph', 'mode': value.get('support_mode'),
                          'calls': services.llm.total_requests}, ensure_ascii=True), flush=True)
        question = '心律不齐的症状是什么？'
        plan = glossary_plan(question)
        ctx = ToolContext(output.name, network=True)
        public = await services.gateway.call('search_pubmed', {'query': plan['variants'][0]['query']}, ctx, '真实网络恢复验收')
        evidence = [EvidenceItem.model_validate(row) for row in public.get('evidence', [])][:5]
        check = {'scope': 'public_evidence_generation_review', 'question': question,
                 'tools': ctx.trace, 'evidence': [row.model_dump() for row in evidence]}
        if evidence and services.llm.total_requests <= 8:
            ledger = {'model_calls': 0, 'successful_calls': 0, 'errors': []}
            token = CALL_CONTEXT.set(ledger)
            try:
                answer = json.loads(await services.llm.generate(question, evidence))
                check['generation'] = answer
                check['citation_errors'] = citation_errors(answer, evidence)
                check['review'] = await services.llm.safety_review(question, answer['answer'], evidence)
            finally:
                CALL_CONTEXT.reset(token)
        else:
            check['skipped_generation_reason'] = 'budget_or_no_public_evidence'
        report['checks'].append(check)
    except Exception as exc:
        report['error'] = type(exc).__name__
    finally:
        report['model_requests'] = getattr(services.llm, 'total_requests', 0) if services else 0
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        server.should_exit = True
        await asyncio.wait_for(task, 30)
        sock.close()
        print(json.dumps({'report': str(output / 'report.json'), 'model_requests': report['model_requests'],
                          'error': report.get('error')}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', required=True, action='store_true')
    parser.parse_args()
    asyncio.run(main())
