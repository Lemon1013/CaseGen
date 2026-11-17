#!/usr/bin/env python3
"""Single-scope runtime validation. Opt-in execution; never confirms test points."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
from http.client import HTTPException
from http.cookiejar import CookieJar
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 8 * 1024 * 1024
SENSITIVE = {'password', 'password_hash', 'api_key', 'apikey', 'api_secret', 'secret',
             'token', 'access_token', 'refresh_token', 'session', 'session_id',
             'session_token', 'csrf_token', 'resume_claim_token', 'authorization',
             'cookie', 'set_cookie'}
CHECKLIST = '竞价基本功能测试.md'
SECTION = '2. 单笔成交金额'


class ValidationError(Exception):
    """Fixed diagnostics only: never interpolate remote content or credentials."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {k: sanitize(v, secrets) for k, v in value.items()
                if k.lower().replace('-', '_') not in SENSITIVE}
    if isinstance(value, list):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, str):
        if any(secret and secret in value for secret in secrets):
            raise ValidationError('secret echoed in business content; archive refused')
        if value.lstrip().startswith(('{', '[')):
            try:
                embedded = json.loads(value)
            except ValueError:
                pass
            else:
                return json.dumps(sanitize(embedded, secrets), ensure_ascii=False)
    return value


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.secrets = []
        self.calls = 0

    def save(self, name, value):
        data = encode(sanitize(value, self.secrets))
        self.bytes(name, data)

    def bytes(self, name, data):
        # All names are internal constants or numeric sequence names.
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())


