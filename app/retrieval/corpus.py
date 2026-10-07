"""Read-only adapters. Source records and review flags are never rewritten."""
import hashlib
import json
import re
import os
import tempfile
from pathlib import Path

from ..models import EvidenceItem
from .hybrid import content_hash, stable_evidence_id

CHUNK_VERSION = 'offset384-overlap64-v3'
PRIMARY = {
    'knowledge/evidence_items.jsonl': 'clinical_knowledge',
    'knowledge/drug_labels.jsonl': 'drug_label',
    'knowledge/drug_interactions.jsonl': 'drug_interaction',
    'huatuo_cardio/cardio_high_quality.jsonl': 'huatuo_cardio',
    'medical_dialogue_chinese/canonical_records.jsonl': 'medical_dialogue',
    'drugs/cardiovascular_drugs_enriched_zh.jsonl': 'drug_label_machine_translation',
}


def load_jsonl(path):
    if path.exists():
        with path.open(encoding='utf-8-sig') as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def text_value(value):
    if isinstance(value, list):
        return '\n'.join(text_value(v) for v in value)
    if isinstance(value, dict):
        return '\n'.join(f'{k}: {text_value(v)}' for k, v in value.items() if v)
    return str(value or '')


def sections(row, kind):
    if kind.startswith('drug_label'):
        title = text_value(row.get('generic_name') or row.get('brand_name') or row.get('dailymed_title'))
        for key, value in row.items():
            if key.startswith('openfda_') and key not in {'openfda_status'} and value:
                yield key, title + '\n' + key + '\n' + text_value(value)
    elif kind == 'medical_dialogue':
        for segment in row.get('segments', []):
            context = segment.get('description', {}).get('raw_text', '')
            for block in segment.get('dialogue_blocks', []):
                content = '\n'.join(t.get('role', '') + ': ' + t.get('content', '') for t in block.get('turns', []))
                if content:
                    yield block.get('block_id', segment.get('segment_id', 'dialogue')), context + '\n' + content
    else:
        parts = [text_value(row.get(k)) for k in ['content', 'content_zh', 'content_original', 'question', 'answer'] if row.get(k)]
        yield 'body', '\n'.join(dict.fromkeys(parts)) or json.dumps(row, ensure_ascii=False)


def chunk_text(text, embedding=None):
    if embedding and hasattr(embedding, 'tokenize_chunks'):
        return embedding.tokenize_chunks(text, 384, 64)
    # Deterministic test-only tokenizer; production uses the encoder's exact tokenizer.
    tokens = re.findall(r'[\u4e00-\u9fff]|[^\u4e00-\u9fff\s]+|\s+', text)
    return [''.join(tokens[i:i+384]) for i in range(0, len(tokens), 320)]


def item_from_row(row, relative, kind, sha, index, section, content, scope='evidence', parent=None):
    origin = str(parent or row.get('evidence_id') or row.get('set_id') or row.get('record_id') or row.get('id') or row.get('case_id') or row.get('query_id') or row.get('sample_id') or content_hash(json.dumps(row, sort_keys=True, ensure_ascii=False)))
    identity = stable_evidence_id(relative, origin, section, content)
    source = row.get('source') if isinstance(row.get('source'), dict) else {}
    return EvidenceItem(evidence_id=identity, content=content, source_type=str(row.get('source_type') or kind),
        source_id=str(row.get('source_id') or origin), title=text_value(row.get('title') or row.get('source_title') or row.get('generic_name') or origin),
        url=row.get('url') or row.get('source_url') or row.get('label_url') or source.get('url'),
        page=row.get('page') if isinstance(row.get('page'), int) else None,
        evidence_level=text_value(row.get('evidence_level')) or None,
        is_demo=bool(row.get('is_demo', False)), is_synthetic=bool(row.get('is_synthetic', row.get('is_demo', False))),
        review_status=str(row.get('review_status', row.get('translation_review_status', 'pending'))),
        medical_truth_verified=bool(row.get('medical_truth_verified', False)), translation_status=row.get('translation_status'),
        source_file=relative, source_sha256=sha,
        metadata={**(row.get('metadata') or {}), 'parent_evidence_id': origin, 'section': section,
                  'content_hash': content_hash(content), 'partition': scope, 'retrieval_allowed': scope == 'evidence',
                  'adapter': kind, 'chunk_version': CHUNK_VERSION})


def snapshot_file(path, directory):
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    for attempt in range(3):
        before=path.stat(); digest=hashlib.sha256()
        fd,tmp=tempfile.mkstemp(prefix='snapshot-',suffix='.tmp',dir=directory)
        try:
            with os.fdopen(fd,'wb') as out, path.open('rb') as src:
                while chunk:=src.read(1024*1024):
                    digest.update(chunk); out.write(chunk)
            after=path.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                continue
            sha=digest.hexdigest(); target=directory/(sha+'.jsonl')
            if not target.exists(): os.replace(tmp,target)
            return target,sha,before
        finally:
            Path(tmp).unlink(missing_ok=True)
    raise RuntimeError('source_busy_during_snapshot:'+str(path))


