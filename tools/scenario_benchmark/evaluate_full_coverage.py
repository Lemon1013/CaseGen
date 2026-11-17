#!/usr/bin/env python3
"""Six-category effective113 judge. Offline by default; HTTP requires run --execute."""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import copy
import csv
import io
import json
import os
import sqlite3
from contextlib import closing
import time
from collections import Counter
from pathlib import Path
import evaluate_scenarios as ev
import evaluate_project_scenarios as scoped
from safe_http import (SafeFailure, Redactor, Transport, provider_public, get_provider,
                       inspect_sse_capture, effective_response, LIMIT)

ROOT = ev.ROOT
PROMPT = Path(__file__).with_name('prompts') / 'evaluator_full_coverage_v1.md'
EFFECTIVE_GOLD = ROOT / 'data/evaluation/bidding-v0.1/scope-without-dvp-dst-20261016/baseline.json'
CONTROL_SHA = '6f965c54a81ec60cfa3dc6143eefe30868c50066e8d37bf1d141ac92bd0673cf'
EXCLUDED = {'OPN-020', 'OPN-021', 'CLS-023', 'CLS-024'}
CATEGORIES = scoped.CATEGORIES
REQUIRED = dict(zip(CATEGORIES, [22,14,13,22,26,26]))
VERSION, MODEL, PROVIDER = 'full-coverage-evaluator-1.1', 'gpt-6.1-sol', 'magpie'
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
B_MAPPING_PROJECTION = {
    'version': 'lossless-B-mapping-1',
    'reverse_index': 'Only current targets are transmitted; the full inverse graph is rebuilt from every original A judgment and the complete candidate domain.',
    'A_judgments': 'All original judgment objects remain unchanged, including every matched edge, label, reason, quote and limitation.',
    'A_sources': 'Repeated original source objects are interned by canonical SHA256, with every path/request/attempt/content/wire/response hash retained.',
    'validation': 'Expand sources and reconstruct all candidate inverse edges, then compare the full mapping object exactly before building every request.'}
B_MAPPING_PROJECTION_SHA256 = ev.digest(ev.canonical(B_MAPPING_PROJECTION))
PARAMETERS = {'protocol': 'responses', 'stream': True, 'store': False,
    'schema_compatibility': 'omit-unique-items', 'max_output_tokens': 8192,
    'format_repairs_max': 2, 'transient_retries_max': 2, 'http_attempts_max_per_block': 5,
    'workers': 1, 'reasoning': 'omitted/provider default',
    'sse_validation': 'inspect_sse_capture + strict_assemble_capture; no two-channel relaxation',
    'container_normalization': 'four absent/null root echo fields only; recorded and replayed',
    'request_limit_bytes': DEFAULT_MAX_REQUEST_BYTES, 'response_limit_bytes': LIMIT,
    'B_mapping_projection_sha256': B_MAPPING_PROJECTION_SHA256}
PROJECTION = ('id','category','title','description','test_focus','text',
              'source_task_id','source_point_id','source_checkpoint_id','point_id')


def require(condition, code):
    if not condition:
        raise SafeFailure(code)


def effective_gold():
    original, rows = ev.load(ev.GOLD)['scenarios'], ev.load(EFFECTIVE_GOLD)['scenarios']
    require(len(original) == 117 and len(rows) == 113, 'gold_inventory_mismatch')
    require(rows == [r for r in original if r['id'] not in EXCLUDED], 'effective113_not_exact_original_rows')
    require({r['id'] for r in original} - {r['id'] for r in rows} == EXCLUDED, 'gold_exclusion_mismatch')
    require(Counter(r['category'] for r in rows) == dict(zip(CATEGORIES,[19,14,13,19,22,26])), 'gold_category_inventory')
    return rows


def project_candidates(rows):
    """Declared lossless business text projection; raw objects archived separately."""
    projected = []
    for row in rows:
        require(all(k in row for k in PROJECTION[:6]), 'scene_business_fields_missing')
        projected.append({k: copy.deepcopy(row[k]) for k in PROJECTION if k in row})
    return projected


def make_context(rows, references, input_id):
    gold = effective_gold()
    context = {'input_id': input_id, 'baseline': gold, 'generated_scenarios': project_candidates(rows),
        'reference_material': references, 'task_scope': {'category_ids': CATEGORIES,
            'baseline_ids': [r['id'] for r in gold], 'reference_scope': 'selected_subset',
            'novelty_scope': 'Only effective113 of original117; four DVP/DST excluded; new requires business review.'},
        'generation_provenance': {'source': 'frozen scenario snapshots; business text projection; not proof of raw first model output'}}
    ev.Validator().input(context)
    return context


def load_artifact(path):
    raw=Path(path).read_bytes()
    require(len(raw)<=16*1024*1024,'input_file_limit_exceeded')
    # strict_json preserves duplicate-key/number rejection while permitting API list roots.
    return ev.strict_json('{"value":'+raw.decode('utf-8')+'}')['value']


def artifact_path(parent, value):
    path = Path(value)
    path = path if path.is_absolute() else parent / path
    require(not path.is_symlink(), 'artifact_symlink_refused')
    path = path.resolve()
    require(path.is_file(), 'artifact_missing')
    return path


def read_frozen_retrievals(database, checkpoints):
    """One read transaction; exact frozen checkpoint rows and seven allowed columns only."""
    database = Path(database).resolve()
    require(database.is_file(), 'frozen_database_missing')
    columns = {'id','task_id','attempt','version','status','retrieval_json','selected_citation_ids_json'}
    def authorize(action, arg1, arg2, db, trigger):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if arg1 == 'task_retrieval_checkpoints' and arg2 in columns else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_TRANSACTION) else sqlite3.SQLITE_DENY
    result = {}
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.set_authorizer(authorize)
        conn.execute('BEGIN')
        try:
            for cp in checkpoints:
                row = conn.execute('SELECT id,task_id,attempt,version,status,retrieval_json,selected_citation_ids_json '
                    'FROM task_retrieval_checkpoints WHERE id=? AND task_id=?',(cp['id'],cp['task_id'])).fetchone()
                require(row is not None and tuple(row[:5]) == tuple(cp[k] for k in ('id','task_id','attempt','version','status')),
                    'frozen_database_checkpoint_identity_mismatch')
                snapshot = ev.strict_json(row[5]); selected = json.loads(row[6])
                require(selected == cp['selected_citation_ids'] and snapshot.get('evidence') == cp['evidence'],
                    'frozen_database_selection_or_evidence_mismatch')
                result[cp['id']] = {'database_path':str(database),'row_sha256':ev.digest(ev.canonical(list(row))),
                    'retrieval':snapshot,'selected_citation_ids':selected}
        finally:
            conn.rollback()
    return result