class Client:
    def __init__(self, archive, project, space, *, root='http://127.0.0.1:8001',
                 origin='http://127.0.0.1:5173', timeout=30, opener=None):
        if root != 'http://127.0.0.1:8001' or origin not in {
                'http://127.0.0.1:5173', 'http://localhost:5173', root}:
            raise ValidationError('only authorized local service/origin allowed')
        self.archive, self.project, self.space = archive, project, space
        self.root, self.origin, self.timeout = root, origin, timeout
        self.jar = CookieJar()
        self.opener = opener or build_opener(NoRedirect(), HTTPCookieProcessor(self.jar))
        self.logged_in = False
        self.observe_only = False
        self.retry_binding = None
        self.retry_sent = False

    def check_write(self, method, path):
        auth = path in {'/api/auth/login', '/api/auth/logout'} and method == 'POST'
        retry = (self.retry_binding is not None and method == 'POST'
                 and path == f"/api/tasks/{self.retry_binding['task_id']}/test-points/retry"
                 and not self.retry_sent)
        if self.observe_only and method != 'GET' and not (auth or retry):
            raise ValidationError('write forbidden by observe-only resume boundary')
        if retry:
            return
        if method != 'GET' and not (path in {'/api/auth/login', '/api/auth/logout', '/api/tasks'}
                or re.fullmatch(r'/api/tasks/[1-9][0-9]*/(?:generate|retrieval-checkpoint/confirm)', path)):
            raise ValidationError('write endpoint forbidden by scenario-only boundary')

    def endpoint(self, path):
        return path + ('&' if '?' in path else '?') + urlencode({
            'project_id': self.project, 'space_id': self.space, 'wiki_space_id': self.space})

    def wire_body(self, path, data):
        return encode(data) if data is not None else None, 'application/json'

    def request(self, method, path, data=None, *, private=False, project_response=None):
        self.check_write(method, path)
        if method == 'POST' and path.endswith('/test-points/retry'):
            if data != self.retry_binding['body']:
                raise ValidationError('retry body differs from durable binding')
            self.retry_sent = True
        endpoint = self.endpoint(path)
        self.archive.calls += 1
        name = f'calls/{self.archive.calls:05d}'
        record = {'method': method, 'endpoint': endpoint}
        if not private:
            record['request'] = data
        self.archive.save(name + '.started.json', record)
        body, content_type = self.wire_body(path, data)
        headers = {'Origin': self.origin, 'Content-Type': content_type,
                   'Accept': 'application/json', 'Accept-Encoding': 'identity'}
        if method != 'GET':
            csrf_name = os.environ.get('CASEGEN_AUTH_CSRF_COOKIE_NAME', 'casegen_csrf')
            csrf = next((c.value for c in self.jar if c.name == csrf_name), None)
            if csrf:
                headers['X-CSRF-Token'] = csrf
        req = Request(self.root + endpoint, data=body,
                      method=method, headers=headers)
        started = time.monotonic()
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                status = response.status
                if status != 204 and response.headers.get_content_type() != 'application/json':
                    raise ValidationError('expected JSON API response')
                if response.headers.get('Content-Encoding', 'identity') not in ('identity', ''):
                    raise ValidationError('encoded response refused')
                chunks, total = [], 0
                reader = getattr(response, 'read1', response.read)
                while True:
                    if time.monotonic() - started > self.timeout:
                        raise ValidationError('total response read deadline exceeded')
                    chunk = reader(min(65536, MAX_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValidationError('API response too large')
                raw = b''.join(chunks)
        except HTTPError as exc:
            record['status'] = exc.code
            exc.close()  # Do not archive an error body that may echo passwords/keys.
            self.archive.save(name + '.json', record)
            raise ValidationError('API HTTP error; inspect recorded endpoint/status') from None
        except (URLError, OSError, TimeoutError, HTTPException):
            record['status'] = 'network_error'
            self.archive.save(name + '.json', record)
            raise ValidationError('network error; uncertain writes are never retried') from None
        self.archive.secrets.extend(c.value for c in self.jar if c.value not in self.archive.secrets)
        record['status'] = status
        if private:
            # Login response, including user identity, never enters the archive.
            self.archive.save(name + '.json', record)
            return None
        try:
            value = json.loads(raw) if raw else None
        except (ValueError, UnicodeError):
            raise ValidationError('invalid API JSON; raw response not archived') from None
        value = project_response(value) if project_response else value
        value = sanitize(value, self.archive.secrets)
        record['response'] = value
        self.archive.save(name + '.json', record)
        return value

    def login(self, username, password):
        if not username or not password:
            raise ValidationError('normal login credentials missing')
        self.archive.secrets.extend([username, password])
        try:
            self.request('POST', '/api/auth/login', {'username': username, 'password': password}, private=True)
            self.logged_in = True
        finally:
            # A successful cookie issuance can precede a response read failure.
            self.logged_in = self.logged_in or bool(list(self.jar))

    def logout(self):
        try:
            if self.logged_in:
                self.request('POST', '/api/auth/logout', private=True)
        finally:
            self.jar.clear()
            self.logged_in = False


def pick(item, fields):
    return {k: item[k] for k in fields if k in item}


def source_metadata(client):
    project = client.request('GET', f'/api/projects/{client.project}', project_response=lambda x:
                             pick(x, ('id', 'name', 'status', 'default_wiki_space_id', 'shared_spaces')))
    space = client.request('GET', f'/api/wiki-spaces/{client.space}', project_response=lambda x:
                           pick(x, ('id', 'name', 'status', 'project_id', 'scope')))
    if (project['id'] != client.project or space['id'] != client.space
            or space.get('project_id') != client.project or space.get('scope') != 'project'
            or project['status'] != 'active' or space['status'] != 'active'
            or any(s.get('enabled') for s in project.get('shared_spaces', []))):
        raise ValidationError('isolated active project/space required')
    models = client.request('GET', '/api/models', project_response=lambda xs:
                            [pick(x, ('id', 'name', 'model_name', 'is_default', 'protocol')) for x in xs])
    def prompts(xs):
        return [dict(pick(x, ('id', 'type', 'version', 'name', 'is_active')),
                     content_sha256=hashlib.sha256(x['content'].encode()).hexdigest())
                for x in xs if x.get('is_active')]
    active = client.request('GET', '/api/prompts', project_response=prompts)
    docs = client.request('GET', '/api/documents', project_response=lambda xs:
                          [pick(x, ('id', 'filename', 'status', 'char_count', 'sha256', 'space_id'))
                           for x in xs if x.get('space_id') == client.space])
    pages = client.request('GET', '/api/wiki/pages', project_response=lambda xs:
                           [pick(x, ('id', 'title', 'status', 'source_document_id')) for x in xs])
    ext = client.request('GET', f'/api/projects/{client.project}/external-wiki',
                         project_response=lambda x: pick(x, ('id', 'enabled', 'external_project_id')) if x else None)
    if ext and ext.get('enabled') and ext.get('external_project_id', '').strip():
        raise ValidationError('external KB enabled; source isolation refused')
    defaults = [m for m in models if m.get('is_default')]
    generate = [p for p in active if p['type'] == 'generate']
    points = [p for p in active if p['type'] == 'test_points']
    if len(defaults) != 1 or len(generate) != 1 or len(points) != 1:
        raise ValidationError('ambiguous model or active prompt')
    if not docs or any(d['status'] != 'ready' for d in docs):
        raise ValidationError('all source documents must be ready')
    checklist = [d for d in docs if d['filename'] == CHECKLIST]
    if len(checklist) != 1:
        raise ValidationError('original user regression checklist missing or ambiguous; normal upload/ingest required')
    chunk_ids = []
    for doc in docs:
        chunks = client.request('GET', f"/api/documents/{doc['id']}/chunks")
        chunk_ids.extend(c['id'] for c in chunks)
        text = ''
        for index, c in enumerate(chunks):
            start, end, part = c['start_char'], c['end_char'], c['text']
            if (c['chunk_index'] != index or start < 0 or start > len(text)
                    or end - start != len(part) or end <= len(text)
                    or text[start:] != part[:len(text) - start]):
                raise ValidationError('source chunks incomplete or inconsistent')
            text += part[len(text) - start:]
        if not chunks or len(text) != doc['char_count']:
            raise ValidationError('source length incomplete')
        if doc['id'] == checklist[0]['id'] and not re.search(r'^##\s+' + re.escape(SECTION) + r'\s*$', text, re.M):
            raise ValidationError('requested regression section absent in KB original')
    return {'project': project, 'space': space, 'models': models, 'prompts': active,
            'documents': docs, 'pages': pages, 'chunk_ids': chunk_ids,
            'model_id': defaults[0]['id'], 'prompt_id': generate[0]['id']}


def task_payload(metadata, project, space):
    return {'title': '运行验收：单笔成交金额', 'description':
            '业务范围：单笔成交金额。请基于本项目已摄入的原始资料生成测试场景。\n'
            '回归清单：《竞价基本功能测试.md》\n回归章节：《2. 单笔成交金额》\n'
            '排除回归项：《DVP》\n排除回归项：《DST》\n'
            '保留所选原文回归项并补充有来源依据的业务分支；资料不足应记录缺口。仅生成场景与验证目标。',
            'project_id': project, 'wiki_space_id': space, 'model_id': metadata['model_id'],
            'prompt_template_id': metadata['prompt_id'], 'auto_review': False, 'run_generate': False,
            'generation_granularity': 'detailed', 'test_dimensions': ['positive', 'negative', 'boundary'],
            'reference_case_ids': [], 'reference_text': ''}


def freeze(client, task_id, *, retry=None):
    proposal = client.request('GET', f'/api/tasks/{task_id}/test-points')
    if proposal['task_id'] != task_id or proposal['status'] != 'pending':
        raise ValidationError('proposal identity/status changed')
    if retry and (proposal['id'] == retry['body']['checkpoint_id']
                  or proposal['attempt'] <= retry['attempt']
                  or proposal['retrieval_checkpoint_id'] != retry['retrieval_checkpoint_id']):
        raise ValidationError('new retry proposal not observed; old checkpoint retained')
    client.archive.save('proposal.json', proposal)
    client.archive.save('coverage-metadata.json', proposal.get('coverage_metadata', {}))
    client.archive.save('scenarios.json', {'task_id': task_id, 'scenarios': proposal['points']})
    output = io.StringIO(newline='')
    fields = sorted({key for point in proposal['points'] for key in point})
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for point in proposal['points']:
        row = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in point.items()}
        # Spreadsheet-safe rendering; JSON is the authoritative unmodified representation.
        writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for k, v in row.items()})
    csv_data = output.getvalue()
    if any(s and s in csv_data for s in client.archive.secrets):
        raise ValidationError('secret in CSV; refused')
    client.archive.bytes('scenarios.csv', csv_data.encode('utf-8-sig'))
    for endpoint in ('events', 'citations'):
        client.archive.save(endpoint + '.json', client.request('GET', f'/api/tasks/{task_id}/{endpoint}'))
    return proposal.get('coverage_metadata', {})


