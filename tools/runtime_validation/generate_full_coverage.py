#!/usr/bin/env python3
"""Six new sequential scenario tasks; opt-in HTTP, durable writes, GET-only resume."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

import credential_runner as credentials
import validate_coverage_flow as base

ROOT = Path(__file__).resolve().parents[2]
VERSION = 'full-coverage-generation-1'
PROJECT, SPACE = 4, 3
CHAPTERS = (
    {'key': 'AUC', 'heading': '1. 集合竞价撮合成交', 'raw_expected': 22, 'required_expected': 22, 'gold_effective': 19},
    {'key': 'AMT', 'heading': '2. 单笔成交金额', 'raw_expected': 14, 'required_expected': 14, 'gold_effective': 14},
    {'key': 'ACC', 'heading': '3. 累计成交金额', 'raw_expected': 13, 'required_expected': 13, 'gold_effective': 13},
    {'key': 'OPN', 'heading': '4. 开盘价', 'raw_expected': 24, 'required_expected': 22, 'gold_effective': 19},
    {'key': 'CLS', 'heading': '5. 收盘价', 'raw_expected': 28, 'required_expected': 26, 'gold_effective': 22},
    {'key': 'CAN', 'heading': '6. 撤单订单类型验证', 'raw_expected': 26, 'required_expected': 26, 'gold_effective': 26},
)
DOCUMENTS = {
    3: {'filename': '竞价基本功能测试.md', 'char_count': 7281, 'sha256': 'ba31c9c712f6ca56a21a7918b6ef707059f2942626a36f22f938d52e0e6c4ce9'},
    4: {'filename': '1_上海证券交易所交易规则（2026年修订）.docx', 'char_count': 19475, 'sha256': 'fc922c433438b2636cb631eab25cca405209712acbb6aaded768c45456ff8888'},
}
TASK_FIELDS = ('id', 'requirement_id', 'project_id', 'wiki_space_id', 'status', 'title', 'description', 'model_id', 'prompt_template_id')
Error = base.ValidationError


def require(condition, message):
    if not condition:
        raise Error(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'record missing or symlink refused')
    return json.loads(path.read_text(encoding='utf-8'))


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdir(path):
    Path(path).mkdir(mode=0o700)
    fsync_directory(Path(path).parent)


def durable(archive, name, value):
    archive.save(name, value)
    fsync_directory((archive.path / name).parent)


def progress(event, **fields):
    print(json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)


def source_hashes():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
        Path(__file__).resolve(), Path(base.__file__).resolve(), Path(credentials.__file__).resolve(),
        ROOT / 'backend/app/services/scenario_coverage.py', ROOT / 'backend/app/services/llm.py')}


def payload(chapter, metadata, campaign_id):
    exclusions = '排除回归项：《DVP》\n排除回归项：《DST》\n' if chapter['key'] in {'OPN', 'CLS'} else ''
    return {'title': f"六模块场景 {campaign_id}：{chapter['heading']}",
        'description': f"业务范围：{chapter['heading']}。基于本项目已摄入原始资料生成测试场景。\n"
            f"回归清单：《竞价基本功能测试.md》\n回归章节：《{chapter['heading']}》\n" + exclusions
            + '保留本章原文回归项及表头/上下文，补充有来源依据的业务分支；资料不足保留缺口。仅生成场景和验证目标。',
        'project_id': PROJECT, 'wiki_space_id': SPACE, 'model_id': metadata.get('model_id'),
        'prompt_template_id': metadata.get('prompt_id'), 'auto_review': False, 'run_generate': False,
        'generation_granularity': 'detailed', 'test_dimensions': ['positive', 'negative', 'boundary'],
        'reference_case_ids': [], 'reference_text': ''}


class Client(base.Client):
    """Only newly bound campaign tasks may receive a marker-authorized write."""
    def __init__(self, archive, **kwargs):
        super().__init__(archive, PROJECT, SPACE, **kwargs)
        self.task_ids = set()
        self.permit = None

    def check_write(self, method, path):
        if method == 'GET':
            fixed = {'/api/health', '/api/projects/4', '/api/wiki-spaces/3', '/api/models', '/api/prompts',
                     '/api/documents', '/api/wiki/pages', '/api/projects/4/external-wiki', '/api/tasks',
                     '/api/documents/3/chunks', '/api/documents/4/chunks'}
            task_reads = {f'/api/tasks/{identifier}{suffix}' for identifier in self.task_ids
                          if type(identifier) is int and identifier > 19
                          for suffix in ('', '/retrieval-checkpoint', '/test-points', '/events', '/citations')}
            require(path in fixed | task_reads, 'read endpoint outside campaign scope')
            return
        if method == 'POST' and path in {'/api/auth/login', '/api/auth/logout'}:
            return
        allowed = {'/api/tasks'} | {f'/api/tasks/{identifier}{suffix}' for identifier in self.task_ids
                                   if type(identifier) is int and identifier > 19
                                   for suffix in ('/generate', '/retrieval-checkpoint/confirm')}
        require(not self.observe_only and method == 'POST' and path in allowed
                and self.permit is not None and self.permit['path'] == path
                and not self.permit['used'], 'write outside new task marker or GET-only resume boundary')
        self.permit['used'] = True

    def request(self, method, path, data=None, **kwargs):
        if method != 'GET' and path not in {'/api/auth/login', '/api/auth/logout'}:
            require(self.permit is not None and self.permit['body_sha256'] == digest(data),
                    'write body differs from durable marker')
        return super().request(method, path, data, **kwargs)

    def write_once(self, owner, label, path, body, identity):
        require(not self.observe_only, 'GET-only resume cannot write')
        require(not (owner.path / f'{label}.started.json').exists()
                and not (owner.path / f'{label}.mapped.json').exists(), 'write previously attempted; never replay')
        marker = {'method': 'POST', 'path': path, 'body': body, 'body_sha256': digest(body),
                  'project_id': PROJECT, 'space_id': SPACE, 'identity': identity}
        durable(owner, f'{label}.started.json', marker)
        self.permit = {'path': path, 'body_sha256': marker['body_sha256'], 'used': False}
        try:
            result = self.request('POST', path, body, project_response=lambda item: base.pick(item, TASK_FIELDS))
            require(type(result.get('id')) is int and result['id'] > 0, 'write returned no valid task ID; never replay')
            durable(owner, f'{label}.mapped.json', {'request_sha256': marker['body_sha256'], 'result': result})
            return result
        finally:
            self.permit = None


def verify_sources(client):
    metadata = base.source_metadata(client)
    require({doc['id'] for doc in metadata['documents']} == set(DOCUMENTS), 'source documents differ from docs3/4')
    rebuilt = {}
    for doc in metadata['documents']:
        require(doc['space_id'] == SPACE and all(doc.get(key) == value for key, value in DOCUMENTS[doc['id']].items()),
                'source document identity or hash changed')
        chunks = client.request('GET', f"/api/documents/{doc['id']}/chunks")
        text, ids = '', []
        for index, chunk in enumerate(chunks):
            start, end, part = chunk['start_char'], chunk['end_char'], chunk['text']
            require(chunk['document_id'] == doc['id'] and chunk['space_id'] == SPACE
                    and chunk['chunk_index'] == index and type(chunk['id']) is int and chunk['id'] > 0
                    and chunk['id'] not in ids and 0 <= start <= len(text) < end
                    and end - start == len(part) and text[start:] == part[:len(text) - start],
                    'source chunk association or reconstruction changed')
            text += part[len(text) - start:]
            ids.append(chunk['id'])
        require(len(text) == doc['char_count'] and bool(ids), 'source chunk text incomplete')
        if doc['id'] == 3:
            require(all(re.search(r'^##\s+' + re.escape(chapter['heading']) + r'\s*$', text, re.M)
                        for chapter in CHAPTERS), 'one or more regression chapters absent')
        rebuilt[str(doc['id'])] = {'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'chunk_ids': ids}
    metadata['reconstruction'] = rebuilt
    return metadata


def chapter_items(checkpoint, chapter, selected, expected_text_sha256=None):
    evidence = checkpoint.get('evidence') or {}
    require(evidence.get('status') == 'complete' and not evidence.get('remaining_unexpanded'), 'retrieval evidence incomplete')
    manifests = [m for m in evidence.get('manifests', []) if m.get('checklist')]
    require(len(manifests) == 1, 'checklist manifest missing or ambiguous')
    manifest = manifests[0]
    require(manifest.get('document_id') == 3 and manifest.get('space_id') == SPACE
            and manifest.get('status') == 'complete' and manifest.get('parse_status') == 'parsed_structural_entries'
            and manifest.get('line_inventory_version') == 1
            and manifest.get('source_version') == DOCUMENTS[3]['sha256'], 'checklist structure or source mismatch')
    if expected_text_sha256 is not None:
        require(manifest.get('full_text_hash') == expected_text_sha256, 'frozen checklist differs from preflight reconstruction')
    full_text = manifest.get('full_text', '')
    require(isinstance(full_text, str) and len(full_text) == DOCUMENTS[3]['char_count']
            and hashlib.sha256(full_text.encode()).hexdigest() == manifest.get('full_text_hash'), 'frozen checklist text hash mismatch')
    counts, eligible, all_ids = Counter(), [], set()
    for item in manifest.get('items', []):
        require(item['id'] not in all_ids, 'duplicate required entry identity')
        all_ids.add(item['id'])
        headings = [re.sub(r'^#+\s*', '', h).strip() for h in item.get('section_context', [])]
        matches = [c['key'] for c in CHAPTERS if c['heading'] in headings]
        require(len(matches) == 1, 'required entry chapter missing or ambiguous')
        counts[matches[0]] += 1
        if chapter['heading'] not in headings:
            continue
        text = '\n'.join([item.get('text', ''), item.get('table_header', ''), *item.get('section_context', [])])
        if any(value in text for value in ('DVP', 'DST')):
            require(chapter['key'] in {'OPN', 'CLS'}, 'unexpected excluded product in another chapter')
            continue
        deps = item.get('candidate_citation_ids') or []
        require(bool(deps) and set(deps) <= selected, 'required entry dependency not selected')
        eligible.append(item['id'])
    require(counts == Counter({c['key']: c['raw_expected'] for c in CHAPTERS})
            and len(eligible) == chapter['required_expected'], 'regression inventory count changed; no gold count substitution')
    lines = {line['id']: line for line in manifest.get('line_inventory', [])}
    for line in lines.values():
        if line.get('role') != 'prose' or line.get('entry_id') is not None:
            continue
        if chapter['heading'] not in [re.sub(r'^#+\s*', '', h).strip() for h in line.get('section_context', [])]:
            continue
        dependencies = [line, *[lines.get(identifier) for identifier in line.get('context_line_ids', [])]]
        require(all(dep is not None and dep.get('candidate_citation_ids') and set(dep['candidate_citation_ids']) <= selected
                    for dep in dependencies), 'same chapter prose dependency not selected')
    return eligible


def selection(checkpoint, chapter, metadata, task_id):
    require(checkpoint.get('task_id') == task_id and checkpoint.get('status') == 'pending'
            and type(checkpoint.get('id')) is int and checkpoint['id'] > 0
            and type(checkpoint.get('version')) is int and checkpoint['version'] > 0
            and not checkpoint.get('conflict_groups'), 'retrieval identity status or conflict mismatch')
    pages = {page['id'] for page in metadata['pages'] if page['status'] == 'published' and page.get('source_document_id') in DOCUMENTS}
    ids = [citation['id'] for citation in checkpoint.get('candidate_citations', [])
           if citation.get('available', True) and not citation.get('legacy')
           and ((citation.get('citation_type') == 'source' and citation.get('source_chunk_id') in metadata['chunk_ids'])
                or (citation.get('citation_type') == 'wiki' and citation.get('wiki_page_id') in pages))]
    require(bool(ids) and len(ids) == len(set(ids)) and all(type(i) is int and i > 0 for i in ids), 'eligible citation identity mismatch')
    required_ids = chapter_items(checkpoint, chapter, set(ids), metadata['reconstruction']['3']['text_sha256'])
    return ids, required_ids


def task_identity(task, binding):
    require(task.get('id') == binding['task_id'] and task.get('project_id') == PROJECT
            and task.get('wiki_space_id') == SPACE and task.get('requirement_id') == binding['requirement_id']
            and task.get('title') == binding['title'] and task.get('description') == binding['description'], 'task identity or original scope changed')


def artifact(path, owner):
    return {'path': str(path.relative_to(owner)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def verify_frozen(owner):
    frozen = read(owner / 'frozen.json')
    for item in frozen['artifacts'].values():
        path = (owner / item['path']).resolve()
        require(path.is_relative_to(owner.resolve()) and path.is_file() and not path.is_symlink()
                and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256'], 'frozen artifact hash or path changed')
    return frozen


def freeze(client, owner, chapter, binding, metadata):
    checkpoint = client.request('GET', f"/api/tasks/{binding['task_id']}/retrieval-checkpoint")
    marker = read(owner / 'retrieval-confirm.started.json')
    body = marker['body']
    require(checkpoint.get('task_id') == binding['task_id'] and checkpoint.get('status') == 'confirmed'
            and checkpoint.get('id') == body['checkpoint_id'] and checkpoint.get('version') == body['expected_version']
            and checkpoint.get('selected_citation_ids') == body['selected_citation_ids'], 'confirmed retrieval binding changed')
    required_ids = chapter_items(checkpoint, chapter, set(checkpoint['selected_citation_ids']),
                                 metadata['reconstruction']['3']['text_sha256'])
    client.archive.save('retrieval-checkpoint.json', checkpoint)
    client.archive.save('retrieval-selection.json', body)
    coverage = base.freeze(client, binding['task_id'])
    proposal = read(client.archive.path / 'proposal.json')
    require(proposal.get('retrieval_checkpoint_id') == checkpoint['id'] and proposal.get('version', 0) >= 1
            and len(proposal.get('points', [])) <= 200, 'proposal retrieval binding or point limit changed')
    required = [entry['id'] for entry in coverage.get('required_items', []) if entry.get('eligible') and entry.get('in_scope')]
    require(set(required) == set(required_ids) and len(required) == chapter['required_expected'], 'coverage required scope differs from frozen source')
    names = {'proposal': 'proposal.json', 'retrieval_checkpoint': 'retrieval-checkpoint.json',
             'retrieval_selection': 'retrieval-selection.json', 'citations': 'citations.json',
             'coverage_metadata': 'coverage-metadata.json', 'scenarios': 'scenarios.json',
             'events': 'events.json', 'scenarios_csv': 'scenarios.csv'}
    frozen = {'key': chapter['key'], 'task_id': binding['task_id'], 'requirement_id': binding['requirement_id'],
        'project_id': PROJECT, 'space_id': SPACE, 'checkpoint_id': proposal['id'], 'version': proposal['version'],
        'retrieval_checkpoint_id': checkpoint['id'], 'points': len(proposal['points']),
        'required_expected': chapter['required_expected'], 'required_actual': len(required),
        'gold_effective_count': chapter['gold_effective'], 'coverage_status': coverage.get('status', 'unknown'),
        'artifact_path_base': 'frozen_parent',
        'artifacts': {key: artifact(client.archive.path / name, owner) for key, name in names.items()}}
    archive = base.Archive(owner); archive.secrets = client.archive.secrets
    durable(archive, 'frozen.json', frozen)
    progress('chapter_frozen', key=chapter['key'], task_id=binding['task_id'], checkpoint_id=proposal['id'],
             points=len(proposal['points']), coverage_status=frozen['coverage_status'])
    return frozen


def chapter_run(client, root, chapter, metadata, campaign_id, *, resume, deadline, interval, existing_ids):
    owner_path = root / 'chapters' / chapter['key']
    if not owner_path.exists():
        if resume:
            return None
        mkdir(owner_path)
    owner = base.Archive(owner_path); owner.secrets = client.archive.secrets
    attempt = owner_path / ('attempt-' + uuid.uuid4().hex); mkdir(attempt)
    client.archive = base.Archive(attempt); client.archive.secrets = owner.secrets
    identity = {'campaign_id': campaign_id, 'chapter': chapter['key'], 'heading': chapter['heading']}
    progress('chapter_observing' if resume else 'chapter_starting', key=chapter['key'])
    if resume:
        require((owner_path / 'task.json').is_file(), 'recorded task missing; uncertain create never repeated')
        binding = read(owner_path / 'task.json')
        require(binding['campaign_id'] == campaign_id and binding['chapter'] == chapter['key']
                and binding['project_id'] == PROJECT and binding['space_id'] == SPACE
                and binding['task_id'] > 19, 'recorded chapter identity mismatch')
        client.task_ids.add(binding['task_id'])
    else:
        body = payload(chapter, metadata, campaign_id)
        task = client.write_once(owner, 'create', '/api/tasks', body, identity)
        require(task['id'] not in existing_ids and task['id'] not in client.task_ids and task['id'] > 19
                and task.get('project_id') == PROJECT and task.get('wiki_space_id') == SPACE
                and type(task.get('requirement_id')) is int and task['requirement_id'] > 0
                and task.get('title') == body['title'] and task.get('description') == body['description'], 'created task not new or scope mismatch')
        binding = {'campaign_id': campaign_id, 'chapter': chapter['key'], 'task_id': task['id'],
            'requirement_id': task['requirement_id'], 'project_id': PROJECT, 'space_id': SPACE,
            'title': body['title'], 'description': body['description'], 'payload_sha256': digest(body)}
        durable(owner, 'task.json', binding)
        client.task_ids.add(task['id'])
        generated = client.write_once(owner, 'generate', f"/api/tasks/{task['id']}/generate", None,
                                      {**identity, 'task_id': task['id']})
        task_identity(generated, binding)
    if (owner_path / 'frozen.json').exists():
        frozen = verify_frozen(owner_path)
        task = client.request('GET', f"/api/tasks/{binding['task_id']}"); task_identity(task, binding)
        proposal = client.request('GET', f"/api/tasks/{binding['task_id']}/test-points")
        require(proposal.get('id') == frozen['checkpoint_id'] and proposal.get('version') == frozen['version']
                and proposal.get('status') == 'pending' and task.get('status') == 'awaiting_test_point_confirmation', 'frozen checkpoint no longer current pending proposal')
        return frozen
    until, states = time.monotonic() + deadline, []
    try:
        while time.monotonic() < until:
            task = client.request('GET', f"/api/tasks/{binding['task_id']}"); task_identity(task, binding)
            status = task['status']; states.append({'status': status, 'at_utc': datetime.now(timezone.utc).isoformat()})
            progress('chapter_state', key=chapter['key'], task_id=binding['task_id'], status=status)
            if status == 'awaiting_test_point_confirmation':
                return freeze(client, owner_path, chapter, binding, metadata)
            if status == 'awaiting_confirmation':
                require(not resume, 'GET-only resume reached retrieval gate; no automatic confirmation')
                checkpoint = client.request('GET', f"/api/tasks/{binding['task_id']}/retrieval-checkpoint")
                ids, required_ids = selection(checkpoint, chapter, metadata, binding['task_id'])
                client.archive.save('retrieval-checkpoint-pending.json', checkpoint)
                body = {'checkpoint_id': checkpoint['id'], 'expected_version': checkpoint['version'],
                    'selected_citation_ids': ids, 'supplemental_text': '',
                    'idempotency_key': f"full-{campaign_id}-{chapter['key']}-{checkpoint['id']}-{checkpoint['version']}"}
                client.archive.save('selection-check.json', {'required_ids': required_ids, 'required_count': len(required_ids)})
                confirmed = client.write_once(owner, 'retrieval-confirm', f"/api/tasks/{binding['task_id']}/retrieval-checkpoint/confirm", body,
                                              {**identity, 'task_id': binding['task_id']})
                task_identity(confirmed, binding)
            else:
                require(status in {'retrieving', 'generating_test_points'}, 'task stopped failed or unexpected; no retry')
            time.sleep(min(interval, max(0, until - time.monotonic())))
        raise Error('chapter observation deadline exceeded; only GET resume permitted')
    finally:
        client.archive.save('state-flow.json', {'task_id': binding['task_id'], 'states': states})


def load_campaign(root):
    plan = read(root / 'plan.json')
    require(plan['version'] == VERSION and plan['project_id'] == PROJECT and plan['space_id'] == SPACE
            and plan['chapters'] == list(CHAPTERS) and plan['source_hashes'] == source_hashes(), 'campaign plan or code binding changed')
    ids = set()
    for chapter in CHAPTERS:
        owner = root / 'chapters' / chapter['key']
        for marker in owner.glob('*.started.json'):
            value = read(marker)
            require(value.get('body_sha256') == digest(value.get('body'))
                    and value.get('project_id') == PROJECT and value.get('space_id') == SPACE
                    and value.get('identity', {}).get('campaign_id') == plan['campaign_id']
                    and value.get('identity', {}).get('chapter') == chapter['key'], 'durable marker hash or identity mismatch')
        if owner.exists():
            require((owner / 'task.json').is_file() and (owner / 'create.mapped.json').is_file(), 'uncertain create; inspect existing resources manually, never replay')
            task = read(owner / 'task.json')
            create = read(owner / 'create.started.json'); mapped = read(owner / 'create.mapped.json')
            require(task['payload_sha256'] == create['body_sha256'] == mapped['request_sha256']
                    and task['task_id'] == mapped['result']['id'] and task['task_id'] not in ids
                    and task['task_id'] > 19, 'recorded task mapping changed')
            ids.add(task['task_id'])
    return plan


@contextmanager
def lock_run(root):
    path = root / '.lock'
    require(not path.is_symlink(), 'lock symlink refused')
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error('another process owns this campaign') from None
        yield


def run(root, config_path, *, resume=False, deadline=1200, interval=3, opener=None):
    root = Path(root)
    if resume:
        plan = load_campaign(root)  # Reject uncertain create before credential read or any HTTP.
    else:
        require(not root.exists() and not root.is_symlink(), 'new campaign directory must not exist')
        plan = {'version': VERSION, 'campaign_id': uuid.uuid4().hex, 'project_id': PROJECT, 'space_id': SPACE,
                'chapters': list(CHAPTERS), 'required_total': 123, 'gold_effective_total': 113, 'source_hashes': source_hashes()}
    config = credentials.load_config(config_path)
    if not resume:
        mkdir(root); durable(base.Archive(root), 'plan.json', plan); mkdir(root / 'chapters')
    with lock_run(root):
        attempt = root / ('session-' + uuid.uuid4().hex); mkdir(attempt)
        archive = base.Archive(attempt)
        client = Client(archive, root=config['base_url'], origin=config['origin'], opener=opener)
        client.observe_only = resume
        outcome = {'all_frozen': False, 'chapters': [], 'required_total': 123, 'gold_effective_total': 113,
                   'stage_only': True, 'scenario_quality_judged': False, 'failure': None}
        try:
            require(client.request('GET', '/api/health') == {'status': 'ok'}, 'health response mismatch')
            client.login(config['username'], config['password'])
            if resume:
                metadata = read(root / 'source-metadata.json')
                existing_ids = set()
            else:
                metadata = verify_sources(client)
                durable(base.Archive(root), 'source-metadata.json', metadata)
                existing = client.request('GET', '/api/tasks', project_response=lambda rows: [base.pick(row, ('id', 'project_id', 'wiki_space_id', 'status')) for row in rows])
                require(all(task.get('status') not in {'retrieving', 'generating_test_points', 'generating', 'reviewing'} for task in existing), 'active task exists; sequential generation refused')
                existing_ids = {task['id'] for task in existing}
            for chapter in CHAPTERS:
                frozen = chapter_run(client, root, chapter, metadata, plan['campaign_id'], resume=resume,
                                     deadline=deadline, interval=interval, existing_ids=existing_ids)
                if frozen is not None:
                    outcome['chapters'].append({'key': chapter['key'], 'task_id': frozen['task_id'],
                        'frozen_path': str((root / 'chapters' / chapter['key'] / 'frozen.json').relative_to(root)),
                        'coverage_status': frozen['coverage_status']})
            outcome['all_frozen'] = len(outcome['chapters']) == 6
            if outcome['all_frozen']:
                summary = {**outcome, 'project_id': PROJECT, 'space_id': SPACE, 'campaign_id': plan['campaign_id'],
                           'frozen_path_base': 'campaign_root', 'all_coverage_complete': all(c['coverage_status'] == 'complete' for c in outcome['chapters'])}
                if (root / 'campaign-summary.json').exists():
                    require(read(root / 'campaign-summary.json') == summary, 'existing summary differs; never overwrite')
                else:
                    durable(base.Archive(root), 'campaign-summary.json', summary)
        except Error as exc:
            outcome['failure'] = str(exc)
        except (ValueError, KeyError, TypeError, OSError):
            outcome['failure'] = 'invalid API schema or local IO; existing writes are never replayed'
        finally:
            client.archive = archive
            try:
                client.logout()
            except Error:
                outcome['logout_failure'] = True
            archive.save('outcome.json', outcome)
        progress('campaign_finished', run=str(root), all_frozen=outcome['all_frozen'], frozen_chapters=len(outcome['chapters']))
        return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--config', type=Path, default=ROOT / 'data/private/casegen-validation/credentials.json')
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--deadline', type=float, default=1200)
    parser.add_argument('--poll-interval', type=float, default=3)
    args = parser.parse_args(argv)
    if not (math.isfinite(args.deadline) and 0 < args.deadline <= 7200
            and math.isfinite(args.poll_interval) and 1 <= args.poll_interval <= 60):
        parser.error('bounded deadline or interval invalid')
    if not args.execute:
        print(json.dumps({'executed': False, 'http_requests': 0, 'credential_reads': 0,
            'mode': 'GET_only_resume' if args.resume else 'six_new_tasks', 'project_id': PROJECT, 'space_id': SPACE,
            'chapters': list(CHAPTERS), 'required_total': 123, 'gold_effective_total': 113,
            'payload_plan': [payload(chapter, {}, '<new-campaign-id>') for chapter in CHAPTERS],
            'model_prompt_binding': 'resolve existing default model and active generate prompt at authenticated preflight; never change them'}, ensure_ascii=False))
        return 0
    try:
        validation = (ROOT / 'data/validation').resolve()
        require(validation.is_dir(), 'existing data/validation directory required')
        root = args.resume.absolute() if args.resume else validation / ('full-coverage-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8])
        require(not root.is_symlink() and not any(p.is_symlink() for p in root.parents)
                and root.resolve().parent == validation and root.name.startswith('full-coverage-'), 'campaign path outside validation root')
        outcome = run(root, args.config, resume=bool(args.resume), deadline=args.deadline, interval=args.poll_interval)
        return 0 if outcome['all_frozen'] else 1
    except (Error, credentials.CredentialError):
        print('full coverage stopped safely; inspect safe records; never replay uncertain writes', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('full coverage interrupted; use GET-only resume of the same run', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