def selected_references(checkpoint, task_id, frozen_context=None):
    """Selected original slices only; dependencies must all be explicitly confirmed."""
    require(checkpoint['status']=='confirmed' and checkpoint['task_id']==task_id, 'retrieval_not_confirmed')
    selected_ids = set(checkpoint['selected_citation_ids'])
    require(len(selected_ids)==len(checkpoint['selected_citation_ids']), 'duplicate_selected_citation_identity')
    candidates = {r['id']:r for r in checkpoint['candidate_citations']}
    require(selected_ids and selected_ids <= set(candidates), 'selected_citation_not_candidate')
    evidence = checkpoint['evidence']
    require(evidence.get('status')=='complete', 'frozen_evidence_not_complete')
    refs, covered, checklist_ids = [], set(), set()
    manifests = evidence['manifests']
    for manifest in manifests:
        require(manifest['status']=='complete' and manifest['document_id'] in (3,4), 'frozen_document_incomplete_or_outside_campaign')
        text = manifest['full_text']
        require(ev.digest(text)==manifest['full_text_hash'], 'frozen_document_text_hash_mismatch')
        if not manifest.get('checklist'):
            continue
        require(manifest.get('line_inventory_version')==1, 'frozen_inventory_version_required')
        checklist_ids.update(manifest['candidate_citation_ids'])
        require(manifest['space_id']==3, 'frozen_manifest_space_mismatch')
        inventory = manifest['line_inventory']
        by_id = {r['id']:r for r in inventory}
        require(len(by_id)==len(inventory), 'duplicate_frozen_line_identity')
        items = {r['id']:r for r in manifest.get('items',[])}
        require(len(items)==len(manifest.get('items',[])), 'duplicate_frozen_entry_identity')
        def confirmed(line):
            cids = set(line.get('candidate_citation_ids',[]))
            return bool(cids) and cids <= selected_ids and line.get('selected') is True and set(line.get('selected_citation_ids',[]))==cids
        roots = [r['id'] for r in inventory if r.get('selected') is True and r.get('role') in ('prose','entry','entry_continuation')]
        selected = set()
        def visit(rid, active):
            require(rid in by_id, 'frozen_context_dependency_missing')
            require(rid not in active, 'frozen_context_dependency_cycle')
            if rid in selected:
                return
            line = by_id[rid]
            require(confirmed(line), 'frozen_dependency_not_confirmed')
            deps = list(line.get('context_line_ids',[]))
            if line.get('role') in ('entry','entry_continuation'):
                entry_id = line.get('entry_id')
                require(entry_id in items and confirmed(items[entry_id]), 'frozen_entry_dependency_not_confirmed')
                item = items[entry_id]
                require(item['line_id'] in by_id, 'frozen_entry_line_missing')
                if line.get('role') == 'entry':
                    require(item['line_id']==rid, 'frozen_entry_line_identity_mismatch')
                    deps += item.get('context_line_ids',[])
            for dep in dict.fromkeys(deps):
                visit(dep,active|{rid})
            selected.add(rid)
        for rid in roots:
            visit(rid,set())
        for rid in sorted(selected,key=lambda key:by_id[key]['start_char']):
            line = by_id[rid]; start,end=line['start_char'],line['end_char']
            require(type(start) is int and type(end) is int and 0<=start<end<=len(text), 'frozen_line_range_missing')
            fragment = text[start:end]
            # Keep the original slice including CR/LF; only verify the declared line convention.
            require(fragment.rstrip('\r\n')==line['text'], 'frozen_line_slice_mismatch')
            cids = sorted(line['candidate_citation_ids']); covered.update(cids)
            refs.append({'document_id':manifest['document_id'],'range':[start,end],'text':fragment,
                'task_id':task_id,'retrieval_checkpoint_id':checkpoint['id'],'line_id':rid,
                'source_id':manifest['source_key'],'citation_ids':cids,
                'document_full_text_sha256':manifest['full_text_hash']})
    remaining = selected_ids-checklist_ids
    context = frozen_context or {}
    for kind in ('source_hits','wiki_hits','external_hits'):
        for hit in context.get(kind,[]):
            cid=hit.get('task_citation_id')
            if cid not in remaining or cid in covered:
                continue
            text=hit.get('content') or hit.get('text') or hit.get('body')
            require(isinstance(text,str) and text, 'selected_frozen_hit_full_text_missing')
            citation=candidates[cid]
            require(hit['space_id']==3, 'selected_frozen_hit_space_mismatch')
            if kind=='source_hits':
                chunk_id=hit.get('source_chunk_id',hit.get('id'))
                require(citation['citation_type']=='source' and chunk_id==citation['source_chunk_id'], 'selected_frozen_chunk_identity_mismatch')
                docs=[m for m in manifests if chunk_id in m.get('source_chunk_ids',[])]
                require(len(docs)<=1, 'selected_frozen_chunk_document_ambiguous')
                start,end=hit['start_char'],hit['end_char']
                require(type(start) is int and type(end) is int and start>=0 and end-start==len(text), 'selected_frozen_chunk_range_mismatch')
                document_id=hit['document_id']; source_key='document:'+str(document_id)
                require(document_id in (3,4), 'selected_frozen_document_outside_campaign')
                if docs:
                    doc=docs[0]
                    require(doc['document_id']==document_id and doc['full_text'][start:end]==text, 'selected_frozen_chunk_text_mismatch')
                    source_key=doc.get('source_key',source_key)
                require(not hit.get('text') or hit['text']==text, 'selected_frozen_hit_content_text_mismatch')
            else:
                page_id=hit.get('page_id',hit.get('id'))
                require(page_id is not None, 'selected_frozen_page_identity_missing')
                require(kind!='wiki_hits' or hit.get('source_document_id') in (3,4)
                    or bool(hit.get('source_document_ids')) and set(hit['source_document_ids']) <= {3,4}, 'selected_frozen_wiki_document_mismatch')
                require(kind!='wiki_hits' or (citation['citation_type']=='wiki' and page_id==citation['wiki_page_id']), 'selected_frozen_wiki_identity_mismatch')
                document_id=kind+':'+str(page_id); start,end=0,len(text); source_key=document_id

            refs.append({'document_id':document_id,'range':[start,end],'text':text,'task_id':task_id,
                'retrieval_checkpoint_id':checkpoint['id'],'source_id':source_key,'citation_ids':[cid], 'frozen_hit_sha256':ev.digest(ev.canonical(hit)),
                'source_identity':{k:hit[k] for k in ('id','page_id','source_chunk_id','chunk_id','space_id','document_id','source_document_id','source_document_ids','revision','status') if k in hit}})
            covered.add(cid)
    require(not remaining-covered, 'selected_nonchecklist_text_requires_frozen_database_snapshot')
    require(refs, 'frozen_selected_source_text_required')
    return refs


