"""Two real HTTP/MCP chat turns, one session, at most ten model attempts."""
import argparse
import asyncio
from dataclasses import replace
import json
import re
import socket
import time

import httpx
import uvicorn
from app.config import Settings, ROOT
from app.main import create_app


async def main(max_requests=10, case='both'):
    output = ROOT/'data/runtime/verification'/('dialogue-scope-'+time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    cfg=replace(Settings.from_env(),deepseek_max_retries=0,mcp_base_url=f'http://127.0.0.1:{port}/mcp/')
    if cfg.llm_backend != 'deepseek':
        raise RuntimeError('live_test_requires_explicit_deepseek_configuration')
    app=create_app(cfg)
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    report={'scope':'full_http_langgraph_mcp_real_model','case':case,'model_request_budget':max_requests,'checks':[]}
    llm=None
    try:
        for _ in range(1200):
            if server.started: break
            if task.done(): await task; raise RuntimeError('startup_failed')
            await asyncio.sleep(.05)
        if not server.started: raise RuntimeError('startup_timeout')
        llm=app.state.services.llm
        llm.request_budget=max_requests
        original_json=llm._json
        report['generation_contracts']=[]
        async def audit_json(system,user):
            value=await original_json(system,user)
            payload=json.loads(user)
            if 'response_scope' in payload:
                # The two fixed smoke questions contain no patient information.
                # Capture only contract metadata, never raw model reasoning.
                report['generation_contracts'].append({
                    'support_mode':value.get('support_mode'),
                    'citation_ids':value.get('citation_ids'),
                    'markers':re.findall(r'\[(\d+)\]',str(value.get('answer',''))),
                    'unsupported_claim_count':len(value.get('unsupported_claims',[]) or []),
                    'allowed_ids':[e['evidence_id'] for e in payload['evidence']],
                })
            return value
        llm._json=audit_json
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=220,trust_env=False) as client:
            questions=['心率不齐的症状是什么样子的，要用什么药缓解？','心率不齐的症状是什么样子的']
            if case=='mixed': questions=questions[:1]
            for index,question in enumerate(questions):
                response=await client.post('/api/v1/chat',json={'message':question,'session_id':output.name,'network_mode':'auto'})
                value=response.json()
                tests={'http_json':response.status_code==200,
                       'symptoms_answered':value.get('support_mode')=='cited' and bool(value.get('citations')),
                       'no_raw_status':not re.search(r'pending|review_status|medical_truth_verified',value.get('answer',''),re.I),
                       'official_source':any('nhlbi.nih.gov' in c.get('url','') for c in value.get('citations',[])),
                       'correct_scope': ('medication_part_declined' in value.get('safety_flags',[])) if index==0 else
                           not re.search(r'关于.{0,10}(?:用药|药缓解)|加量、减量|为你选药|推荐.{0,6}药',value.get('answer',''))}
                report['checks'].append({'question':question,'response':value,'assertions':tests,'passed':all(tests.values())})
                print(json.dumps({'turn':index+1,'checks':tests,'model_requests':llm.total_requests},ensure_ascii=True),flush=True)
                if llm.total_requests>=max_requests and index<len(questions)-1:
                    report['remaining_turn_skipped']='model_budget_exhausted'; break
        report['passed']=len(report['checks'])==len(questions) and all(row['passed'] for row in report['checks'])
    except Exception as exc:
        report['error']=type(exc).__name__; report['passed']=False
    finally:
        report['model_requests']=getattr(llm,'total_requests',0)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        server.should_exit=True
        await asyncio.wait_for(task,30)
        sock.close()
        print(json.dumps({'report':str(output/'report.json'),'passed':report.get('passed'),
                          'model_requests':report['model_requests'],'error':report.get('error')},ensure_ascii=True))
    return report.get('passed',False)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--case',choices=['both','mixed'],default='both')
    parser.add_argument('--max-model-requests',type=int,choices=range(1,11),default=10)
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.max_model_requests,args.case)) else 1)
