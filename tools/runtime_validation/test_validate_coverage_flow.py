"""Offline behavioral tests: no app.main, production DB, network, or real model."""
import importlib.util
from http.cookiejar import Cookie
from pathlib import Path
import json
from email.message import Message
from urllib.error import HTTPError
import io

import pytest

SPEC = importlib.util.spec_from_file_location('runtime_validation', Path(__file__).with_name('validate_coverage_flow.py'))
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)


class Response:
    status = 200

    def __init__(self, value):
        self.raw = io.BytesIO(json.dumps(value).encode())
        self.headers = Message()
        self.headers['Content-Type'] = 'application/json'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.raw.read(size)


def cookie(name, value):
    return Cookie(0, name, value, None, False, '127.0.0.1', False, False,
                  '/', True, False, None, True, None, None, {})


def client(tmp_path):
    return v.Client(v.Archive(tmp_path), 9, 12)


def test_normal_login_origin_csrf_and_no_secret_archive(tmp_path):
    c = client(tmp_path)
    seen = []

    class Opener:
        def open(self, req, timeout):
            seen.append(req)
            if '/auth/login?' in req.full_url:
                assert json.loads(req.data) == {'username': 'operator-name', 'password': 'pw-long-secret'}
                c.jar.set_cookie(cookie('casegen_session', 'session-long-secret'))
                c.jar.set_cookie(cookie('casegen_csrf', 'csrf-long-secret'))
                return Response({'user': {'username': 'operator-name'}, 'session': 'session-long-secret'})
            return Response({'status': 'ok'})
    c.opener = Opener()
    c.login('operator-name', 'pw-long-secret')
    c.request('POST', '/api/tasks', {'title': 'runtime'})
    assert seen[-1].get_header('Origin') == 'http://127.0.0.1:5173'
    assert seen[-1].get_header('X-csrf-token') == 'csrf-long-secret'
    c.logout()
    assert not list(c.jar)
    archived = ''.join(p.read_text() for p in tmp_path.rglob('*.json'))
    for secret in ('operator-name', 'pw-long-secret', 'session-long-secret', 'csrf-long-secret'):
        assert secret not in archived
    assert '/api/auth/login?' in archived


def test_models_projection_drops_even_unmasked_key_before_archive(tmp_path):
    c = client(tmp_path)
    c.opener = type('Opener', (), {'open': lambda self, req, timeout:
        Response([{'id': 1, 'name': 'model', 'api_key': 'raw-model-secret'}])})()
    result = c.request('GET', '/api/models', project_response=lambda xs: [v.pick(x, ('id', 'name')) for x in xs])
    assert result == [{'id': 1, 'name': 'model'}]
    assert 'raw-model-secret' not in ''.join(p.read_text() for p in tmp_path.rglob('*.json'))
    assert v.sanitize({'detail_json': '{"resume_claim_token":"hidden","ok":1}'}) == {'detail_json': '{"ok": 1}'}


def test_echoed_secret_refuses_entire_business_archive(tmp_path):
    c = client(tmp_path)
    c.archive.secrets.append('long-sensitive-value')
    c.opener = type('Opener', (), {'open': lambda self, req, timeout: Response({'error_message': 'long-sensitive-value'})})()
    with pytest.raises(v.ValidationError, match='archive refused'):
        c.request('GET', '/api/tasks/1')
    assert len(list(tmp_path.glob('calls/*.json'))) == 1  # started record only
    assert 'long-sensitive-value' not in next(tmp_path.glob('calls/*.json')).read_text()


def test_real_http_failure_keeps_status_without_response_body(tmp_path):
    c = client(tmp_path)

    class Opener:
        def open(self, req, timeout):
            raise HTTPError(req.full_url, 422, 'unsafe-secret', {}, io.BytesIO(b'unsafe-secret'))
    c.opener = Opener()
    with pytest.raises(v.ValidationError, match='HTTP error'):
        c.request('POST', '/api/tasks', {'title': 'scope'})
    record = json.loads((tmp_path / 'calls/00001.json').read_text())
    assert record['status'] == 422
    assert 'unsafe-secret' not in json.dumps(record)