def campaign_context(manifest_path, input_id, database=None):
    manifest_path = Path(manifest_path).resolve()
    manifest = ev.load(manifest_path)
    require(manifest.get('all_frozen') is True, 'six_frozen_chapters_required')
    chapters = manifest.get('chapters', manifest.get('frozen_paths'))
    require(isinstance(chapters,list) and len(chapters) == 6, 'six_frozen_paths_required')
    sources = {str(manifest_path): ev.sha(manifest_path)}
    rows, categories, snapshots, refs, dependencies = [], {}, [], {}, {}
    seen_tasks, seen_categories = set(), set()
    for chapter in chapters:
        pointer = chapter if isinstance(chapter,str) else chapter['frozen_path']
        frozen_path = artifact_path(manifest_path.parent, pointer)
        require(frozen_path.is_relative_to(manifest_path.parent), 'frozen_path_outside_campaign')
        frozen = ev.load(frozen_path)
        sources[str(frozen_path)] = ev.sha(frozen_path)
        category = frozen['key']
        require(not isinstance(chapter,dict) or chapter['key']==category, 'summary_chapter_identity_mismatch')
        task_id = frozen['task_id']
        require(category in CATEGORIES and category not in seen_categories, 'chapter_category_duplicate_or_unknown')
        require(type(task_id) is int and task_id > 19 and task_id not in seen_tasks, 'new_distinct_task_required')
        require(frozen['project_id'] == 4 and frozen['space_id'] == 3, 'campaign_project_space_mismatch')
        seen_categories.add(category); seen_tasks.add(task_id)
        values = {}
        for name in ('proposal','retrieval_checkpoint','retrieval_selection','citations'):
            artifact = frozen['artifacts'][name]
            path = artifact_path(frozen_path.parent,artifact['path'])
            require(path.is_relative_to(frozen_path.parent), 'artifact_path_outside_chapter')
            require(ev.sha(path) == artifact['sha256'], 'frozen_artifact_hash_mismatch')
            sources[str(path)] = ev.sha(path)
            values[name] = load_artifact(path)
        proposal = values['proposal']
        points = proposal.get('points',proposal.get('scenarios'))
        require(isinstance(points,list) and points, 'proposal_points_missing')
        checkpoint = values['retrieval_checkpoint']
        selection = values['retrieval_selection']
        require(proposal['task_id']==task_id and proposal['id']==frozen['checkpoint_id']
            and proposal['version']==frozen['version'] and proposal['status']=='pending'
            and proposal['retrieval_checkpoint_id']==frozen['retrieval_checkpoint_id']==checkpoint['id'], 'proposal_frozen_identity_mismatch')
        require(checkpoint['task_id']==task_id and checkpoint['status']=='confirmed'
            and checkpoint['id']==selection['checkpoint_id'] and checkpoint['version']==selection['expected_version']
            and checkpoint['selected_citation_ids']==selection['selected_citation_ids'], 'retrieval_selection_identity_mismatch')
        require(frozen['required_expected']==REQUIRED[category] and frozen['required_actual']==REQUIRED[category], 'required_inventory_mismatch')
        citations = values['citations']
        citations = citations if isinstance(citations,list) else citations.get('items',citations.get('citations',[]))
        citation_map = {r['id']:r for r in citations}
        citation_ids = set(citation_map)
        for citation in checkpoint['candidate_citations']:
            if citation['id'] in checkpoint['selected_citation_ids']:
                require(citation['id'] in citation_map and all(citation.get(k)==citation_map[citation['id']].get(k)
                    for k in ('citation_type','source_chunk_id','wiki_page_id')), 'selected_citation_identity_changed')
        require(set(checkpoint['selected_citation_ids']) <= citation_ids, 'selected_citation_not_in_task_snapshot')
        snapshots.append({'category': category,'task_id': task_id,'frozen': frozen,'artifacts': values})
        for point in points:
            pid, title = point['id'], point['title']
            goal = point.get('verification_goal',point.get('goal'))
            require(type(pid) is int and isinstance(title,str) and isinstance(goal,str), 'real_point_id_title_goal_required')
            require(point['task_id']==task_id and point['checkpoint_id']==proposal['id'], 'point_task_checkpoint_mismatch')
            require(set(point['citation_ids']) <= set(checkpoint['selected_citation_ids']), 'point_unconfirmed_citation_dependency')
            gid = f'TP-{task_id}-{pid}'
            require(gid not in categories, 'duplicate_real_point_identity')
            rows.append({'id':gid,'category':None,'title':title,'description':goal,'test_focus':goal,
                'text':title+'\n'+goal,'source_task_id':task_id,'source_point_id':pid,
                **({'source_checkpoint_id':point['checkpoint_id']} if 'checkpoint_id' in point else {})})
            categories[gid] = category
    database_rows = read_frozen_retrievals(database,[s['artifacts']['retrieval_checkpoint'] for s in snapshots]) if database else {}
    for chapter in snapshots:
        checkpoint = chapter['artifacts']['retrieval_checkpoint']; task_id=chapter['task_id']
        dbrow = database_rows.get(checkpoint['id'])
        if dbrow:
            chapter['frozen_database_row'] = dbrow
        for source in selected_references(checkpoint,task_id,dbrow['retrieval'].get('context') if dbrow else None):
            key = ev.canonical([source['document_id'],source['range']])
            if key in refs:
                require(refs[key]['text']==source['text'], 'same_document_range_text_mismatch')
            else:
                refs[key]={'source_id':'frozen:'+ev.digest(key)[:24],
                    'source':'New campaign frozen selected retrieval evidence','text':source['text']}
                dependencies[key]=[]
            dependencies[key].append(source)
    context = make_context(rows,list(refs.values()),input_id)
    snapshot = {'manifest':manifest,'chapters':snapshots,'reference_provenance':[
        {'reference_source_id':refs[k]['source_id'],'document_range_key':k,'dependencies':dependencies[k]} for k in refs]}
    return context,snapshot,categories,sources


def public_provider():
    public = provider_public(PROVIDER)
    return {**public,'endpoint':public['endpoint'].removesuffix('/chat/completions')+'/responses'}