def snapshot_inventory(root, directory):
    base=Path(root); inventory=[]
    for path in sorted(base.rglob('*')):
        if not path.is_file(): continue
        relative=path.relative_to(base).as_posix()
        if path.name.startswith('.') or path.suffix!='.jsonl':
            inventory.append({'file':relative,'indexed':False,'reason':'cache_partial_or_non_jsonl'}); continue
        snapshot,sha,before=snapshot_file(path,directory)
        inventory.append({'file':relative,'indexed':True,'sha256':sha,'snapshot_file':str(snapshot),
                          'bytes':before.st_size,'partition':'evidence' if relative in PRIMARY else 'test'})
    return inventory


def snapshot_items(entry, embedding=None, start_offset=0, start_chunk=0):
    """Yield item and the exact next byte/chunk checkpoint; a resumed row is bounded."""
    relative=entry['file']; kind=PRIMARY.get(relative,'development_fixture'); scope=entry['partition']
    with Path(entry['snapshot_file']).open('rb') as stream:
        stream.seek(start_offset)
        while True:
            offset=stream.tell(); line=stream.readline()
            if not line: break
            end=stream.tell()
            if not line.strip(): continue
            row=json.loads(line.decode('utf-8-sig')); ordinal=0
            for section,text in sections(row,kind):
                for n,chunk in enumerate(chunk_text(text,embedding if scope=='evidence' else None)):
                    if not chunk.strip(): continue
                    ordinal+=1
                    if offset==start_offset and ordinal<=start_chunk: continue
                    yield item_from_row(row,relative,kind,entry['sha256'],0,f'{section}:{n}',chunk,scope), (offset,ordinal)
            if relative=='evaluation/ragcare_cardiology_golden.jsonl':
                context='\n'.join(str(row[k]) for k in ['source_text_zh','source_text_original'] if row.get(k))
                for parent in row.get('relevant_evidence_ids',[]):
                    for n,chunk in enumerate(chunk_text(context,embedding)):
                        ordinal+=1
                        if offset==start_offset and ordinal<=start_chunk: continue
                        clean={k:row[k] for k in ['review_status','medical_truth_verified','translation_status'] if k in row}
                        clean['title']='RAGCare context '+parent
                        yield item_from_row(clean,relative,'benchmark_context',entry['sha256'],0,f'context:{n}',chunk,'benchmark',parent), (offset,ordinal)


def load_verified_corpus(root, embedding=None, inventory=None, snapshot_dir=None, progress=None):
    base = Path(root)
    inventory = inventory if inventory is not None else []
    for path in sorted(base.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        if path.name.startswith('.') or path.suffix != '.jsonl':
            inventory.append({'file': relative, 'indexed': False, 'reason': 'cache_partial_or_non_jsonl'})
            continue
        # Translation writers may replace the completed JSONL while we tokenize it.
        # Capture a stable byte snapshot once; never reopen a moving source for rows.
        snapshot,sha,before=snapshot_file(path,snapshot_dir or base.parent/'runtime/source_snapshots')
        if progress:
            progress({'stage': 'source_snapshot', 'file': relative, 'bytes': before.st_size, 'sha256': sha})
        kind = PRIMARY.get(relative, 'development_fixture')
        scope = 'evidence' if relative in PRIMARY else 'test'
        count = 0
        rows = load_jsonl(snapshot)
        for index, row in enumerate(rows, 1):
            count += 1
            for section, text in sections(row, kind):
                for n, chunk in enumerate(chunk_text(text, embedding if scope == 'evidence' else None)):
                    if chunk.strip():
                        yield item_from_row(row, relative, kind, sha, index, f'{section}:{n}', chunk, scope)
            if relative == 'evaluation/ragcare_cardiology_golden.jsonl':
                context = '\n'.join(str(row[k]) for k in ['source_text_zh', 'source_text_original'] if row.get(k))
                for parent in row.get('relevant_evidence_ids', []):
                    for n, chunk in enumerate(chunk_text(context, embedding)):
                        clean = {k: row[k] for k in ['review_status', 'medical_truth_verified', 'translation_status'] if k in row}
                        clean['title'] = 'RAGCare context ' + parent
                        yield item_from_row(clean, relative, 'benchmark_context', sha, index, f'context:{n}', chunk, 'benchmark', parent)
        after = path.stat()
        changed = before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
        inventory.append({'file': relative, 'sha256': sha, 'rows': count, 'partition': scope, 'indexed': True,
                          'snapshot_file': str(snapshot) if snapshot else None, 'changed_after_snapshot': changed})
        if progress:
            progress({'stage': 'source_adapted', 'file': relative, 'rows': count, 'changed_after_snapshot': changed})