class FlowClient:
    project, space = 9, 12

    def __init__(self, tmp_path, statuses):
        self.archive = v.Archive(tmp_path / 'attempt')
        self.archive.path.mkdir()
        self.statuses = iter(statuses)
        self.requests = []

    def request(self, method, path, data=None):
        self.requests.append((method, path, data))
        self.archive.calls += 1
        if path.endswith('/retrieval-checkpoint'):
            return {'id': 8, 'version': 1, 'task_id': 41, 'status': 'pending', 'candidate_citations': [
                {'id': 3, 'citation_type': 'source', 'source_chunk_id': 5},
                {'id': 4, 'citation_type': 'source', 'source_chunk_id': 999},
                {'id': 6, 'citation_type': 'source', 'source_chunk_id': 5, 'legacy': True}]}
        if path.endswith('/test-points'):
            return {'id': 10, 'task_id': 41, 'status': 'pending',
                    'coverage_metadata': {'status': 'incomplete', 'warnings': ['model returned invalid branch'], 'actual_calls': 7},
                    'points': [{'id': 1, 'title': '=formula title', 'verification_goal': '条件→行为', 'citation_ids': [3]}]}
        if path.endswith('/events') or path.endswith('/citations'):
            return []
        status = next(self.statuses) if method == 'GET' else 'draft'
        return {'id': 41, 'project_id': 9, 'wiki_space_id': 12, 'status': status,
                'error_message': 'model failure' if status == 'failed' else None}


def metadata():
    return {'model_id': 1, 'prompt_id': 2, 'chunk_ids': [5], 'pages': [], 'documents': [{'id': 2}]}


def test_flow_confirms_only_retrieval_freezes_complete_points_and_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(v.time, 'sleep', lambda _: None)
    c = FlowClient(tmp_path, ['awaiting_confirmation', 'awaiting_test_point_confirmation'])
    result = v.observe(c, tmp_path, metadata())
    writes = [(method, path, data) for method, path, data in c.requests if method == 'POST']
    assert [w[1] for w in writes] == ['/api/tasks', '/api/tasks/41/generate', '/api/tasks/41/retrieval-checkpoint/confirm']
    assert writes[-1][2]['selected_citation_ids'] == [3]
    assert result['status'] == 'incomplete'
    proposal = json.loads((c.archive.path / 'proposal.json').read_text())
    assert proposal['points'][0]['verification_goal'] == '条件→行为'
    assert "'=formula title" in (c.archive.path / 'scenarios.csv').read_text(encoding='utf-8-sig')
    assert 'model returned invalid branch' in (c.archive.path / 'coverage-metadata.json').read_text()
    assert '回归章节：《2. 单笔成交金额》' in writes[0][2]['description']


@pytest.mark.parametrize('status', ['failed', 'generated', 'draft', 'stopped'])
def test_terminal_or_unexpected_state_never_regenerates(tmp_path, status):
    c = FlowClient(tmp_path, [status])
    with pytest.raises(v.ValidationError, match='visible gap retained'):
        v.observe(c, tmp_path, metadata())
    assert len([r for r in c.requests if r[1] == '/api/tasks']) == 1
    assert json.loads((c.archive.path / 'state-flow.json').read_text())['states'][0]['status'] == status
    assert not (c.archive.path / 'scenarios.json').exists()


def test_resume_only_observes_recorded_task_no_new_creation_or_confirmation(tmp_path):
    (tmp_path / 'task.json').write_text(json.dumps({'task_id': 41, 'project_id': 9, 'space_id': 12}))
    c = FlowClient(tmp_path, ['awaiting_confirmation'])
    with pytest.raises(v.ValidationError, match='observe-only'):
        v.observe(c, tmp_path, None, resume=True)
    assert all(method == 'GET' for method, _, _ in c.requests)
    assert c.requests[0][1] == '/api/tasks/41'


def test_uncertain_create_marker_prevents_duplicate_creation(tmp_path):
    (tmp_path / 'create.started.json').write_text('{}')
    c = FlowClient(tmp_path, [])
    with pytest.raises(v.ValidationError, match='never create twice'):
        v.observe(c, tmp_path, metadata())
    assert c.requests == []
    with pytest.raises(v.ValidationError, match='requires recorded task ID'):
        v.observe(c, tmp_path, None, resume=True)


def test_scenario_confirmation_endpoint_cannot_be_called(tmp_path):
    c = client(tmp_path)
    with pytest.raises(v.ValidationError, match='forbidden'):
        c.request('POST', '/api/tasks/41/test-points/confirm', {})
    assert c.archive.calls == 0