def source_hashes(extra=None):
    paths = [Path(__file__),PROMPT,ev.PROMPT,scoped.PROMPT,Path(ev.__file__),Path(scoped.__file__),
        Path(__file__).with_name('safe_http.py'),Path(__file__).with_name('sse_fidelity.py'),
        ev.GOLD,EFFECTIVE_GOLD,ev.ASSETS/'judgment.schema.json',ev.ASSETS/'.qa/validate_assets.py']
    return {**{str(p.resolve()):ev.sha(p) for p in paths},**(extra or {})}


def require_request_limit(value):
    require(type(value) is int and DEFAULT_MAX_REQUEST_BYTES <= value <= MAX_REQUEST_BYTES,
        'request_limit_out_of_bounds')
    return value


def restore_mapping(projected,context,targets):
    """Expand the exact transmitted B graph; reject altered sources/target edges/representation."""
    require(set(projected)=={'origin','candidate_to_baseline','baseline_judgments','sources_by_sha256','projection_sha256'},
        'B_mapping_projected_fields_changed')
    require(projected['projection_sha256']==B_MAPPING_PROJECTION_SHA256, 'B_mapping_projection_changed')
    sources=projected['sources_by_sha256']
    require(all(ev.digest(ev.canonical(source))==key for key,source in sources.items()), 'B_mapping_source_hash_changed')
    inverse={r['id']:[] for r in context['generated_scenarios']}
    judgments,used=[],set()
    for entry in projected['baseline_judgments']:
        require(set(entry)=={'judgment','source_sha256'}, 'B_mapping_projected_entry_changed')
        source_sha=entry['source_sha256']
        require(source_sha in sources, 'B_mapping_source_missing')
        used.add(source_sha)
        judgment=entry['judgment']
        judgments.append({'judgment':judgment,'source':sources[source_sha]})
        for gid in judgment['matched_generated_ids']:
            require(gid in inverse, 'B_mapping_unknown_candidate')
            inverse[gid].append(judgment['baseline_id'])
    require(used==set(sources), 'B_mapping_orphan_source')
    baseline_ids=[r['id'] for r in context['baseline']]
    is_sizing=projected['origin']=='preflight sizing only; never accepted judgment'
    require([r['judgment']['baseline_id'] for r in judgments]==baseline_ids or (is_sizing and not judgments),
        'B_mapping_A_not_complete')
    require(projected['candidate_to_baseline']=={gid:inverse[gid] for gid in targets}, 'B_mapping_target_edges_changed')
    return {'origin':projected['origin'],'candidate_to_baseline':inverse,'baseline_judgments':judgments}


def project_request(data):
    """Lossless transport-only B metadata projection; local validator keeps the full mapping."""
    if data['stage']!='B':
        return data
    mapping=data['a_mapping']
    require(set(mapping)=={'origin','candidate_to_baseline','baseline_judgments'}, 'B_mapping_projection_unknown_field')
    sources,judgments={},[]
    for entry in mapping['baseline_judgments']:
        require(set(entry)=={'judgment','source'}, 'B_mapping_entry_unknown_field')
        source_sha=ev.digest(ev.canonical(entry['source']))
        if source_sha in sources:
            require(sources[source_sha]==entry['source'], 'B_mapping_source_hash_collision')
        sources[source_sha]=entry['source']
        judgments.append({'judgment':entry['judgment'],'source_sha256':source_sha})
    projected=dict(data)
    projected['a_mapping']={'origin':mapping['origin'],
        'candidate_to_baseline':{gid:mapping['candidate_to_baseline'][gid] for gid in data['targets']},
        'baseline_judgments':judgments,'sources_by_sha256':sources,
        'projection_sha256':B_MAPPING_PROJECTION_SHA256}
    # Rebuild FROM the outgoing representation, not from aliases of the full local graph.
    require(restore_mapping(projected['a_mapping'],data['context'],data['targets'])==mapping,
        'B_mapping_lossless_rebuild_mismatch')
    return projected


def body_for(data,schema,prompt,repair=None,max_request_bytes=DEFAULT_MAX_REQUEST_BYTES):
    require_request_limit(max_request_bytes)
    repair = repair or {}
    projection_note = ('\n\nSealed lossless B transport metadata projection (business rules unchanged):\n'
        + ev.canonical(B_MAPPING_PROJECTION)) if data['stage']=='B' else ''
    messages = ev.messages_for(project_request(data),prompt+projection_note,schema,repair.get('prior'),repair.get('errors'))
    body = ev.body_for(MODEL,messages,schema,'json_schema',8192,'responses','omit-unique-items',True)
    require('reasoning' not in body,'unexpected_reasoning_override')
    require(len(json.dumps(body,ensure_ascii=False,allow_nan=False).encode()) <= max_request_bytes,'request_size_limit')
    return body


