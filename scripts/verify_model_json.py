"""Two-request opt-in probe of generation and safety JSON; no patient inputs.

The preceding Agentic smoke used eight requests. This probe completes the
delivery's ten-request ceiling; it never retries or executes the whole graph.
"""
import asyncio
import json
import time

from app.config import ROOT, Settings
from app.models import EvidenceItem
from app.providers import DeepSeekProvider


async def main():
    cfg = Settings.from_env()
    llm = DeepSeekProvider(cfg.deepseek_api_key, cfg.aihubmix_base_url, cfg.deepseek_model,
        timeout=60, max_retries=0, thinking_mode=cfg.deepseek_thinking_mode,
        max_tokens=cfg.deepseek_max_tokens)
    llm.request_budget = 2
    report = {'prior_delivery_requests': 8, 'request_budget': 2,
              'thinking_mode': cfg.deepseek_thinking_mode, 'checks': []}
    source = ROOT / 'data/verified/knowledge/evidence_items.jsonl'
    evidence = []
    with source.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if len(row.get('content', '')) > 100 and 'ventricular' in row.get('content', '').lower():
                evidence = [EvidenceItem.model_validate(row)]
                break
    assert evidence, 'existing_real_source_required'
    query = 'In one sentence, summarize this excerpt about ventricular arrhythmias. Cite only this source; give no personal treatment advice.'
    try:
        try:
            raw = await llm.generate(query, evidence)
            value = json.loads(raw)
            # Preserve only non-patient source-based output, never the key or reasoning.
            report['checks'].append({'name': 'generation', 'status': 'valid_json',
                'result': value, 'response_metadata': llm.last_response_metadata})
            answer = value['answer']
        except Exception as exc:
            report['checks'].append({'name': 'generation', 'status': 'failed',
                'error': type(exc).__name__, 'response_metadata': llm.last_response_metadata})
            answer = 'Insufficient evidence; no medical conclusion is provided.'
        review = await llm.safety_review(query, answer, evidence)
        report['checks'].append({'name': 'safety', 'result': review,
            'response_metadata': llm.last_response_metadata})
    finally:
        report['model_requests'] = llm.total_requests
        report['delivery_total_requests'] = 8 + llm.total_requests
        report['source_ids'] = [e.evidence_id for e in evidence]
        path = ROOT / 'data/runtime/verification' / ('model-json-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        await llm.close()
        print(json.dumps({'report': str(path), 'model_requests': llm.total_requests,
                          'delivery_total_requests': report['delivery_total_requests']}))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True,
                        help='Explicit consent to two metered API requests')
    parser.parse_args()
    asyncio.run(main())
