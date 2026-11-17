"""Offline preparation acceptance: realistic API shapes, no service/DB/model access."""
import importlib.util
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from http.cookiejar import Cookie
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parent))
SPEC = importlib.util.spec_from_file_location('prepare_sources', Path(__file__).with_name('prepare_sources.py'))
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


class Response:
    status = 200

    def __init__(self, value, status=200):
        self.status = status
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


PLAN = {'name': 'Coverage runtime isolated', 'slug': 'coverage-runtime-isolated', 'description': 'Runtime validation'}
STAMP = '2026-10-18T10:00:00'


def fixture(tmp_path):
    paths = (tmp_path / p.SOURCE_PATHS[0].name, tmp_path / p.SOURCE_PATHS[1].name)
    paths[0].write_text('# Original checklist\n\n## 2. 单笔成交金额\n\noriginal-only-business-text\n', encoding='utf-8')
    paths[1].write_bytes(b'PK-mock-original-docx-bytes')
    sources = [p.source_info(path) for path in paths]
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    attempt = run / 'attempt'
    attempt.mkdir(mode=0o700)
    owner = p.DurableArchive(run)
    c = p.PreparationClient(p.DurableArchive(attempt), owner)
    c.archive.secrets = owner.secrets
    api = API(c, paths, sources)
    c.opener = api
    return c, api, paths, sources