def prepare(input_path,output,public=None,target_size=5,campaign=False,control_path=None,input_id='full-coverage',database=None,max_request_bytes=DEFAULT_MAX_REQUEST_BYTES):
    require(type(target_size) is int and 1 <= target_size <= 5,'target_size_out_of_bounds')
    require_request_limit(max_request_bytes)
    output = Path(output)
    require(not output.exists() and not output.is_symlink(),'output_exists')
    path = Path(input_path).resolve()
    if campaign:
        context,snapshot,categories,extra = campaign_context(path,input_id,database)
    else:
        raw = ev.load(path)
        require('generated_scenarios' in raw or 'scenarios' in raw,'generic_scene_or_context_required')
        rows = raw.get('generated_scenarios',raw.get('scenarios'))
        refs = [{k:r[k] for k in ('source_id','source','text')} for r in raw.get('reference_material',[])]
        if 'baseline' in raw:
            require(raw['baseline'] == effective_gold(),'generic_context_gold_changed')
        context = make_context(rows,refs,raw.get('input_id',input_id))
        snapshot,categories = raw,raw.get('report_categories',{})
        extra = {str(path):ev.sha(path)}
    if control_path:
        control_path = Path(control_path).resolve()
        require(ev.sha(control_path) == CONTROL_SHA,'original120_control_hash_mismatch')
        control = ev.load(control_path)
        require(len(control['scenarios']) == 120,'control120_inventory_mismatch')
        context = make_context(control['scenarios'],context['reference_material'],input_id+'-control')
        categories = {r['id']:dict(zip(scoped.SCOPES,CATEGORIES)).get(r.get('task_scope')) for r in control['scenarios']}
        snapshot = {'reference_input_snapshot':snapshot,'control_original':control}
        extra[str(control_path)] = ev.sha(control_path)
    validate_context(context)
    require(all(v in CATEGORIES or v is None for v in categories.values()),'report_category_invalid')
    prompt = PROMPT.read_text(encoding='utf-8')
    require(prompt.endswith(ev.PROMPT.read_text(encoding='utf-8')),'original_business_prompt_not_verbatim')
    spec = {'version':VERSION,'model':MODEL,'provider':PROVIDER,'parameters':{**PARAMETERS,'target_size':target_size,'request_limit_bytes':max_request_bytes},
        'public_provider':public or public_provider(),'source_inputs':extra,'sources':source_hashes(extra),
        'input_canonical_sha256':ev.digest(ev.canonical(context)),'targets':scoped.target_plan(context,target_size),
        'snapshot_canonical_sha256':ev.digest(ev.canonical(snapshot)),'report_categories':categories,
        'B_mapping_projection':B_MAPPING_PROJECTION,
        'projection':list(PROJECTION),'control_original_sha256':CONTROL_SHA if control_path else None,
        'B_mapping_notice':'B reverses real A links; not independent blind evaluation',
        'comparison_notice':'Generation inputs differ; not single-variable A/B or proof of regulatory correctness'}
    validator = ev.Validator()
    mapping = {'origin':'preflight sizing only; never accepted judgment',
        'candidate_to_baseline':{r['id']:[] for r in context['generated_scenarios']},
        'baseline_judgments':[]}
    for target in spec['targets']:
        request = scoped.request_data(context,target,mapping if target['stage']=='B' else None)
        body_for(request,scoped.scoped_schema(request,validator.legacy_schema),prompt,max_request_bytes=max_request_bytes)
    spec['plan_sha256'] = ev.digest(ev.canonical(spec))
    output.mkdir(parents=True)
    ev.save(output/'plan.json',spec); ev.save(output/'context.json',context)
    ev.save(output/'source-snapshot.json',snapshot); ev.save(output/'prompt.md',prompt,text=True)
    for target in spec['targets']:
        if target['stage'] != 'A':
            continue
        request = scoped.request_data(context,target)
        schema = scoped.scoped_schema(request,validator.legacy_schema)
        directory = output/'dry-run-requests'/target['request_id']
        ev.save(directory/'input.json',request); ev.save(directory/'schema.json',schema)
        ev.save(directory/'request.json',body_for(request,schema,prompt,max_request_bytes=max_request_bytes))
    ev.save(output/'dry-run.json',{'status':'dry_run','http_count':0,'credential_reads':0,
        'A_blocks':sum(t['stage']=='A' for t in spec['targets']),'B_blocks':sum(t['stage']=='B' for t in spec['targets']),
        'B_requests_deferred_until_all_A_real_and_valid':True,'effective_gold':113,
        'request_limit_bytes':max_request_bytes,'response_limit_bytes':LIMIT,
        'B_mapping_projection_sha256':B_MAPPING_PROJECTION_SHA256,
        'preflight':'A exact initial bodies and B context/schema lower-bound sizing with no accepted A graph; actual full A graph and every repair are rechecked before HTTP'})
    return spec


def validate_context(context):
    require(context['baseline'] == effective_gold(),'effective113_context_mismatch')
    require(context['task_scope']['category_ids'] == CATEGORIES and
        context['task_scope']['reference_scope'] == 'selected_subset','six_category_selected_subset_required')
    require(context['generated_scenarios'] == project_candidates(context['generated_scenarios']),'unprojected_candidate_metadata')
    ev.Validator().input(context)


def validate_prepared(output):
    output = Path(output)
    require(not output.is_symlink(),'symlink_run_refused')
    spec,context = ev.load(output/'plan.json'),ev.load(output/'context.json')
    unsigned = {k:v for k,v in spec.items() if k != 'plan_sha256'}
    require(spec['plan_sha256'] == ev.digest(ev.canonical(unsigned)),'sealed_plan_changed')
    size = spec['parameters']['target_size']
    request_limit = require_request_limit(spec['parameters']['request_limit_bytes'])
    require(spec['B_mapping_projection']==B_MAPPING_PROJECTION, 'B_mapping_projection_changed')
    require(type(size) is int and 1 <= size <= 5 and spec['parameters'] == {**PARAMETERS,'target_size':size,'request_limit_bytes':request_limit}
        and spec['version']==VERSION and spec['model']==MODEL and spec['provider']==PROVIDER,'sealed_parameters_changed')
    require(spec['sources'] == source_hashes({p:ev.sha(p) for p in spec['source_inputs']}),'source_hash_changed')
    require(ev.digest(ev.canonical(context)) == spec['input_canonical_sha256'],'context_changed')
    require(ev.digest(ev.canonical(ev.load(output/'source-snapshot.json'))) == spec['snapshot_canonical_sha256'],'snapshot_changed')
    require(spec['targets'] == scoped.target_plan(context,size),'target_plan_changed')
    require((output/'prompt.md').read_text() == PROMPT.read_text(),'prompt_changed')
    validate_context(context)
    return spec,context


