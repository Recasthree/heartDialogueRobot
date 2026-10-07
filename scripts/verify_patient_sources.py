"""Actual public fetch over MCP; optional TWO model requests for a batch test.

--prior-report is mandatory for paid verification and counts earlier attempts.
No key, patient input or private attachment is written to the report.
"""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import time

import uvicorn
from app.config import Settings, ROOT
from app.core.graph import citation_errors
from app.main import create_app
from app.models import EvidenceItem, GenerationPayload
from app.providers import CALL_CONTEXT, prompt_text
from app.tools.contracts import ToolContext
from app.tools.patient_sources import education_links

QUESTIONS = ['房颤的常见检查有哪些？', '心律不齐的症状是什么？']


async def main(args):
    previous = json.loads(Path(args.prior_report).read_text(encoding='utf-8')) if args.prior_report else {}
    used = previous.get('model_requests', 0)
    if args.live_two and (not args.prior_report or used > 8):
        raise ValueError('Two model attempts require prior report and remaining budget >= 2')
    output = ROOT / 'data/runtime/verification' / ('patient-sources-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    report = {'scope':'actual_mcp_public_fetch_and_optional_batch_generation_review',
              'prior_model_requests':used, 'model_request_budget':10, 'checks':[]}
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    cfg = replace(Settings.from_env(), deepseek_max_retries=0, mcp_base_url=f'http://127.0.0.1:{port}/mcp/')
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    provider = None
    token = None
    try:
        for _ in range(1200):
            if server.started: break
            if task.done(): await task; raise RuntimeError('startup_failed')
            await asyncio.sleep(.05)
        if not server.started: raise RuntimeError('startup_timeout')
        services = app.state.services
        provider = services.llm
        provider.request_budget = 2 if args.live_two else 0
        batch = []
        for i, question in enumerate(QUESTIONS):
            url = education_links(question)[0]['url']
            ctx = ToolContext(output.name, network=True, allowed_urls={url})
            public = await services.gateway.call('fetch_evidence', {'url':url}, ctx, '实际获取官方患者教育正文，不调用模型')
            evidence = [EvidenceItem.model_validate(row) for row in public.get('evidence', [])]
            ranked = await services.retriever.rerank(question, evidence, 5, enabled=False)
            check = {'question':question, 'tools':ctx.trace, 'status':public['status'],
                     'fetched_count':len(evidence), 'selected_count':len(ranked),
                     'evidence':[e.model_dump() for e in ranked]}
            report['checks'].append(check)
            batch.append({'case_id':str(i), 'query':question, 'evidence':check['evidence']})
            print(json.dumps({k:v for k,v in check.items() if k not in {'evidence','tools'}},ensure_ascii=True),flush=True)
        if args.live_two and all(c['selected_count'] for c in report['checks']):
            ledger = {'model_calls':0, 'successful_calls':0, 'errors':[]}
            token = CALL_CONTEXT.set(ledger)
            raw = await provider._json(prompt_text('generation_with_citation') +
                '\n这是两条独立问题的批量验收。返回 JSON {"cases":[{"case_id":"0","generation":{...}},{"case_id":"1","generation":{...}}]}。'
                'generation 必须符合给定 schema。仅根据各题提供的正文回答；不能跨题引用。citation_ids 必须使用对应 evidence_id，'
                '按正文首次出现顺序排列，正文用对应 [1]、[2]。禁止个体化诊断、用药剂量或停药建议。标注【信息来源于网络，需要甄别】。'
                '这些是公开患者教育正文，不是已审核指南；可用于带出处的一般介绍，不得把 pending 本身视为拒绝所有一般介绍的理由。',
                json.dumps({'cases':batch, 'schema':GenerationPayload.model_json_schema()},ensure_ascii=False))
            cases = raw['cases']
            assert len(cases)==2 and {c['case_id'] for c in cases}=={'0','1'}
            answers = {c['case_id']:GenerationPayload.model_validate(c['generation']).model_dump() for c in cases}
            review_input=[]
            for i, check in enumerate(report['checks']):
                answer=answers[str(i)]
                check['generation']=answer
                check['citation_errors']=citation_errors(answer,[EvidenceItem.model_validate(e) for e in check['evidence']])
                review_input.append({**batch[i], 'answer':answer})
            reviews=await provider._json(prompt_text('safety_review') +
                '\n分别复核两题，不能以来源 pending 状态直接否定有正文支持的一般信息。返回 JSON '
                '{"cases":[{"case_id":"0","status":"pass|unsafe|unsupported","safe":true,"rule_hits":[]},'
                '{"case_id":"1","status":"pass|unsafe|unsupported","safe":true,"rule_hits":[]}]}。',
                json.dumps({'cases':review_input},ensure_ascii=False))
            results=reviews['cases']
            assert len(results)==2 and {r['case_id'] for r in results}=={'0','1'}
            by_id={r['case_id']:r for r in results}
            for i,check in enumerate(report['checks']):
                check['review']=by_id[str(i)]
                check['passed']=check['generation']['support_mode']=='cited' and not check['citation_errors'] and check['review']['safe'] is True and check['review']['status']=='pass'
        report['passed']=all(check.get('passed',bool(check['selected_count'])) for check in report['checks'])
    except Exception as exc:
        report['error']=type(exc).__name__
        report['passed']=False
    finally:
        if token is not None: CALL_CONTEXT.reset(token)
        report['new_model_requests']=getattr(provider,'total_requests',0)
        report['model_requests']=used+report['new_model_requests']
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        server.should_exit=True
        await asyncio.wait_for(task,30)
        sock.close()
        print(json.dumps({'report':str(output/'report.json'), 'passed':report.get('passed'),
                          'model_requests':report['model_requests'], 'error':report.get('error')},ensure_ascii=True),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--live-two',action='store_true')
    parser.add_argument('--prior-report')
    asyncio.run(main(parser.parse_args()))
