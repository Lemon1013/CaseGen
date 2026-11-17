"""Synthetic API only: no app imports, database, credentials file or real HTTP."""
import copy
from email.message import Message
from http.cookiejar import Cookie
import importlib.util
import io
import json
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_full_coverage as g


class Response:
    status = 200

    def __init__(self, value):
        self.raw = io.BytesIO(json.dumps(value, ensure_ascii=False).encode())
        self.headers = Message(); self.headers['Content-Type'] = 'application/json'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.raw.read(size)


def cookie(name, value):
    return Cookie(0, name, value, None, False, '127.0.0.1', False, False,
                  '/', True, False, None, True, None, None, {})


class API:
    def __init__(self, documents):
        self.documents = documents
        self.tasks, self.checkpoints, self.calls = {}, {}, []
        self.client = None
        self.fail_once = None
        self.cp_change = None
        self.coverage_status = 'partial'
        self.source_bad = False

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        body = json.loads(request.data) if request.data is not None else None
        self.calls.append((method, path, body))
        if method == 'POST' and path not in {'/api/auth/login'}:
            assert request.get_header('X-csrf-token') == 'synthetic-csrf-value'
        if path == '/api/health':
            result = {'status': 'ok'}
        elif path == '/api/auth/login':
            assert body == {'username': 'synthetic_user', 'password': 'synthetic-password-value'}
            self.client.jar.set_cookie(cookie('casegen_session', 'synthetic-session-value'))
            self.client.jar.set_cookie(cookie('casegen_csrf', 'synthetic-csrf-value'))
            result = {'username': 'synthetic_user', 'session': 'synthetic-session-value'}
        elif path == '/api/auth/logout':
            result = {'status': 'ok'}
        elif path == '/api/projects/4':
            result = {'id': 4, 'status': 'active', 'default_wiki_space_id': 3, 'shared_spaces': []}
        elif path == '/api/wiki-spaces/3':
            result = {'id': 3, 'project_id': 4, 'scope': 'project', 'status': 'active'}
        elif path == '/api/projects/4/external-wiki':
            result = None
        elif path == '/api/models':
            result = [{'id': 1, 'name': 'synthetic-model', 'is_default': True, 'protocol': 'chat_completions', 'api_key': 'never-archive-model-key'}]
        elif path == '/api/prompts':
            result = [{'id': index, 'type': kind, 'version': 1, 'is_active': True, 'content': 'synthetic prompt'}
                      for index, kind in enumerate(['generate', 'test_points'], 1)]
        elif path == '/api/documents':
            result = [{'id': identifier, 'space_id': 3, 'status': 'ready', **{key: value for key, value in doc.items() if key != 'text'}}
                      for identifier, doc in self.documents.items()]
            if self.source_bad:
                result[0]['sha256'] = 'wrong'
        elif path == '/api/wiki/pages':
            result = [{'id': 90, 'status': 'published', 'source_document_id': 3}]
        elif path.endswith('/chunks'):
            identifier = int(path.split('/')[3]); doc = self.documents[identifier]
            result = [{'id': identifier * 10 + 1, 'document_id': identifier, 'space_id': 3,
                       'chunk_index': 0, 'start_char': 0, 'end_char': len(doc['text']), 'text': doc['text']}]
        elif path == '/api/tasks' and method == 'GET':
            result = [{'id': 19, 'project_id': 4, 'wiki_space_id': 3, 'status': 'awaiting_test_point_confirmation'}, *self.tasks.values()]
        elif path == '/api/tasks' and method == 'POST':
            identifier = 40 + len(self.tasks)
            result = {**body, 'id': identifier, 'requirement_id': identifier + 100, 'status': 'draft'}
            self.tasks[identifier] = copy.deepcopy(result)
        elif path.startswith('/api/tasks/'):
            identifier = int(path.split('/')[3]); task = self.tasks[identifier]
            if path.endswith('/generate'):
                task['status'] = 'awaiting_confirmation'; result = task
            elif path.endswith('/retrieval-checkpoint/confirm'):
                cp = self.checkpoints[identifier]; assert cp['id'] == body['checkpoint_id'] and cp['version'] == body['expected_version']
                cp['selected_citation_ids'] = body['selected_citation_ids']; cp['status'] = 'confirmed'
                task['status'] = 'awaiting_test_point_confirmation'; result = task
            elif path.endswith('/retrieval-checkpoint'):
                if identifier not in self.checkpoints:
                    self.checkpoints[identifier] = self.make_checkpoint(identifier)
                result = self.checkpoints[identifier]
                if self.cp_change and result['status'] == 'pending':
                    result = self.cp_change(copy.deepcopy(result))
            elif path.endswith('/test-points'):
                result = self.proposal(identifier)
            elif path.endswith('/events') or path.endswith('/citations'):
                result = []
            else:
                result = task
        else:
            raise AssertionError('Unexpected endpoint')
        if self.fail_once and method == 'POST' and path == self.fail_once:
            self.fail_once = None
            raise URLError('synthetic-password-value')  # Accepted write, lost response.
        return Response(copy.deepcopy(result))

    def make_checkpoint(self, identifier):
        cid = identifier * 100
        items = []
        for chapter in g.CHAPTERS:
            for index in range(chapter['raw_expected']):
                text = f'synthetic-{index}'
                if chapter['key'] in {'OPN', 'CLS'} and index >= chapter['raw_expected'] - 2:
                    text = 'DVP' if index == chapter['raw_expected'] - 2 else 'DST'
                items.append({'id': f"{chapter['key']}-{index}", 'text': text, 'table_header': '',
                    'section_context': ['# Synthetic', '## ' + chapter['heading']], 'candidate_citation_ids': [cid]})
        text = self.documents[3]['text']
        manifest = {'document_id': 3, 'space_id': 3, 'checklist': True, 'status': 'complete',
            'source_version': self.documents[3]['sha256'],
            'parse_status': 'parsed_structural_entries', 'line_inventory_version': 1,
            'full_text': text, 'full_text_hash': g.hashlib.sha256(text.encode()).hexdigest(), 'items': items,
            'line_inventory': [{'id': 'rule', 'role': 'prose', 'entry_id': None, 'text': 'synthetic rule',
                'section_context': ['## ' + g.CHAPTERS[0]['heading']], 'context_line_ids': ['heading'], 'candidate_citation_ids': [cid]},
                {'id': 'heading', 'role': 'heading', 'text': 'heading', 'candidate_citation_ids': [cid]}]}
        return {'id': identifier + 200, 'task_id': identifier, 'version': 1, 'attempt': 1, 'status': 'pending',
            'selected_citation_ids': [], 'conflict_groups': [], 'candidate_citations': [
                {'id': cid, 'citation_type': 'source', 'source_chunk_id': 31},
                {'id': cid + 1, 'citation_type': 'source', 'source_chunk_id': 999},
                {'id': cid + 2, 'citation_type': 'source', 'source_chunk_id': 41, 'legacy': True}],
            'evidence': {'status': 'complete', 'manifests': [manifest], 'remaining_unexpanded': []}}

    def proposal(self, identifier):
        task, cp = self.tasks[identifier], self.checkpoints[identifier]
        chapter = next(chapter for chapter in g.CHAPTERS if f"回归章节：《{chapter['heading']}》" in task['description'])
        entries = []
        for item in cp['evidence']['manifests'][0]['items']:
            included = '## ' + chapter['heading'] in item['section_context'] and item['text'] not in {'DVP', 'DST'}
            entries.append({**item, 'eligible': included, 'in_scope': included})
        return {'id': identifier + 300, 'task_id': identifier, 'version': 1, 'status': 'pending',
            'retrieval_checkpoint_id': cp['id'], 'points': [{'id': identifier * 100, 'title': 'synthetic scene',
                'verification_goal': 'original goal', 'citation_ids': [identifier * 100]}],
            'coverage_metadata': {'status': self.coverage_status, 'required_items': entries,
                'summary': {'unverified': 1}, 'model_errors': [{'category': 'timeout', 'attempts': 4}],
                'warnings': ['synthetic unverified branch'], 'budget': {'calls_used': 3}}}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    checklist = '# Synthetic\n' + '\n'.join('## ' + chapter['heading'] + '\nrule' for chapter in g.CHAPTERS)
    docs = {3: {'filename': g.base.CHECKLIST, 'text': checklist}, 4: {'filename': 'synthetic-rules.docx', 'text': 'synthetic rules'}}
    for doc in docs.values():
        doc.update(char_count=len(doc['text']), sha256=g.hashlib.sha256(doc['text'].encode()).hexdigest())
    monkeypatch.setattr(g, 'DOCUMENTS', {identifier: {key: value for key, value in doc.items() if key != 'text'} for identifier, doc in docs.items()})
    monkeypatch.setattr(g.time, 'sleep', lambda _: None)
    config = {'username': 'synthetic_user', 'password': 'synthetic-password-value', 'base_url': 'http://127.0.0.1:8001', 'origin': 'http://127.0.0.1:5173'}
    loads = []
    def load(path):
        loads.append(path); return config
    monkeypatch.setattr(g.credentials, 'load_config', load)
    api = API(docs)
    original = g.Client
    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs['opener'] = api
            super().__init__(*args, **kwargs); api.client = self
    monkeypatch.setattr(g, 'Client', MockClient)
    return tmp_path / 'full-coverage-mock', api, loads