def test_missing_credentials_never_attempts_login(tmp_path):
    c = client(tmp_path)
    with pytest.raises(v.ValidationError, match='credentials missing'):
        c.login(None, None)
    assert c.archive.calls == 0


class MetadataClient:
    project, space = 9, 12

    def __init__(self, *, checklist=True, gap=False):
        self.checklist, self.gap = checklist, gap
        self.text = '# 竞价基本功能测试\n\n## 2. 单笔成交金额\n\n| 场景 | 内容 |\n| --- | --- |\n| 1 | 用户原文 |\n'

    def request(self, method, path, *, project_response=None):
        responses = {
            '/api/projects/9': {'id': 9, 'status': 'active', 'shared_spaces': []},
            '/api/wiki-spaces/12': {'id': 12, 'status': 'active', 'project_id': 9, 'scope': 'project'},
            '/api/models': [{'id': 1, 'name': 'model', 'is_default': True, 'api_key': 'never-store-model-key'}],
            '/api/prompts': [{'id': i, 'type': kind, 'content': 'template', 'is_active': True}
                             for i, kind in enumerate(('generate', 'test_points'), 1)],
            '/api/documents': [{'id': 2, 'space_id': 12, 'status': 'ready', 'char_count': len(self.text),
                                'filename': v.CHECKLIST if self.checklist else 'only-rules.docx'}],
            '/api/wiki/pages': [],
            '/api/projects/9/external-wiki': {'enabled': True, 'external_project_id': ''},
            '/api/documents/2/chunks': [{'id': 5, 'chunk_index': 0, 'start_char': 1 if self.gap else 0,
                                        'end_char': len(self.text), 'text': self.text}],
        }
        value = responses[path]
        return project_response(value) if project_response else value


def test_original_kb_preflight_handles_unbound_default_external_config():
    result = v.source_metadata(MetadataClient())
    assert result['chunk_ids'] == [5]
    assert result['model_id'] == 1
    assert 'api_key' not in result['models'][0]
    assert 'content' not in result['prompts'][0]


@pytest.mark.parametrize('options,diagnostic', [({'checklist': False}, 'checklist missing'),
                                               ({'gap': True}, 'chunks incomplete')])
def test_ready_is_insufficient_without_original_checklist_and_lossless_chunks(options, diagnostic):
    with pytest.raises(v.ValidationError, match=diagnostic):
        v.source_metadata(MetadataClient(**options))


def test_execute_without_credentials_records_gap_and_never_creates_task(tmp_path, monkeypatch):
    monkeypatch.setattr(v, 'ROOT', tmp_path)
    monkeypatch.delenv('CASEGEN_USERNAME', raising=False)
    monkeypatch.delenv('CASEGEN_PASSWORD', raising=False)
    requested = []

    class Opener:
        def open(self, req, timeout):
            requested.append(req.full_url)
            return Response({'status': 'ok'})
    monkeypatch.setattr(v, 'build_opener', lambda *args: Opener())
    assert v.main(['--execute', '--project-id', '9', '--space-id', '12']) == 1
    assert len(requested) == 1 and '/api/health?' in requested[0]
    outcome = json.loads(next(tmp_path.glob('data/validation/*/attempt-*/outcome.json')).read_text())
    assert outcome['completed'] is False
    assert outcome['visible_gaps'] == ['normal login credentials missing']
    assert not list(tmp_path.rglob('task.json'))


def retry_setup(tmp_path, *, stale=None, fail=False):
    (tmp_path / 'task.json').write_text(json.dumps({'task_id': 41, 'project_id': 9, 'space_id': 12}))
    c = client(tmp_path / 'attempt')
    proposal = {'id': 10, 'task_id': 41, 'status': 'pending', 'version': 2, 'attempt': 1,
                'retrieval_checkpoint_id': 8, 'points': [], 'coverage_metadata': {}}
    if stale:
        proposal.update(stale)
    seen = []
    sent = False
    class Opener:
        def open(self, req, timeout):
            nonlocal sent
            path = req.full_url.split('?')[0].removeprefix(c.root)
            seen.append((req.method, path, json.loads(req.data) if req.data else None))
            if path.endswith('/retry'):
                sent = True
                if fail:
                    raise TimeoutError()
                return Response({'id': 41, 'status': 'generating_test_points'})
            if path.endswith('/test-points'):
                return Response({**proposal, 'id': 11, 'attempt': 2} if sent else proposal)
            if path.endswith('/events') or path.endswith('/citations'):
                return Response([])
            return Response({'id': 41, 'project_id': 9, 'wiki_space_id': 12,
                             'status': 'awaiting_test_point_confirmation'})
    c.opener = Opener()
    return c, seen, proposal