class Runner:
    def __init__(self, output, transport=None):
        self.output = Path(output)
        self.spec, self.context = validate_prepared(output)
        self.validator = ev.Validator()
        self.prompt = (self.output / 'prompt.md').read_text()
        self.transport = transport
        self.redactor = getattr(transport, 'redactor', Redactor())
        self.cancel = False

    def decode(self, response, data, schema):
        require(response.get('http_status') == 200 and not response.get('error')
                and not response.get('redaction_applied'), response.get('error') or 'non_success_HTTP')
        require(response.get('captured_bytes', 0) <= LIMIT, 'response_size_limit')
        raw = response.get('eventmeta', {}).get('raw_events_text', '')
        require(len(raw.encode('utf-8')) <= LIMIT, 'response_size_limit')
        inspect_sse_capture(response)
        scoped.strict_assemble_capture(response)
        native = effective_response(response)
        ev.validate_response_model(native, MODEL)
        content, finish = ev.parse_content(native, 'responses')
        require(finish == 'stop', 'SSE_not_complete')
        return content

    def audit_attempt(self, directory, data, prior=None, errors=None):
        """Recompute request, strict raw SSE -> original text -> normalized wire."""
        schema = scoped.scoped_schema(data, self.validator.legacy_schema)
        repair = {'prior': prior, 'errors': errors} if prior is not None else {}
        require(ev.load(directory / 'input.json') == data, 'archived_input_changed')
        require(ev.load(directory / 'schema.json') == schema, 'archived_schema_changed')
        require(ev.load(directory / 'repair.json') == repair, 'archived_repair_changed')
        require(ev.load(directory / 'request.json') == body_for(data, schema, self.prompt, repair, self.spec['parameters']['request_limit_bytes']), 'archived_request_changed')
        require(ev.load(directory / 'messages.json') == {'input': ev.load(directory / 'request.json')['input']}, 'messages_changed')
        started = ev.load(directory / 'started.json')
        require(started['request_sha256'] == ev.sha(directory / 'request.json'), 'request_hash_changed')
        if not (directory / 'status.json').exists():
            return None, {'status': 'evaluation_failed', 'failure_kind': 'interrupted', 'errors': ['interrupted_HTTP_unknown_billing']}
        state = ev.load(directory / 'status.json')
        response = ev.load(directory / 'response.safe.json')
        require(state['response_sha256'] == ev.sha(directory / 'response.safe.json'), 'response_hash_changed')
        require(state['usage'] == ev.usage_for(effective_response(response), 'responses'), 'archived_usage_changed')
        require(self.redactor.clean(response) == response, 'archived_sensitive_response')
        if isinstance(response.get('eventmeta'), dict):
            require((directory / 'response.sse').read_text() == response['eventmeta']['raw_events_text'], 'raw_SSE_changed')
        if response.get('http_status') == 401 or ev.quota_exhausted(response):
            self.cancel = True
        if state['status'] != 'success':
            require(state['retryable'] == ev.retryable(response), 'retryable_changed')
            if state['failure_kind'] == 'format':
                content = self.decode(response, data, schema)
                require(content == (directory / 'content.original.txt').read_text(), 'original_content_changed')
                reproduced = None
                try:
                    wire, filled = scoped.normalize_container_fields(ev.strict_json(content.strip()), data)
                    scoped.validate_scoped(data, wire, schema, self.validator)
                except (ValueError, SafeFailure) as exc:
                    reproduced = str(exc)
                require(reproduced is not None and state['errors'] == [reproduced], 'format_failure_changed')
            else:
                reproduced = None
                try:
                    self.decode(response, data, schema)
                except (ValueError, SafeFailure) as exc:
                    reproduced = str(exc)
                require(reproduced is not None and state['errors'] == [reproduced], 'technical_failure_changed')
            return None, state
        content = self.decode(response, data, schema)
        require(content == (directory / 'content.original.txt').read_text(), 'original_content_changed')
        wire, filled = scoped.normalize_container_fields(ev.strict_json(content.strip()), data)
        require(wire == ev.load(directory / 'wire.json') and filled == state['container_fields_filled'], 'wire_or_container_fill_changed')
        scoped.validate_scoped(data, wire, schema, self.validator)
        require(state['wire_sha256'] == ev.digest(ev.canonical(wire)), 'wire_hash_changed')
        origin = {'stage': data['stage'], 'request_id': data['request_id'], 'attempt': directory.name,
            'path': str(directory.relative_to(self.output)), 'content_sha256': ev.digest(content),
            'wire_sha256': state['wire_sha256'], 'response_sha256': state['response_sha256']}
        require(ev.load(directory / 'mapping.json') == origin, 'origin_changed')
        require(ev.load(directory / 'validation.json') == {'status': 'passed', 'container_fields_filled': filled,
            'strict_SSE_replayed': True, 'scoped_validation': True, 'business_semantics_judged_by_model': True}, 'validation_changed')
        return {'wire': wire, 'origin': origin}, state

    def block(self, target, mapping=None):
        data = scoped.request_data(self.context, target, mapping)
        root = self.output / 'requests' / target['request_id']
        prior, errors, formats, transients = None, None, 0, 0
        directories = sorted(root.glob('attempt-*'))
        for directory in directories:
            entry, state = self.audit_attempt(directory, data, prior, errors)
            if entry:
                return entry
            if state['failure_kind'] == 'format':
                formats += 1
                prior, errors = (directory / 'content.original.txt').read_text(), state['errors']
            elif state.get('retryable') or state['failure_kind'] == 'interrupted':
                transients += 1
            else:
                return None
        if self.cancel or formats > 2 or transients > 2:
            return None
        while len(directories) < 5 and formats <= 2 and transients <= 2 and not self.cancel:
            require(self.transport is not None, 'execute_transport_required')
            directory = root / f'attempt-{len(directories) + 1:03d}'
            schema = scoped.scoped_schema(data, self.validator.legacy_schema)
            repair = {'prior': prior, 'errors': errors} if prior is not None else {}
            body = body_for(data, schema, self.prompt, repair, self.spec['parameters']['request_limit_bytes'])
            require(self.redactor.clean(body) == body, 'request_sensitive_content')
            ev.save(directory / 'input.json', data)
            ev.save(directory / 'schema.json', schema)
            ev.save(directory / 'repair.json', repair)
            ev.save(directory / 'messages.json', {'input': body['input']})
            ev.save(directory / 'request.json', body)
            ev.save(directory / 'started.json', {'pid': os.getpid(), 'request_sha256': ev.sha(directory / 'request.json')})
            try:
                response = self.transport(body)
                self.redactor = getattr(self.transport, 'redactor', self.redactor)
            except Exception as exc:
                response = {'http_status': None, 'body': None, 'error': 'transport_' + type(exc).__name__}
            if self.redactor.clean(response) != response:
                response = self.redactor.clean(response)
                response['error'] = 'credential_echo_redacted'
            ev.save(directory / 'response.safe.json', response)
            if isinstance(response.get('eventmeta'), dict):
                ev.save(directory / 'response.sse', response['eventmeta']['raw_events_text'], text=True)
            state = {'status': 'evaluation_failed', 'failure_kind': 'technical', 'errors': [],
                'retryable': ev.retryable(response), 'response_sha256': ev.sha(directory / 'response.safe.json'),
                'container_fields_filled': [], 'usage': ev.usage_for(effective_response(response), 'responses')}
            content = None
            try:
                content = self.decode(response, data, schema)
                ev.save(directory / 'content.original.txt', content, text=True)
                state['failure_kind'] = 'format'
                wire, filled = scoped.normalize_container_fields(ev.strict_json(content.strip()), data)
                state['container_fields_filled'] = filled
                scoped.validate_scoped(data, wire, schema, self.validator)
                state.update(status='success', failure_kind=None, wire_sha256=ev.digest(ev.canonical(wire)))
                ev.save(directory / 'wire.json', wire)
                ev.save(directory / 'mapping.json', {'stage': data['stage'], 'request_id': data['request_id'],
                    'attempt': directory.name, 'path': str(directory.relative_to(self.output)),
                    'content_sha256': ev.digest(content), 'wire_sha256': state['wire_sha256'], 'response_sha256': state['response_sha256']})
            except Exception as exc:
                state['errors'] = [str(exc) if isinstance(exc, (SafeFailure, ValueError)) else 'local_' + type(exc).__name__]
            ev.save(directory / 'validation.json', {'status': 'passed' if state['status'] == 'success' else 'failed',
                'container_fields_filled': state['container_fields_filled'], 'strict_SSE_replayed': state['status'] == 'success',
                'scoped_validation': state['status'] == 'success', 'business_semantics_judged_by_model': True})
            ev.save(directory / 'status.json', state, self.redactor)
            directories.append(directory)
            entry, checked = self.audit_attempt(directory, data, prior, errors)
            if entry:
                return entry
            if self.cancel:
                return None
            if state['failure_kind'] == 'format':
                formats += 1
                prior, errors = content, state['errors']
            elif state['retryable']:
                transients += 1
                if transients <= 2:
                    time.sleep(transients)
            else:
                return None
        return None

    def audit(self):
        accepted = []
        for target in self.spec['targets']:
            if target['stage'] == 'B':
                if sum(len(e['wire'].get('baseline_judgments', {})) for e in accepted) != 113:
                    break
                mapping = scoped.inverse_mapping(self.context, [e for e in accepted if e['origin']['stage'] == 'A'])
            else:
                mapping = None
            data = scoped.request_data(self.context, target, mapping)
            prior, errors = None, None
            directories = sorted((self.output / 'requests' / target['request_id']).glob('attempt-*'))
            require(len(directories) <= 5 and [p.name for p in directories] ==
                    [f'attempt-{i:03d}' for i in range(1, len(directories) + 1)], 'attempt_history_sequence_or_budget')
            formats, transients = 0, 0
            for index, directory in enumerate(directories):
                require(formats <= 2 and transients <= 2, 'historical_retry_budget_exceeded')
                entry, state = self.audit_attempt(directory, data, prior, errors)
                if entry:
                    require(index == len(directories) - 1, 'attempt_after_success')
                    accepted.append(entry)
                    break
                if state['failure_kind'] == 'format':
                    formats += 1
                    prior, errors = (directory / 'content.original.txt').read_text(), state['errors']
                elif state.get('retryable') or state['failure_kind'] == 'interrupted':
                    transients += 1
                else:
                    require(index == len(directories) - 1, 'attempt_after_terminal_failure')
        assembled = None
        if len(accepted) == len(self.spec['targets']):
            assembled = scoped.assembled_result(self.context, accepted, self.validator)
        if (self.output / 'assembled.json').exists():
            require(assembled is not None and ev.load(self.output / 'assembled.json') == assembled, 'assembled_changed')
        return accepted, assembled