def business_writes(api):
    return [call for call in api.calls if call[0] == 'POST' and call[1] not in {'/api/auth/login', '/api/auth/logout'}]


def test_dry_run_has_six_precise_payloads_no_config_or_http(environment, capsys):
    _, api, loads = environment
    assert g.main(['--config', '/does/not/exist']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['http_requests'] == plan['credential_reads'] == 0 and not api.calls and not loads
    assert plan['required_total'] == 123 and plan['gold_effective_total'] == 113
    assert len(plan['payload_plan']) == 6
    for chapter, body in zip(g.CHAPTERS, plan['payload_plan']):
        assert f"回归章节：《{chapter['heading']}》" in body['description']
        assert ('排除回归项' in body['description']) == (chapter['key'] in {'OPN', 'CLS'})
        assert body['run_generate'] is False and body['auto_review'] is False and body['reference_case_ids'] == []


def test_six_sequential_new_tasks_freeze_partial_and_interface(environment):
    root, api, _ = environment
    outcome = g.run(root, 'mock', deadline=5, interval=1)
    assert outcome['all_frozen'] and len(api.tasks) == 6
    writes = business_writes(api)
    assert len(writes) == 18
    for index, chapter in enumerate(g.CHAPTERS):
        tid = 40 + index
        assert [call[1] for call in writes[index * 3:index * 3 + 3]] == [
            '/api/tasks', f'/api/tasks/{tid}/generate', f'/api/tasks/{tid}/retrieval-checkpoint/confirm']
        owner = root / 'chapters' / chapter['key']
        frozen = g.verify_frozen(owner)
        assert frozen['task_id'] == tid and frozen['required_actual'] == chapter['required_expected']
        assert frozen['coverage_status'] == 'partial'
        assert set(('proposal', 'retrieval_checkpoint', 'retrieval_selection', 'citations')) <= set(frozen['artifacts'])
        selected = g.read(owner / 'retrieval-confirm.started.json')['body']['selected_citation_ids']
        assert selected == [tid * 100]
        for label in ('create', 'generate', 'retrieval-confirm'):
            marker = g.read(owner / f'{label}.started.json'); assert marker['body_sha256'] == g.digest(marker['body'])
    summary = g.read(root / 'campaign-summary.json')
    assert summary['all_frozen'] and not summary['all_coverage_complete']
    assert {entry['task_id'] for entry in summary['chapters']} == set(range(40, 46))
    text = ''.join(path.read_text() for path in root.rglob('*.json'))
    for secret in ('synthetic_user', 'synthetic-password-value', 'synthetic-csrf-value', 'synthetic-session-value', 'never-archive-model-key'):
        assert secret not in text
    assert api.calls[-1][1] == '/api/auth/logout'
    before = len(business_writes(api)); observed = g.run(root, 'mock', resume=True, deadline=5, interval=1)
    assert observed['all_frozen'] and len(business_writes(api)) == before


@pytest.mark.parametrize('path,label', [('/api/tasks', 'create'), ('/api/tasks/40/generate', 'generate'),
    ('/api/tasks/40/retrieval-checkpoint/confirm', 'retrieval-confirm')])
def test_uncertain_writes_never_replayed_and_resume_is_GET_only(environment, path, label):
    root, api, loads = environment; api.fail_once = path
    result = g.run(root, 'mock', deadline=5, interval=1)
    assert not result['all_frozen'] and len(api.tasks) == 1
    owner = root / 'chapters/AUC'
    assert (owner / f'{label}.started.json').exists() and not (owner / f'{label}.mapped.json').exists()
    writes_before, calls_before, loads_before = len(business_writes(api)), len(api.calls), len(loads)
    if label == 'create':
        with pytest.raises(g.Error, match='uncertain create'): g.run(root, 'mock', resume=True)
        assert len(api.calls) == calls_before and len(loads) == loads_before
    else:
        resumed = g.run(root, 'mock', resume=True, deadline=5, interval=1)
        assert not resumed['all_frozen']
        assert len(business_writes(api)) == writes_before
        assert all(method == 'GET' or endpoint in {'/api/auth/login', '/api/auth/logout'} for method, endpoint, _ in api.calls[calls_before:])
        assert len(api.tasks) == 1
        if label == 'retrieval-confirm': assert (owner / 'frozen.json').is_file()


def test_source_change_stops_before_any_task_creation(environment):
    root, api, _ = environment; api.source_bad = True
    result = g.run(root, 'mock', deadline=5, interval=1)
    assert not result['all_frozen'] and not business_writes(api)


@pytest.mark.parametrize('change', ['conflict', 'foreign_dependency', 'bad_count', 'missing_heading_dependency', 'wrong_source', 'version', 'source_version', 'self_consistent_text_drift'])
def test_retrieval_gate_checks_fail_before_confirm(environment, change):
    root, api, _ = environment
    def modify(cp):
        manifest = cp['evidence']['manifests'][0]
        if change == 'conflict': cp['conflict_groups'] = [{'conflict_key': 'synthetic'}]
        elif change == 'foreign_dependency': manifest['items'][0]['candidate_citation_ids'] = [999]
        elif change == 'bad_count': manifest['items'].pop()
        elif change == 'missing_heading_dependency': manifest['line_inventory'][0]['context_line_ids'] = ['missing']
        elif change == 'wrong_source': manifest['document_id'] = 2
        elif change == 'version': cp['version'] = False
        elif change == 'source_version': manifest['source_version'] = 'different-upload-hash'
        elif change == 'self_consistent_text_drift':
            manifest['full_text'] = 'X' + manifest['full_text'][1:]
            manifest['full_text_hash'] = g.hashlib.sha256(manifest['full_text'].encode()).hexdigest()
        return cp
    api.cp_change = modify
    result = g.run(root, 'mock', deadline=5, interval=1)
    assert not result['all_frozen']
    assert [call[1] for call in business_writes(api)] == ['/api/tasks', '/api/tasks/40/generate']


@pytest.mark.parametrize('method,path', [('POST', '/api/tasks/19/generate'), ('POST', '/api/tasks/40/test-points/confirm'),
    ('POST', '/api/tasks/40/test-points/retry'), ('POST', '/api/documents/3/ingest'), ('PUT', '/api/tasks/40'),
    ('DELETE', '/api/tasks'), ('GET', '/api/tasks/19'), ('GET', '/api/users')])
def test_forbidden_endpoints_old_ids_and_unbound_writes(environment, tmp_path, method, path):
    _, api, _ = environment
    client = g.Client(g.base.Archive(tmp_path / 'wire'))
    client.task_ids |= {19, 40}
    client.permit = {'path': path, 'body_sha256': g.digest({}), 'used': False}
    with pytest.raises(g.Error): client.request(method, path, {})
    assert not api.calls


def test_confirmed_snapshot_content_drift_is_rejected(environment, tmp_path):
    root, api, _ = environment
    assert g.run(root, 'mock', deadline=5, interval=1)['all_frozen']
    manifest = api.checkpoints[40]['evidence']['manifests'][0]
    manifest['full_text'] = 'X' + manifest['full_text'][1:]
    manifest['full_text_hash'] = g.hashlib.sha256(manifest['full_text'].encode()).hexdigest()
    client = api.client; client.archive = g.base.Archive(tmp_path / 'drift-check')
    owner = root / 'chapters/AUC'
    with pytest.raises(g.Error, match='preflight reconstruction'):
        g.freeze(client, owner, g.CHAPTERS[0], g.read(owner / 'task.json'), g.read(root / 'source-metadata.json'))
    assert not (tmp_path / 'drift-check/proposal.json').exists()


def test_flock_refuses_second_owner(tmp_path):
    with g.lock_run(tmp_path):
        with pytest.raises(g.Error, match='another process'):
            with g.lock_run(tmp_path): pass


def test_marker_tamper_refuses_resume_before_credentials(environment):
    root, api, loads = environment
    g.run(root, 'mock', deadline=5, interval=1)
    path = root / 'chapters/AUC/generate.started.json'; value = g.read(path); value['body'] = {'tampered': True}
    path.write_text(json.dumps(value))
    before = len(api.calls), len(loads)
    with pytest.raises(g.Error, match='marker hash'): g.run(root, 'mock', resume=True)
    assert (len(api.calls), len(loads)) == before


def test_deadline_is_bounded_and_does_not_regenerate(environment, monkeypatch):
    root, api, _ = environment
    clock = iter(range(10000)); monkeypatch.setattr(g.time, 'monotonic', lambda: next(clock))
    outcome = g.run(root, 'mock', deadline=0.01, interval=1)
    assert not outcome['all_frozen'] and outcome['failure'] is not None
    assert [call[1] for call in business_writes(api)] == ['/api/tasks', '/api/tasks/40/generate']