class API:
    def __init__(self, client, paths, sources):
        self.client, self.paths, self.sources = client, paths, sources
        self.plan = PLAN
        self.requests, self.docs = [], {}
        self.texts = [paths[0].read_text(encoding='utf-8'), 'original rules parsed text\n']
        self.job_status = 'success'
        self.fail_path = None
        self.network_fail = False
        self.gap = False
        self.preview_mismatch = False
        self.wrong_hash = False
        self.shared = False
        self.external = False
        self.pending_once = False

    def project(self):
        return dict(self.plan, id=19, status='active', default_wiki_space_id=23,
                    private_spaces=[{'id': 23, 'name': 'private', 'slug': 'private-wiki', 'scope': 'project',
                                     'namespace': None, 'status': 'active', 'enabled': True, 'priority': None}],
                    shared_spaces=[{'id': 1, 'enabled': True}] if self.shared else [],
                    created_at=STAMP, updated_at=STAMP)

    def job(self, index, status):
        return {'id': 51 + index, 'document_id': 31 + index, 'space_id': 23, 'space_name': 'private',
                'status': status, 'stage': 'ready' if status.startswith('success') else status,
                'progress': 100 if status.startswith('success') else 0, 'cancel_requested': False,
                'plan_json': '{"api_key":"unsafe-job-plan-key"}', 'model_ref': 'configured',
                'prompt_version_ref': '1', 'step_log_json': '["unsafe-server-log"]',
                'error_message': 'unsafe-error-body', 'created_at': STAMP, 'updated_at': STAMP}

    def open(self, req, timeout):
        self.requests.append(req)
        parsed = urlsplit(req.full_url)
        path, query, method = parsed.path, parse_qs(parsed.query), req.method
        assert req.get_header('Origin') == 'http://127.0.0.1:5173'
        if method == 'POST' and path not in {'/api/auth/login', '/api/auth/logout'}:
            assert req.get_header('X-csrf-token') == 'csrf-long-secret'
            label = 'project' if path == '/api/projects' else (
                'upload-' + str(len(self.docs)) if path == '/api/documents' else
                'ingest-' + str(int(path.split('/')[3]) - 31))
            assert (self.client.owner.path / (label + '.started.json')).is_file()
        if self.fail_path == path and method == 'POST':
            if self.network_fail:
                raise URLError('unsafe-password-or-key')
            raise HTTPError(req.full_url, 503, 'unsafe-error-password', {}, io.BytesIO(b'unsafe-error-password'))
        if path == '/api/health':
            return Response({'status': 'ok'})
        if path == '/api/auth/login':
            assert json.loads(req.data) == {'username': 'operator-long-name', 'password': 'password-long-secret'}
            self.client.jar.set_cookie(cookie('casegen_session', 'session-long-secret'))
            self.client.jar.set_cookie(cookie('casegen_csrf', 'csrf-long-secret'))
            return Response({'user': {'username': 'operator-long-name'}, 'token': 'never-login-body'})
        if path == '/api/auth/logout':
            return Response(None, 204)
        if path == '/api/models':
            return Response([{'id': 1, 'name': 'configured', 'model_name': 'test', 'is_default': True,
                              'protocol': 'openai', 'api_key': 'raw-model-key', 'base_url': 'unsafe-url'}])
        if path == '/api/projects':
            assert method == 'POST'
            self.plan = json.loads(req.data)
            assert set(self.plan) == {'name', 'slug', 'description'}
            assert self.plan['slug'].startswith('coverage-runtime-')
            assert len(self.plan['slug']) <= 64 and len(self.plan['name']) <= 120
            assert not query
            return Response(self.project(), 201)
        assert query['project_id'] == ['19'] and query['space_id'] == ['23']
        if path == '/api/projects/19':
            return Response(self.project())
        if path == '/api/wiki-spaces/23':
            return Response({'id': 23, 'project_id': 19, 'scope': 'project', 'status': 'active', 'name': 'private'})
        if path == '/api/projects/19/external-wiki':
            return Response({'enabled': True, 'external_project_id': 'remote' if self.external else '',
                             'api_key': 'external-key-must-not-archive'})
        if path == '/api/documents' and method == 'GET':
            return Response(list(self.docs.values()))
        if path == '/api/documents' and method == 'POST':
            index = len(self.docs)
            message = BytesParser(policy=default).parsebytes(
                ('Content-Type: ' + req.get_header('Content-type') + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + req.data)
            parts = list(message.iter_parts())
            assert parts[0].get_param('name', header='Content-Disposition') == 'space_id'
            assert parts[0].get_payload(decode=True) == b'23'
            assert parts[1].get_filename() == self.paths[index].name
            assert parts[1].get_payload(decode=True) == self.paths[index].read_bytes()
            assert (self.client.owner.path / 'project.mapped.json').is_file()
            doc = {'id': 31 + index, **self.sources[index], 'space_id': 23, 'space_name': 'private',
                   'status': 'parsed', 'stored_path': 'raw/hidden', 'content_type': 'application/octet-stream',
                   'char_count': len(self.texts[index]), 'error_message': None,
                   'created_at': STAMP, 'updated_at': STAMP}
            if self.wrong_hash:
                doc['sha256'] = '0' * 64
            self.docs[31 + index] = doc
            return Response(doc)
        if path.startswith('/api/documents/'):
            doc_id = int(path.split('/')[3])
            index = doc_id - 31
            assert doc_id in self.docs
            if path.endswith('/ingest'):
                assert method == 'POST' and 'force' not in query
                assert (self.client.owner.path / f'upload-{index}.mapped.json').is_file()
                return Response(self.job(index, 'queued'))
            if path.endswith('/chunks'):
                text = self.texts[index]
                return Response([{'id': 71 + index, 'document_id': doc_id, 'space_id': 23,
                                  'chunk_index': 0, 'title': 'original', 'text': text,
                                  'start_char': 1 if self.gap else 0, 'end_char': len(text),
                                  'section': '', 'clause_ids_json': '[]', 'parent_index': None,
                                  'page_start': None, 'page_end': None, 'created_at': STAMP}])
            if path.endswith('/preview'):
                assert query['max_chars'] == ['200000']
                text = self.texts[index] + ('!' if self.preview_mismatch else '')
                return Response({'document_id': doc_id, 'filename': self.sources[index]['filename'],
                                 'text': text, 'char_count': len(text), 'returned_chars': len(text),
                                 'truncated': False, 'quality_ok': True, 'diagnostics': {}})
            return Response(dict(self.docs[doc_id], status='ready'))
        if path.startswith('/api/ingest-jobs/'):
            index = int(path.split('/')[-1]) - 51
            if self.pending_once:
                self.pending_once = False
                return Response(self.job(index, 'running'))
            return Response(self.job(index, self.job_status))
        raise AssertionError('unhandled mock endpoint')


def login(c):
    c.login('operator-long-name', 'password-long-secret')


def archives(run):
    return '\n'.join(f.read_text() for f in run.rglob('*.json'))


def test_dry_run_zero_http_and_no_stdin_or_source_reads(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(p, 'ROOT', tmp_path)
    monkeypatch.setattr(p, 'PreparationClient', lambda *a, **kw: pytest.fail('zero HTTP'))
    monkeypatch.setattr(p, 'source_info', lambda *a: pytest.fail('no source reads in dry-run'))
    monkeypatch.setattr(p.sys, 'stdin', None)
    assert p.main(['--credentials-stdin']) == 0
    assert json.loads(capsys.readouterr().out)['http_requests'] == 0
    assert not list(tmp_path.iterdir())


def test_missing_credentials_zero_http_and_no_project(monkeypatch, tmp_path):
    monkeypatch.setattr(p, 'ROOT', tmp_path)
    monkeypatch.delenv('CASEGEN_USERNAME', raising=False)
    monkeypatch.delenv('CASEGEN_PASSWORD', raising=False)
    monkeypatch.setattr(p, 'PreparationClient', lambda *a, **kw: pytest.fail('zero HTTP'))
    with pytest.raises(p.ValidationError, match='credentials missing.*zero HTTP'):
        p.main(['--execute'])
    assert not list(tmp_path.iterdir())


def test_success_real_shapes_marker_mapping_hashes_and_private_manifest(tmp_path, monkeypatch):
    c, api, paths, sources = fixture(tmp_path)
    monkeypatch.setattr(p.time, 'sleep', lambda _: None)
    api.pending_once = True
    login(c)
    c.request('GET', '/api/models', project_response=lambda xs: [p.pick(x, ('id', 'model_name')) for x in xs])
    manifest = p.prepare(c, paths, sources, PLAN)
    c.archive.save('manifest.json', manifest)
    c.logout()
    assert manifest['project_id'] == 19 and manifest['space_id'] == 23
    assert [x['document_id'] for x in manifest['sources']] == [31, 32]
    assert [x['job_id'] for x in manifest['sources']] == [51, 52]
    assert [x['sha256'] for x in manifest['sources']] == [x['sha256'] for x in sources]
    assert manifest['sources'][0]['section_verified'] is True
    assert manifest['model_effectiveness_validated'] is False
    assert manifest['scenario_quality_judged'] is False
    assert 'unknown' in manifest['model_calls']
    assert '--project-id 19 --space-id 23' in manifest['validate_command']
    for name in ('project', 'upload-0', 'upload-1', 'ingest-0', 'ingest-1'):
        assert json.loads((c.owner.path / (name + '.mapped.json')).read_text())['id'] > 0
        assert (c.owner.path / (name + '.started.json')).stat().st_mode & 0o777 == 0o600
    contents = archives(c.owner.path)
    for secret in ('operator-long-name', 'password-long-secret', 'session-long-secret', 'csrf-long-secret',
                   'never-login-body', 'raw-model-key', 'external-key-must-not-archive', 'unsafe-job-plan-key',
                   'unsafe-server-log', 'unsafe-error-body', 'original-only-business-text', 'PK-mock-original-docx-bytes'):
        assert secret not in contents
    assert 'multipart/form-data' not in contents
    assert not list(c.jar)


@pytest.mark.parametrize('endpoint', ['/api/projects', '/api/documents', '/api/documents/31/ingest'])
@pytest.mark.parametrize('network', [False, True])
def test_failed_or_uncertain_write_never_replayed(tmp_path, endpoint, network):
    c, api, paths, sources = fixture(tmp_path)
    login(c)
    api.fail_path, api.network_fail = endpoint, network
    with pytest.raises(p.ValidationError, match='HTTP error|network error'):
        p.prepare(c, paths, sources, PLAN)
    before = len(api.requests)
    with pytest.raises(p.ValidationError, match='already attempted'):
        p.prepare(c, paths, sources, PLAN)
    assert len(api.requests) == before
    with pytest.raises(p.ValidationError, match='uncertain write'):
        p.resume_records(c, sources, PLAN)
    assert len(api.requests) == before
    assert 'unsafe-error-password' not in archives(c.owner.path)
    assert 'unsafe-password-or-key' not in archives(c.owner.path)


def test_resume_after_success_only_gets_business_endpoints(tmp_path):
    c, api, paths, sources = fixture(tmp_path)
    login(c)
    first = p.prepare(c, paths, sources, PLAN)
    api.requests.clear()
    second = p.prepare(c, paths, sources, PLAN, resume=True)
    assert first == second
    assert all(r.method == 'GET' for r in api.requests)
    with pytest.raises(p.ValidationError, match='resume observes only'):
        c.write_once('new', '/api/projects', PLAN, p.PROJECT_FIELDS)


@pytest.mark.parametrize('endpoint', ['/api/documents/2/ingest', '/api/documents/31/rechunk', '/api/tasks',
                                     '/api/models', '/api/projects/3/shared-wikis/2', '/api/ingest-jobs/51/retry-failed-windows'])
def test_preparation_cannot_write_old_docs_or_arbitrary_api(tmp_path, endpoint):
    c, api, paths, sources = fixture(tmp_path)
    login(c)
    before = len(api.requests)
    with pytest.raises(p.ValidationError, match='forbidden|outside'):
        c.request('POST', endpoint, {})
    assert len(api.requests) == before


@pytest.mark.parametrize('option,diagnostic', [('gap', 'chunk gap'), ('preview_mismatch', 'differ'),
                                              ('wrong_hash', 'source hash mismatch'), ('shared', 'isolation mismatch'),
                                              ('external', 'external KB')])
def test_source_integrity_and_isolation_fail_closed(tmp_path, option, diagnostic):
    c, api, paths, sources = fixture(tmp_path)
    setattr(api, option, True)
    login(c)
    with pytest.raises(p.ValidationError, match=diagnostic):
        p.prepare(c, paths, sources, PLAN)
    assert not (c.archive.path / 'manifest.json').exists()
    if option == 'wrong_hash':
        assert (c.owner.path / 'upload-0.mapped.json').is_file()
        assert not (c.owner.path / 'ingest-0.started.json').exists()


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'unexpected'])
def test_job_failure_stops_without_retry_or_model_quality_claim(tmp_path, status):
    c, api, paths, sources = fixture(tmp_path)
    api.job_status = status
    login(c)
    with pytest.raises(p.ValidationError, match='no retry'):
        p.prepare(c, paths, sources, PLAN)
    assert len([r for r in api.requests if r.full_url.split('?')[0].endswith('/ingest')]) == 2
    api.requests.clear()
    with pytest.raises(p.ValidationError, match='no retry'):
        p.prepare(c, paths, sources, PLAN, resume=True)
    assert all(r.method == 'GET' for r in api.requests)