def report_files(directory, runner, accepted, assembled, status):
    maps = {'baseline': {}, 'generated': {}}
    for entry in accepted:
        for kind in maps:
            maps[kind].update(entry['wire'].get(kind+'_judgments', {}))
    baseline_rows = [{'id':r['id'], 'category':r['category'], 'judgment':maps['baseline'].get(r['id']),
        'technical_status':'assessed' if r['id'] in maps['baseline'] else 'technical_unassessed'} for r in runner.context['baseline']]
    candidate_rows = [{'id':r['id'], 'category':runner.spec['report_categories'].get(r['id'],r.get('category')),
        'source_task_id':r.get('source_task_id'), 'source_point_id':r.get('source_point_id'),
        'title':r['title'], 'text':r['text'], 'judgment':maps['generated'].get(r['id']),
        'technical_status':'assessed' if r['id'] in maps['generated'] else 'technical_unassessed'} for r in runner.context['generated_scenarios']]
    def counts(rows, field):
        return dict(Counter(r['judgment'][field] for r in rows if r['judgment']))
    def summarize(brows, grows):
        return {'baseline_denominator':len(brows), 'candidate_denominator':len(grows),
            'assessed_A':sum(r['judgment'] is not None for r in brows), 'assessed_B':sum(r['judgment'] is not None for r in grows),
            'coverage':counts(brows,'coverage'), 'baseline_business_consistency':counts(brows,'business_consistency'), 'relations':counts(grows,'relation'),
            'candidate_business_consistency':counts(grows,'business_consistency'), 'granularity':counts(grows,'granularity')}
    report = {'status':status, 'partial':assembled is None, 'input_id':runner.context['input_id'],
        'baseline_denominator':113, 'candidate_denominator':len(candidate_rows),
        'request_limit_bytes':runner.spec['parameters']['request_limit_bytes'], 'response_limit_bytes':LIMIT,
        'B_mapping_projection_sha256':B_MAPPING_PROJECTION_SHA256,
        'accepted_A_rows':len(maps['baseline']), 'accepted_B_rows':len(maps['generated']),
        'baseline_technical_unassessed':113-len(maps['baseline']),
        'candidate_technical_unassessed':len(candidate_rows)-len(maps['generated']),
        'overall':summarize(baseline_rows,candidate_rows), 'categories':{c:summarize(
            [r for r in baseline_rows if r['category']==c], [r for r in candidate_rows if r['category']==c]) for c in CATEGORIES},
        'unclassified_candidates':sum(r['category'] not in CATEGORIES for r in candidate_rows),
        'http_count_this_sample_all_sessions':len(list(runner.output.glob('requests/*/attempt-*/started.json'))),
        'interrupted_HTTP_unknown_billing':sum(not p.with_name('status.json').exists() for p in runner.output.glob('requests/*/attempt-*/started.json')),
        'billing_notice':'All started HTTP attempts count across resume; missing/zero usage is not proof of zero billing.',
        'usage_each_HTTP':[ev.load(p)['usage'] for p in sorted(runner.output.glob('requests/*/attempt-*/status.json'))],
        'technical_failure_as_missing':False, 'not_single_variable_AB':True, 'not_regulatory_correctness_proof':True,
        'B_independent_blind_judgment':False, 'novelty_relative_to_effective113_only':True,
        'sources_unchanged':runner.spec['sources']==source_hashes({p:ev.sha(p) for p in runner.spec['source_inputs']}),
        'assembly_validated':assembled is not None, 'runtime_failure':getattr(runner,'runtime_failure',None), 'accepted_origins':[e['origin'] for e in accepted],
        'baseline_rows':baseline_rows, 'candidate_rows':candidate_rows,
        'duplicate_groups':[g for e in accepted for g in e['wire']['duplicate_groups']],
        'review_queue':[q for e in accepted for q in e['wire']['review_queue']],
        'limitations':[v for e in accepted for v in e['wire']['limitations']],
        'duplicate_notice':'All original rows retained. Cross-task duplicates may be reported. Owner omissions/disputes remain original limitations and require manual review; no inferred labels added.'}
    ev.save(directory/'report.json',report,runner.redactor)
    for name,rows in [('baseline',baseline_rows),('candidates',candidate_rows)]:
        ev.save(directory/(name+'.json'),{'rows':rows})
        stream = io.StringIO(newline='')
        fields = list(rows[0]) if rows else ['id','category','judgment','technical_status']
        writer = csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
        for row in rows:
            writer.writerow({k:ev.canonical(v) if isinstance(v,(dict,list)) else v for k,v in row.items()})
        ev.save(directory/(name+'.csv'),stream.getvalue(),text=True)
    lines = [f'# Full coverage evaluation: {status}', '',
        f"Effective reference 113; candidates {len(candidate_rows)}. Assessed A {len(maps['baseline'])}/113; B {len(maps['generated'])}/{len(candidate_rows)}.",
        'Technical unassessed rows have no business label and are never counted as missing.', '',
        '|Category|Reference|A assessed|Candidates|B assessed|Coverage counts|', '|---|---:|---:|---:|---:|---|']
    for c,item in [('overall',report['overall']),*report['categories'].items()]:
        lines.append(f"|{c}|{item['baseline_denominator']}|{item['assessed_A']}|{item['candidate_denominator']}|{item['assessed_B']}|{ev.canonical(item['coverage'])}|")
    lines += ['', 'Novelty is relative only to effective113. B reverses real A edges; not independent blind agreement. Generation inputs differ: not a single-variable capability A/B or proof of regulatory correctness.', '', '## Every reference row', '', '|ID|Category|Coverage|Business|Reason|', '|---|---|---|---|---|']
    def cell(value):
        return str(value).replace('|',r'\|').replace('\n','<br>')
    for row in baseline_rows:
        judge = row['judgment'] or {}
        lines.append('|'+'|'.join(cell(v) for v in [row['id'],row['category'],judge.get('coverage','technical_unassessed'),judge.get('business_consistency',''),judge.get('reason','')])+'|')
    lines += ['', '## Every candidate', '', '|ID|Source category|Title|Relation|Business|Reason|', '|---|---|---|---|---|---|']
    for row in candidate_rows:
        judge = row['judgment'] or {}
        lines.append('|'+'|'.join(cell(v) for v in [row['id'],row['category'],row['title'],judge.get('relation','technical_unassessed'),judge.get('business_consistency',''),judge.get('reason','')])+'|')
    lines += ['', '## Original limitations and review queue', '', ev.canonical(report['limitations']), '', ev.canonical(report['review_queue']), '', '## Original duplicate groups', '', ev.canonical(report['duplicate_groups']), '', report['duplicate_notice']]
    ev.save(directory/'report.md','\n'.join(lines)+'\n',text=True)
    return report