def test_retry_is_off_by_default_and_resume_rejects_mutation(tmp_path):
    c = client(tmp_path)
    with pytest.raises(v.ValidationError, match='forbidden'):
        c.request('POST', '/api/tasks/41/test-points/retry', {})
    c.observe_only = True
    for path in ('/api/tasks', '/api/tasks/41/generate', '/api/tasks/41/retrieval-checkpoint/confirm'):
        with pytest.raises(v.ValidationError, match='observe-only'):
            c.request('POST', path, {})
    assert c.archive.calls == 0


@pytest.mark.parametrize('flags', [[], ['--execute'], ['--resume', 'unused'],
                                    ['--execute', '--resume', 'unused', '--expected-version', '0']])
def test_retry_cli_requires_execute_resume_and_paired_positive_flags(flags):
    with pytest.raises(SystemExit):
        v.main(['--project-id', '9', '--space-id', '12', '--retry-checkpoint', '10'] + flags)


@pytest.mark.parametrize('stale', [{'id': 99}, {'task_id': 99}, {'version': 3},
                                 {'status': 'confirmed'}, {'retrieval_checkpoint_id': None}])
def test_retry_stale_identity_version_status_never_posts(tmp_path, stale):
    c, seen, _ = retry_setup(tmp_path, stale=stale)
    with pytest.raises(v.ValidationError, match='checkpoint identity/version/status'):
        v.observe(c, tmp_path, None, resume=True, retry_checkpoint=10, expected_version=2)
    assert all(m == 'GET' for m, _, _ in seen)
    assert not list(tmp_path.glob('retry-*.started.json'))


@pytest.mark.parametrize('fail', [False, True])
def test_explicit_retry_posts_once_then_only_gets_and_same_marker_never_replays(tmp_path, fail):
    c, seen, _ = retry_setup(tmp_path, fail=fail)
    result = v.observe(c, tmp_path, None, resume=True, retry_checkpoint=10, expected_version=2)
    assert result == {}
    marker = json.loads((tmp_path / 'retry-10-v2.started.json').read_text())
    writes = [r for r in seen if r[0] != 'GET']
    assert len(writes) == 1 and writes[0][1] == '/api/tasks/41/test-points/retry'
    assert writes[0][2] == marker['body']
    assert marker['retrieval_checkpoint_id'] == 8 and marker['attempt'] == 1
    post_index = next(i for i, r in enumerate(seen) if r[0] == 'POST')
    assert all(m == 'GET' for m, _, _ in seen[post_index + 1:])
    assert json.loads((c.archive.path / 'proposal.json').read_text())['id'] == 11
    second_archive = tmp_path / 'second-attempt'
    c.archive = v.Archive(second_archive)
    before = len(seen)
    v.observe(c, tmp_path, None, resume=True, retry_checkpoint=10, expected_version=2)
    assert all(m == 'GET' for m, _, _ in seen[before:])
    assert json.loads((tmp_path / 'retry-10-v2.started.json').read_text()) == marker


def test_retry_marker_is_durable_before_post_and_concurrent_marker_blocks_send(tmp_path, monkeypatch):
    c, seen, _ = retry_setup(tmp_path)
    original = v.Archive.save
    def competing_save(self, name, value):
        original(self, name, value)
        if name.startswith('retry-'):
            # Simulate another owner winning the atomic create before this owner.
            original(self, name, value)
    monkeypatch.setattr(v.Archive, 'save', competing_save)
    with pytest.raises(FileExistsError):
        v.observe(c, tmp_path, None, resume=True, retry_checkpoint=10, expected_version=2)
    assert all(m == 'GET' for m, _, _ in seen)