def test_resume_partial_mapped_run_stops_without_http(tmp_path):
    c, api, paths, sources = fixture(tmp_path)
    c.owner.save('project.started.json', PLAN)
    c.owner.save('project.mapped.json', api.project())
    with pytest.raises(p.ValidationError, match='required preparation record missing'):
        p.resume_records(c, sources, PLAN)
    assert api.requests == []


def test_source_change_refuses_upload_before_marker(tmp_path):
    c, api, paths, sources = fixture(tmp_path)
    paths[0].write_text('changed', encoding='utf-8')
    login(c)
    with pytest.raises(p.ValidationError, match='source changed before upload'):
        p.prepare(c, paths, sources, PLAN)
    assert not (c.owner.path / 'upload-0.started.json').exists()
    assert not api.docs


def test_bounded_poll_stops_with_ids_and_resume_does_not_ingest(tmp_path, monkeypatch):
    c, api, paths, sources = fixture(tmp_path)
    login(c)
    clock = iter([0, 2])
    # Shared transport also uses the clock; inject expiration after all writes.
    original_observe = p.observe
    def expire(*a, **kwargs):
        monkeypatch.setattr(p.time, 'monotonic', lambda: next(clock))
        return original_observe(*a, **kwargs)
    monkeypatch.setattr(p, 'observe', expire)
    with pytest.raises(p.ValidationError, match='expired'):
        p.prepare(c, paths, sources, PLAN, deadline=1)
    assert (c.owner.path / 'ingest-1.mapped.json').is_file()
    assert len([r for r in api.requests if r.method == 'POST' and '/ingest?' in r.full_url]) == 2