def retry_proposal(client, run_root, task_id, checkpoint_id, expected_version):
    """Called only by explicit execute+resume; marker existence always means GET-only.

    The CLI holds the run's exclusive lock. O_EXCL additionally prevents a second
    process from sending even if callers omit that lock. An uncertain request is
    never replayed: the persisted key remains available for server-side audit.
    """
    marker_name = f'retry-{checkpoint_id}-v{expected_version}.started.json'
    marker = run_root / marker_name
    if marker.exists():
        saved = json.loads(marker.read_text())
        if (saved['task_id'] != task_id or saved['project_id'] != client.project
                or saved['space_id'] != client.space
                or saved['body']['checkpoint_id'] != checkpoint_id
                or saved['body']['expected_version'] != expected_version):
            raise ValidationError('retry marker identity mismatch')
        return saved
    task = client.request('GET', f'/api/tasks/{task_id}')
    if (task['id'] != task_id or task['project_id'] != client.project
            or task['wiki_space_id'] != client.space
            or task['status'] != 'awaiting_test_point_confirmation'):
        raise ValidationError('retry task identity/scope/status mismatch')
    proposal = client.request('GET', f'/api/tasks/{task_id}/test-points')
    if (proposal['task_id'] != task_id or proposal['id'] != checkpoint_id
            or proposal['version'] != expected_version or proposal['status'] != 'pending'
            or not isinstance(proposal.get('retrieval_checkpoint_id'), int)
            or proposal['retrieval_checkpoint_id'] <= 0
            or not isinstance(proposal.get('attempt'), int) or proposal['attempt'] <= 0):
        raise ValidationError('retry checkpoint identity/version/status mismatch')
    body = {'checkpoint_id': checkpoint_id, 'expected_version': expected_version,
            'idempotency_key': 'runtime-retry-' + uuid.uuid4().hex}
    saved = {'task_id': task_id, 'project_id': client.project, 'space_id': client.space,
             'retrieval_checkpoint_id': proposal['retrieval_checkpoint_id'],
             'attempt': proposal['attempt'], 'body': body}
    owner = Archive(run_root)
    owner.secrets = client.archive.secrets
    owner.save(marker_name, saved)
    directory_fd = os.open(run_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    client.retry_binding = saved
    try:
        client.request('POST', f'/api/tasks/{task_id}/test-points/retry', body)
    except ValidationError:
        # Durable marker prohibits resend even when response acceptance is unknown.
        client.archive.save('retry-uncertain.json', {'observe_only': True, 'marker': marker_name})
    finally:
        client.retry_binding = None
    return saved


def observe(client, run_root, metadata, *, resume=False, deadline=1200, interval=3,
            retry_checkpoint=None, expected_version=None):
    if (retry_checkpoint is None) != (expected_version is None) or (
            retry_checkpoint is not None and (not resume or retry_checkpoint <= 0 or expected_version <= 0)):
        raise ValidationError('retry requires resume and positive checkpoint/version')
    retry = None
    identity = run_root / 'task.json'
    if resume:
        client.observe_only = True
        if not identity.is_file():
            raise ValidationError('resume requires recorded task ID; uncertain create will not be repeated')
        binding = json.loads(identity.read_text())
        if binding['project_id'] != client.project or binding['space_id'] != client.space:
            raise ValidationError('resume project/space mismatch')
        task_id = binding['task_id']
        if not isinstance(task_id, int) or task_id <= 0:
            raise ValidationError('invalid recorded task identity')
        if retry_checkpoint is not None:
            retry = retry_proposal(client, run_root, task_id, retry_checkpoint, expected_version)
    else:
        if identity.exists() or (run_root / 'create.started.json').exists():
            raise ValidationError('creation already attempted; never create twice')
        payload = task_payload(metadata, client.project, client.space)
        owner = Archive(run_root)
        owner.secrets = client.archive.secrets
        owner.save('create.started.json', payload)
        task = client.request('POST', '/api/tasks', payload)
        task_id = task['id']
        if task['project_id'] != client.project or task['wiki_space_id'] != client.space:
            raise ValidationError('created task scope mismatch')
        owner.save('task.json', {'task_id': task_id, 'project_id': client.project, 'space_id': client.space})
        client.request('POST', f'/api/tasks/{task_id}/generate')
    until = time.monotonic() + deadline
    states, confirmed = [], False
    try:
        while time.monotonic() < until:
            task = client.request('GET', f'/api/tasks/{task_id}')
            if (task['id'] != task_id or task['project_id'] != client.project
                    or task['wiki_space_id'] != client.space):
                raise ValidationError('observed task scope mismatch')
            status = task['status']
            states.append({'status': status, 'at': datetime.now(timezone.utc).isoformat()})
            if status == 'awaiting_test_point_confirmation':
                return freeze(client, task_id, retry=retry)
            if status == 'awaiting_confirmation':
                if resume:
                    raise ValidationError('observe-only resume reached retrieval gate; no mutation permitted')
                if confirmed:
                    raise ValidationError('retrieval confirmation already attempted; no replay')
                cp = client.request('GET', f'/api/tasks/{task_id}/retrieval-checkpoint')
                if cp['task_id'] != task_id or cp['status'] != 'pending' or cp.get('conflict_groups'):
                    raise ValidationError('retrieval conflict or checkpoint mismatch')
                page_ids = {p['id'] for p in metadata['pages'] if p['status'] == 'published'
                            and p.get('source_document_id') in {d['id'] for d in metadata['documents']}}
                selected = [c['id'] for c in cp['candidate_citations']
                            if c.get('available', True) and not c.get('legacy')
                            and ((c['citation_type'] == 'source' and c.get('source_chunk_id') in metadata['chunk_ids'])
                                 or (c['citation_type'] == 'wiki' and c.get('wiki_page_id') in page_ids))]
                if not selected:
                    raise ValidationError('no eligible isolated KB citations')
                body = {'checkpoint_id': cp['id'], 'expected_version': cp['version'],
                        'selected_citation_ids': selected, 'supplemental_text': '',
                        'idempotency_key': f"runtime-{task_id}-{cp['id']}-{cp['version']}"}
                client.archive.save('retrieval-selection.json', body)
                confirmed = True
                client.request('POST', f'/api/tasks/{task_id}/retrieval-checkpoint/confirm', body)
            elif status not in ('retrieving', 'generating_test_points'):
                # Preserve the real server response in call records; never turn errors into scenarios.
                raise ValidationError('task stopped/failed/unexpected state; visible gap retained')
            time.sleep(min(interval, max(0, until - time.monotonic())))
        raise ValidationError('poll deadline exceeded; resume may only observe recorded task ID')
    finally:
        client.archive.save('state-flow.json', {'task_id': task_id, 'states': states})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--project-id', type=int, required=True)
    parser.add_argument('--space-id', type=int, required=True)
    parser.add_argument('--resume', type=Path, help='Existing run directory; observe only unless explicit retry options are paired')
    parser.add_argument('--retry-checkpoint', type=int, help='Explicitly retry this current pending checkpoint once')
    parser.add_argument('--expected-version', type=int, help='Expected current pending checkpoint version')
    parser.add_argument('--username-env', default='CASEGEN_USERNAME')
    parser.add_argument('--password-env', default='CASEGEN_PASSWORD')
    parser.add_argument('--credentials-stdin', action='store_true', help='Two lines: username, password; never echoed')
    parser.add_argument('--deadline', type=float, default=1200)
    parser.add_argument('--poll-interval', type=float, default=3)
    args = parser.parse_args(argv)
    if (args.retry_checkpoint is None) != (args.expected_version is None) or (
            args.retry_checkpoint is not None and (not args.execute or not args.resume
                or args.retry_checkpoint <= 0 or args.expected_version <= 0)):
        parser.error('retry requires --execute --resume --retry-checkpoint ID --expected-version V')
    if (args.project_id <= 0 or args.space_id <= 0
            or not all(math.isfinite(v) and v > 0 for v in (args.deadline, args.poll_interval))
            or args.deadline > 7200 or not 1 <= args.poll_interval <= 60):
        parser.error('invalid IDs or bounded timing')
    availability = {'username_available': bool(os.environ.get(args.username_env)),
                    'password_available': bool(os.environ.get(args.password_env))}
    if not args.execute:
        print(json.dumps({'executed': False, 'http_requests': 0, 'credentials': availability,
                          'section': SECTION}, ensure_ascii=False))
        return 0
    validation = (ROOT / 'data/validation').resolve()
    validation.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.resume:
        run_root = args.resume.absolute()
        if (run_root.is_symlink() or any(p.is_symlink() for p in run_root.parents)
                or run_root.resolve().parent != validation or not run_root.name.startswith('coverage-flow-')
                or not (run_root / 'task.json').is_file()):
            raise ValidationError('resume must use recorded validation run under data/validation')
    else:
        run_root = validation / ('coverage-flow-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8])
        run_root.mkdir(mode=0o700)
    lock_fd = os.open(run_root / '.lock', os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValidationError('another observer owns this run') from None
        attempt = run_root / ('attempt-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        archive = Archive(attempt)
        client = Client(archive, args.project_id, args.space_id)
        client.observe_only = bool(args.resume)
        outcome = {'completed': False, 'visible_gaps': [], 'model_calls': 'unknown until service coverage metadata observed'}
        try:
            if client.request('GET', '/api/health') != {'status': 'ok'}:
                raise ValidationError('unexpected health response')
            if args.credentials_stdin:
                username, password = sys.stdin.readline().rstrip('\r\n'), sys.stdin.readline().rstrip('\r\n')
            else:
                username, password = os.environ.get(args.username_env), os.environ.get(args.password_env)
            client.login(username, password)
            metadata = None if args.resume else source_metadata(client)
            if metadata:
                archive.save('source-metadata.json', metadata)
            coverage = observe(client, run_root, metadata, resume=bool(args.resume),
                               deadline=args.deadline, interval=args.poll_interval,
                               retry_checkpoint=args.retry_checkpoint, expected_version=args.expected_version)
            outcome.update(completed=True, stopped_at='awaiting_test_point_confirmation', coverage_metadata=coverage,
                           model_calls=(coverage.get('budget') or {}).get('calls_used', 'unknown'),
                           scenario_quality_judged=False)
            outcome['visible_gaps'].extend(coverage.get('gaps') or [])
            outcome['visible_gaps'].extend(coverage.get('warnings') or [])
        except ValidationError as exc:
            outcome['visible_gaps'].append(str(exc))
        except (ValueError, KeyError, TypeError, OSError):
            outcome['visible_gaps'].append('invalid API schema or local IO error; inspect safe call records')
        finally:
            try:
                client.logout()
            except ValidationError:
                outcome['visible_gaps'].append('own session logout failed; in-memory cookies discarded')
            archive.save('outcome.json', dict(outcome, http_calls=archive.calls))
        print(json.dumps({'run': str(run_root), 'completed': outcome['completed'], 'http_calls': archive.calls}, ensure_ascii=False))
        return 0 if outcome['completed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValidationError, KeyboardInterrupt):
        print('validation stopped safely; inspect outcome/call status; no automatic retry', file=sys.stderr)
        raise SystemExit(1)