def test_retry_bound_client_rejects_other_task_body_and_second_post(tmp_path):
    c = client(tmp_path)
    c.observe_only = True
    body = {'checkpoint_id': 10, 'expected_version': 2, 'idempotency_key': 'stable-key'}
    c.retry_binding = {'task_id': 41, 'body': body}
    with pytest.raises(v.ValidationError, match='observe-only'):
        c.request('POST', '/api/tasks/42/test-points/retry', body)
    with pytest.raises(v.ValidationError, match='differs'):
        c.request('POST', '/api/tasks/41/test-points/retry', {**body, 'idempotency_key': 'other-key'})
    c.opener = type('Opener', (), {'open': lambda *a, **kw: Response({})})()
    c.request('POST', '/api/tasks/41/test-points/retry', body)
    with pytest.raises(v.ValidationError, match='observe-only'):
        c.request('POST', '/api/tasks/41/test-points/retry', body)
    assert c.archive.calls == 1


def test_retry_preflight_rejects_recorded_scope_before_http(tmp_path):
    c, seen, _ = retry_setup(tmp_path)
    (tmp_path / 'task.json').write_text(json.dumps({'task_id': 41, 'project_id': 99, 'space_id': 12}))
    with pytest.raises(v.ValidationError, match='project/space mismatch'):
        v.observe(c, tmp_path, None, resume=True, retry_checkpoint=10, expected_version=2)
    assert seen == []


def test_retry_persists_binding_before_outbound_request(tmp_path):
    c, seen, _ = retry_setup(tmp_path)
    original = c.opener.open
    def check(req, timeout):
        if req.method == 'POST':
            marker = json.loads((tmp_path / 'retry-10-v2.started.json').read_text())
            assert marker['body'] == json.loads(req.data)
        return original(req, timeout)
    c.opener.open = check
    v.observe(c, tmp_path, None, resume=True, retry_checkpoint=10, expected_version=2)
    assert len([r for r in seen if r[0] == 'POST']) == 1


def test_resume_exclusive_lock_refuses_concurrent_observer_before_http(tmp_path, monkeypatch):
    monkeypatch.setattr(v, 'ROOT', tmp_path)
    run = tmp_path / 'data/validation/coverage-flow-offline'
    run.mkdir(parents=True)
    (run / 'task.json').write_text(json.dumps({'task_id': 41, 'project_id': 9, 'space_id': 12}))
    with (run / '.lock').open('w') as lock:
        v.fcntl.flock(lock, v.fcntl.LOCK_EX | v.fcntl.LOCK_NB)
        monkeypatch.setattr(v.Client, 'request', lambda *a, **kw: pytest.fail('locked run must not send HTTP'))
        with pytest.raises(v.ValidationError, match='another observer'):
            v.main(['--execute', '--resume', str(run), '--project-id', '9', '--space-id', '12',
                    '--retry-checkpoint', '10', '--expected-version', '2'])


def test_uncertain_marker_cannot_freeze_old_checkpoint_or_cross_scope(tmp_path):
    c, seen, _ = retry_setup(tmp_path)
    marker = {'task_id': 41, 'project_id': 9, 'space_id': 12, 'retrieval_checkpoint_id': 8,
              'attempt': 1, 'body': {'checkpoint_id': 10, 'expected_version': 2, 'idempotency_key': 'same-key'}}
    (tmp_path / 'retry-10-v2.started.json').write_text(json.dumps(marker))
    with pytest.raises(v.ValidationError, match='new retry proposal not observed'):
        v.observe(c, tmp_path, None, resume=True, retry_checkpoint=10, expected_version=2)
    assert all(m == 'GET' for m, _, _ in seen)
    assert not (c.archive.path / 'proposal.json').exists()
    marker['project_id'] = 99
    (tmp_path / 'retry-10-v2.started.json').write_text(json.dumps(marker))
    before = len(seen)
    with pytest.raises(v.ValidationError, match='marker identity'):
        v.retry_proposal(c, tmp_path, 41, 10, 2)
    assert len(seen) == before


def test_poll_deadline_leaves_recorded_id_without_retry(tmp_path, monkeypatch):
    c = FlowClient(tmp_path, [])
    clock = iter([0, 10])
    monkeypatch.setattr(v.time, 'monotonic', lambda: next(clock))
    with pytest.raises(v.ValidationError, match='poll deadline exceeded'):
        v.observe(c, tmp_path, metadata(), deadline=1)
    assert json.loads((tmp_path / 'task.json').read_text())['task_id'] == 41
    assert len([r for r in c.requests if r[1] == '/api/tasks']) == 1