def test_scenario_client_still_rejects_all_preparation_writes(tmp_path):
    c = p.Client(p.Archive(tmp_path), 3, 2)
    for endpoint in ('/api/projects', '/api/documents', '/api/documents/2/ingest', '/api/documents/2/rechunk'):
        with pytest.raises(p.ValidationError, match='scenario-only'):
            c.request('POST', endpoint, {})
    assert c.archive.calls == 0


@pytest.mark.parametrize('use_stdin', [False, True])
def test_cli_execute_safe_credentials_and_resume_observes_only(tmp_path, monkeypatch, capsys, use_stdin):
    seed, _, paths, sources = fixture(tmp_path)
    monkeypatch.setattr(p, 'ROOT', tmp_path)
    monkeypatch.setattr(p, 'SOURCE_PATHS', paths)
    created = []
    real_client = p.PreparationClient

    def make_client(archive, owner):
        client = real_client(archive, owner)
        api = API(client, paths, sources)
        if created:
            api.docs = created[0][1].docs
            api.plan = created[0][1].plan
        client.opener = api
        created.append((client, api))
        return client
    monkeypatch.setattr(p, 'PreparationClient', make_client)
    if use_stdin:
        monkeypatch.delenv('CASEGEN_USERNAME', raising=False)
        monkeypatch.delenv('CASEGEN_PASSWORD', raising=False)
        monkeypatch.setattr(p.sys, 'stdin', io.StringIO('operator-long-name\npassword-long-secret\n'))
    else:
        monkeypatch.setenv('CASEGEN_USERNAME', 'operator-long-name')
        monkeypatch.setenv('CASEGEN_PASSWORD', 'password-long-secret')
    argv = ['--execute'] + (['--credentials-stdin'] if use_stdin else [])
    assert p.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['completed'] and result['project_id'] == 19 and result['space_id'] == 23
    run = Path(result['run'])
    manifest = json.loads(next(run.glob('attempt-*/manifest.json')).read_text())
    assert [x['sha256'] for x in manifest['sources']] == [x['sha256'] for x in sources]
    assert run.stat().st_mode & 0o777 == 0o700
    for file in run.rglob('*.json'):
        assert file.stat().st_mode & 0o777 == 0o600
    archived = archives(run)
    for secret in ('password-long-secret', 'session-long-secret', 'csrf-long-secret', 'raw-model-key', 'never-login-body'):
        assert secret not in archived
    if use_stdin:
        monkeypatch.setattr(p.sys, 'stdin', io.StringIO('operator-long-name\npassword-long-secret\n'))
    assert p.main(argv + ['--resume', str(run)]) == 0
    assert all(r.method == 'GET' or urlsplit(r.full_url).path in {'/api/auth/login', '/api/auth/logout'}
               for r in created[-1][1].requests)
    capsys.readouterr()
    paths[0].write_text('changed source', encoding='utf-8')
    if use_stdin:
        monkeypatch.setattr(p.sys, 'stdin', io.StringIO('operator-long-name\npassword-long-secret\n'))
    with pytest.raises(p.ValidationError, match='source hashes changed'):
        p.main(argv + ['--resume', str(run)])
    assert len(created) == 2