def execute(output,execute_http=False,resume=False,smoke=False,transport=None):
    runner = Runner(output,transport)
    require(resume or not list((runner.output/'requests').glob('*/attempt-*')),'existing_HTTP_requires_resume')
    with scoped.pid_lock(runner.output):
        accepted,assembled = runner.audit()
        status = 'dry_run'
        if execute_http:
            require(runner.spec['public_provider']==public_provider(),'public_provider_changed')
            if assembled is None and transport is None:
                def deferred_transport(body):
                    key,endpoint,config = get_provider(PROVIDER)
                    endpoint=endpoint.removesuffix('/chat/completions')+'/responses'
                    require(endpoint==runner.spec['public_provider']['endpoint'],'provider_changed_after_preflight')
                    runner.transport=Transport(key,endpoint); runner.redactor=runner.transport.redactor
                    return runner.transport(body)
                runner.transport=deferred_transport
            if runner.transport is not None:
                require(getattr(runner.transport,'endpoint',runner.spec['public_provider']['endpoint'])==runner.spec['public_provider']['endpoint'],'transport_endpoint_mismatch')
            accepted = []
            for target in runner.spec['targets']:
                if smoke and target != runner.spec['targets'][0]:
                    break
                mapping = None
                if target['stage']=='B':
                    if sum(len(e['wire'].get('baseline_judgments',{})) for e in accepted)!=113:
                        break
                    mapping = scoped.inverse_mapping(runner.context,[e for e in accepted if e['origin']['stage']=='A'])
                try:
                    entry = runner.block(target,mapping)
                except SafeFailure as exc:
                    runner.runtime_failure=str(exc)
                    break
                if entry is None:
                    break
                accepted.append(entry)
            accepted,assembled = runner.audit()
            status = 'complete' if assembled is not None else 'smoke_success' if smoke and accepted else 'evaluation_failed'
            if assembled is not None:
                scoped.save_once(runner.output/'assembled.json',assembled)
        session = runner.output/f'session-{len(list(runner.output.glob("session-*")))+1:03d}'
        return report_files(session,runner,accepted,assembled,status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    p = sub.add_parser('prepare')
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--input',type=Path); group.add_argument('--campaign',type=Path)
    p.add_argument('--database',type=Path,help='Optional ro snapshot access for selected non-checklist full hits'); p.add_argument('--control',type=Path); p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--max-request-bytes',type=int,default=DEFAULT_MAX_REQUEST_BYTES,
        help='Sealed request byte limit, 1048576..2097152; independent of fixed 2MiB response capture')
    p.add_argument('--target-size',type=int,choices=range(1,6),default=5); p.add_argument('--input-id',default='full-coverage')
    for name in ('run','audit'):
        p = sub.add_parser(name); p.add_argument('--run-dir',type=Path,required=True)
        if name=='run':
            p.add_argument('--execute',action='store_true'); p.add_argument('--resume',action='store_true'); p.add_argument('--smoke',action='store_true')
    args = parser.parse_args()
    try:
        if args.command=='prepare':
            spec = prepare(args.campaign or args.input,args.output_dir,target_size=args.target_size,campaign=args.campaign is not None,control_path=args.control,input_id=args.input_id,database=args.database,max_request_bytes=args.max_request_bytes)
            result = {'status':'dry_run','http_count':0,'credential_reads':0,'plan_sha256':spec['plan_sha256'],'targets':len(spec['targets'])}
        elif args.command=='audit':
            runner = Runner(args.run_dir); accepted,assembled = runner.audit()
            result = {'status':'complete' if assembled else 'incomplete','accepted_blocks':len(accepted),'assembly_validated':assembled is not None}
        else:
            report = execute(args.run_dir,args.execute,args.resume,args.smoke)
            result = {k:v for k,v in report.items() if k not in ('baseline_rows','candidate_rows','accepted_origins')}
        print(ev.canonical(result))
        return 1 if result['status']=='evaluation_failed' else 0
    except Exception as exc:
        print('error: '+(str(exc) if isinstance(exc,SafeFailure) else 'local_'+type(exc).__name__),file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