def test_cli_uncertain_resume_stops_before_login(tmp_path, monkeypatch, capsys):
    seed, _, paths, sources = fixture(tmp_path)
    monkeypatch.setattr(p, 'ROOT', tmp_path)
    monkeypatch.setattr(p, 'SOURCE_PATHS', paths)
    monkeypatch.setenv('CASEGEN_USERNAME', 'operator-long-name')
    monkeypatch.setenv('CASEGEN_PASSWORD', 'password-long-secret')
    run = tmp_path / 'data/validation/prepare-sources-uncertain'
    run.mkdir(parents=True)
    archive = p.DurableArchive(run)
    archive.save('sources.json', sources)
    archive.save('plan.json', PLAN)
    archive.save('project.started.json', PLAN)
    monkeypatch.setattr(p.PreparationClient, 'request', lambda *a, **kw: pytest.fail('uncertain resume must be zero HTTP'))
    assert p.main(['--execute', '--resume', str(run)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['http_calls'] == 0 and 'uncertain write' in output['visible_gaps'][0]


def test_missing_section_rejected_after_ready(tmp_path):
    c, api, paths, sources = fixture(tmp_path)
    api.texts[0] = '# Checklist without requested section\n'
    login(c)
    with pytest.raises(p.ValidationError, match='section absent'):
        p.prepare(c, paths, sources, PLAN)


def test_success_with_warnings_is_retained_without_quality_claim(tmp_path):
    c, api, paths, sources = fixture(tmp_path)
    api.job_status = 'success_with_warnings'
    login(c)
    manifest = p.prepare(c, paths, sources, PLAN)
    assert all(x['job_status'] == 'success_with_warnings' for x in manifest['sources'])
    assert not manifest['model_effectiveness_validated']
