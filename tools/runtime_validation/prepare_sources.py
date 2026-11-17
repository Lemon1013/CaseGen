#!/usr/bin/env python3
"""Prepare two original sources in a new isolated project; explicit opt-in only.

Resume is strictly observational. Interrupted/failed writes are never replayed.
"""
from __future__ import annotations

import argparse
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
from urllib.parse import urlencode
import uuid

from validate_coverage_flow import Archive, Client, ValidationError, pick, SECTION

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = (
    Path('/home/xinyu/文档/竞价基本功能测试.md'),
    Path('/home/xinyu/下载/1_上海证券交易所交易规则（2026年修订）.docx'),
)
MAX_UPLOAD = 20 * 1024 * 1024
DOC_FIELDS = ('id', 'filename', 'sha256', 'space_id', 'status', 'char_count')
JOB_FIELDS = ('id', 'document_id', 'space_id', 'status', 'stage', 'progress', 'cancel_requested')
PROJECT_FIELDS = ('id', 'name', 'slug', 'status', 'default_wiki_space_id', 'private_spaces', 'shared_spaces')


class DurableArchive(Archive):
    def __init__(self, path):
        super().__init__(path)
        self.sync_directory(self.path.parent)

    @staticmethod
    def sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def bytes(self, name, data):
        super().bytes(name, data)
        # fsync the directory entry as well as file data before any HTTP write.
        self.sync_directory((self.path / name).parent)


def read_json(path):
    if path.is_symlink() or not path.is_file():
        raise ValidationError('required preparation record missing; manually reconcile, never replay')
    return json.loads(path.read_text(encoding='utf-8'))


def source_info(path):
    if path.name not in {p.name for p in SOURCE_PATHS} or not path.is_file():
        raise ValidationError('required original source file missing or unexpected filename')
    size = path.stat().st_size
    if not 0 < size <= MAX_UPLOAD:
        raise ValidationError('source file empty or exceeds upload bound')
    content = path.read_bytes()
    if len(content) != size:
        raise ValidationError('source changed during inspection')
    return {'filename': path.name, 'size': size, 'sha256': hashlib.sha256(content).hexdigest()}


class PreparationClient(Client):
    """Own write boundary; scenario Client remains scenario-only by default."""
    def __init__(self, archive, owner, **kwargs):
        super().__init__(archive, None, None, **kwargs)
        self.owner = owner
        self.documents = set()
        self.jobs = set()
        self.observe_only = False
        self._multipart = None
        self._permit = None

    def endpoint(self, path):
        if path.startswith('/api/auth/') or path in {'/api/health', '/api/projects', '/api/models'}:
            return path
        if not self.project or not self.space:
            raise ValidationError('project/space not bound to preparation response')
        return path + ('&' if '?' in path else '?') + urlencode({
            'project_id': self.project, 'space_id': self.space})

    def check_write(self, method, path):
        if method == 'GET':
            allowed = {'/api/health', '/api/models'}
            if self.project and self.space:
                allowed |= {f'/api/projects/{self.project}', f'/api/wiki-spaces/{self.space}',
                            f'/api/projects/{self.project}/external-wiki', '/api/documents'}
                allowed |= {f'/api/documents/{i}{suffix}' for i in self.documents
                            for suffix in ('', '/chunks', '/preview?max_chars=200000')}
                allowed |= {f'/api/ingest-jobs/{i}' for i in self.jobs}
            if path not in allowed:
                raise ValidationError('read endpoint outside recorded preparation scope')
            return
        if method == 'POST' and path in {'/api/auth/login', '/api/auth/logout'}:
            return
        if self.observe_only or method != 'POST' or self._permit != path:
            raise ValidationError('write forbidden by preparation boundary or observe-only resume')
        if path == '/api/projects' and self.project is None:
            return
        if path == '/api/documents' and self.project and self.space and self._multipart:
            return
        if path in {f'/api/documents/{i}/ingest' for i in self.documents}:
            return
        raise ValidationError('write endpoint outside newly recorded document scope')

    def wire_body(self, path, data):
        if path == '/api/documents' and self._multipart:
            return self._multipart
        return super().wire_body(path, data)

    def write_once(self, label, path, data, fields):
        if self.observe_only:
            raise ValidationError('resume observes only; no write replay')
        marker = self.owner.path / (label + '.started.json')
        if marker.exists() or (self.owner.path / (label + '.mapped.json')).exists():
            raise ValidationError('write already attempted; manually reconcile, never replay')
        self.owner.save(label + '.started.json', {'endpoint': path, 'request': data})
        self._permit = path
        try:
            result = self.request('POST', path, data, project_response=lambda x: pick(x, fields))
            # Preserve returned identity even if subsequent scope/hash/status checks fail.
            if type(result.get('id')) is not int or result['id'] <= 0:
                raise ValidationError('write returned no valid ID; manually reconcile, never replay')
            self.owner.save(label + '.mapped.json', result)
            return result
        finally:
            self._permit = None

    def upload(self, index, path, info):
        content = path.read_bytes()
        if len(content) != info['size'] or hashlib.sha256(content).hexdigest() != info['sha256']:
            raise ValidationError('source changed before upload; refused')
        boundary = 'casegen-runtime-' + uuid.uuid4().hex
        mime = 'text/markdown' if path.suffix == '.md' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="space_id"\r\n\r\n'
                  f'{self.space}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                  f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        self._multipart = (prefix + content + f'\r\n--{boundary}--\r\n'.encode(),
                           'multipart/form-data; boundary=' + boundary)
        try:
            return self.write_once(f'upload-{index}', '/api/documents', info, DOC_FIELDS)
        finally:
            self._multipart = None


def positive_id(value):
    return type(value) is int and value > 0


def bind_project(client, project, expected):
    if not positive_id(project.get('id')) or not positive_id(project.get('default_wiki_space_id')):
        raise ValidationError('new project response lacks project/private space ID')
    client.project, client.space = project['id'], project['default_wiki_space_id']
    if (project.get('slug') != expected['slug'] or project.get('name') != expected['name']
            or project.get('status') != 'active' or project.get('shared_spaces')
            or len(project.get('private_spaces', [])) != 1
            or project['private_spaces'][0]['id'] != client.space
            or project['private_spaces'][0].get('scope') != 'project'):
        raise ValidationError('created project identity/isolation mismatch')


def check_isolation(client, expected):
    project = client.request('GET', f'/api/projects/{client.project}',
                             project_response=lambda x: pick(x, PROJECT_FIELDS))
    original = (client.project, client.space)
    bind_project(client, project, expected)
    if original != (client.project, client.space):
        raise ValidationError('recorded project/private space changed')
    space = client.request('GET', f'/api/wiki-spaces/{client.space}',
                           project_response=lambda x: pick(x, ('id', 'project_id', 'scope', 'status')))
    if space != {'id': client.space, 'project_id': client.project, 'scope': 'project', 'status': 'active'}:
        raise ValidationError('active private project space required')
    external = client.request('GET', f'/api/projects/{client.project}/external-wiki',
                              project_response=lambda x: pick(x, ('enabled', 'external_project_id')) if x else None)
    if external and external.get('enabled') and (external.get('external_project_id') or '').strip():
        raise ValidationError('effective external KB binding refused')


def check_document(client, doc, info):
    if (not positive_id(doc.get('id')) or doc.get('space_id') != client.space
            or doc.get('filename') != info['filename'] or doc.get('sha256') != info['sha256']):
        raise ValidationError('uploaded document identity/source hash mismatch')
    if doc.get('status') not in {'parsed', 'ingesting', 'ready'}:
        raise ValidationError('document failed or unexpected state; no retry')


def check_job(client, job, document_id):
    if (not positive_id(job.get('id')) or job.get('space_id') != client.space
            or job.get('document_id') != document_id):
        raise ValidationError('ingest job identity/scope mismatch')


def resume_records(client, sources, plan):
    # Refuse any uncertain write before making any HTTP request, including login.
    for marker in client.owner.path.glob('*.started.json'):
        if not marker.with_name(marker.name.replace('.started.json', '.mapped.json')).is_file():
            raise ValidationError('uncertain write recorded; manually reconcile IDs, never replay')
    project = read_json(client.owner.path / 'project.mapped.json')
    bind_project(client, project, plan)
    docs, jobs, document_ids, job_ids = [], [], set(), set()
    for index, info in enumerate(sources):
        doc = read_json(client.owner.path / f'upload-{index}.mapped.json')
        check_document(client, doc, info)
        if doc['id'] in document_ids:
            raise ValidationError('duplicate uploaded document ID')
        document_ids.add(doc['id'])
        job = read_json(client.owner.path / f'ingest-{index}.mapped.json')
        check_job(client, job, doc['id'])
        if job['id'] in job_ids:
            raise ValidationError('duplicate ingest job ID')
        job_ids.add(job['id'])
        docs.append(doc)
        jobs.append(job)
    client.documents, client.jobs = document_ids, job_ids
    client.observe_only = True
    return docs, jobs


def verify_chunks(client, doc, info, checklist):
    def summarize(chunks):
        text, ids = '', []
        for index, chunk in enumerate(chunks):
            start, end, part = chunk['start_char'], chunk['end_char'], chunk['text']
            if (chunk['document_id'] != doc['id'] or chunk['space_id'] != client.space
                    or chunk['chunk_index'] != index or start < 0 or start > len(text)
                    or end - start != len(part) or end <= len(text)
                    or text[start:] != part[:len(text) - start] or not positive_id(chunk['id'])
                    or chunk['id'] in ids):
                raise ValidationError('source chunk gap or inconsistent offsets/identity')
            text += part[len(text) - start:]
            ids.append(chunk['id'])
        if not chunks or len(text) != doc['char_count']:
            raise ValidationError('source chunk reconstruction incomplete')
        if checklist and not re.search(r'^##\s+' + re.escape(SECTION) + r'\s*$', text, re.M):
            raise ValidationError('requested checklist section absent')
        return {'chunk_ids': ids, 'chunk_count': len(ids), 'char_count': len(text),
                'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'section_verified': checklist}
    summary = client.request('GET', f"/api/documents/{doc['id']}/chunks", project_response=summarize)
    # Compare reconstruction with the server's fresh parse of this immutable upload.
    # Never archive the preview body or multipart source contents.
    preview = client.request('GET', f"/api/documents/{doc['id']}/preview?max_chars=200000",
                             project_response=lambda x: {
                                 'document_id': x['document_id'], 'char_count': x['char_count'],
                                 'truncated': x['truncated'], 'quality_ok': x['quality_ok'],
                                 'text_sha256': hashlib.sha256(x['text'].encode()).hexdigest()})
    if (preview['document_id'] != doc['id'] or preview['truncated'] or not preview['quality_ok']
            or preview['char_count'] != summary['char_count'] or preview['text_sha256'] != summary['text_sha256']):
        raise ValidationError('chunks differ from complete original parse or preview bound exceeded')
    return dict(info, document_id=doc['id'], space_id=client.space, status='ready', **summary)


def observe(client, docs, jobs, sources, *, deadline, interval):
    until = time.monotonic() + deadline
    summaries = {}
    while time.monotonic() < until:
        for index, (doc, recorded_job, info) in enumerate(zip(docs, jobs, sources)):
            if index in summaries:
                continue
            job = client.request('GET', f"/api/ingest-jobs/{recorded_job['id']}",
                                 project_response=lambda x: pick(x, JOB_FIELDS))
            check_job(client, job, doc['id'])
            if job['id'] != recorded_job['id']:
                raise ValidationError('observed job ID changed')
            if job['status'] in {'queued', 'running'} and not job.get('cancel_requested'):
                continue
            if job['status'] not in {'success', 'success_with_warnings'} or job.get('cancel_requested'):
                raise ValidationError('ingest failed/cancelled/unexpected state; no retry')
            current = client.request('GET', f"/api/documents/{doc['id']}",
                                     project_response=lambda x: pick(x, DOC_FIELDS))
            check_document(client, current, info)
            if current['id'] != doc['id']:
                raise ValidationError('observed document ID changed')
            if current['status'] != 'ready':
                raise ValidationError('successful job without ready document')
            summaries[index] = dict(verify_chunks(client, current, info, index == 0),
                                    job_id=job['id'], job_status=job['status'])
        if len(summaries) == len(sources):
            return [summaries[i] for i in range(len(sources))]
        time.sleep(min(interval, max(0, until - time.monotonic())))
    raise ValidationError('bounded ingest observation expired; resume observes recorded IDs only')


def prepare(client, paths, sources, plan, *, resume=False, deadline=1200, interval=3):
    if resume:
        docs, jobs = resume_records(client, sources, plan)
    else:
        project = client.write_once('project', '/api/projects', plan, PROJECT_FIELDS)
        bind_project(client, project, plan)
        docs, jobs = [], []
    check_isolation(client, plan)
    existing = client.request('GET', '/api/documents', project_response=lambda xs: [pick(x, DOC_FIELDS) for x in xs])
    if {d['id'] for d in existing} != client.documents:
        raise ValidationError('unexpected document in isolated space; refused')
    if not resume:
        for index, (path, info) in enumerate(zip(paths, sources)):
            doc = client.upload(index, path, info)
            check_document(client, doc, info)
            if doc['id'] in client.documents:
                raise ValidationError('duplicate new document ID')
            client.documents.add(doc['id'])
            docs.append(doc)
        for index, doc in enumerate(docs):
            job = client.write_once(f'ingest-{index}', f"/api/documents/{doc['id']}/ingest", None, JOB_FIELDS)
            check_job(client, job, doc['id'])
            client.jobs.add(job['id'])
            jobs.append(job)
    summaries = observe(client, docs, jobs, sources, deadline=deadline, interval=interval)
    check_isolation(client, plan)
    return {'completed': True, 'project_id': client.project, 'space_id': client.space,
            'sources': summaries, 'section': SECTION, 'scenario_quality_judged': False,
            'model_effectiveness_validated': False,
            'model_calls': 'unknown; normal ingest may invoke the configured model',
            'validate_command': f'backend/.venv/bin/python tools/runtime_validation/validate_coverage_flow.py --project-id {client.project} --space-id {client.space}'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--resume', type=Path, help='Existing preparation run; observation only')
    parser.add_argument('--username-env', default='CASEGEN_USERNAME')
    parser.add_argument('--password-env', default='CASEGEN_PASSWORD')
    parser.add_argument('--credentials-stdin', action='store_true', help='Safe pipe with two lines; never echoed')
    parser.add_argument('--deadline', type=float, default=1200)
    parser.add_argument('--poll-interval', type=float, default=3)
    args = parser.parse_args(argv)
    if (not all(math.isfinite(x) and x > 0 for x in (args.deadline, args.poll_interval))
            or args.deadline > 7200 or not 1 <= args.poll_interval <= 60):
        parser.error('invalid bounded timing')
    if not args.execute:
        print(json.dumps({'executed': False, 'http_requests': 0, 'sources': [p.name for p in SOURCE_PATHS],
                          'credentials': {'username_available': bool(os.environ.get(args.username_env)),
                                          'password_available': bool(os.environ.get(args.password_env))}}, ensure_ascii=False))
        return 0
    if args.credentials_stdin:
        username, password = sys.stdin.readline().rstrip('\r\n'), sys.stdin.readline().rstrip('\r\n')
    else:
        username, password = os.environ.get(args.username_env), os.environ.get(args.password_env)
    if not username or not password:
        raise ValidationError('normal login credentials missing; no project created, zero HTTP')
    paths = SOURCE_PATHS
    sources = [source_info(path) for path in paths]
    validation = (ROOT / 'data/validation').resolve()
    validation.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.resume:
        run = args.resume.absolute()
        if (run.is_symlink() or any(p.is_symlink() for p in run.parents)
                or run.resolve().parent != validation or not run.name.startswith('prepare-sources-')):
            raise ValidationError('resume must use an existing preparation run under data/validation')
    else:
        run = validation / ('prepare-sources-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8])
        run.mkdir(mode=0o700)
    lock_fd = os.open(run / '.lock', os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValidationError('another preparation observer owns this run') from None
        owner = DurableArchive(run)
        owner.secrets.extend([username, password])
        if args.resume:
            original = read_json(run / 'sources.json')
            if sources != original:
                raise ValidationError('original source hashes changed; resume refused')
            plan = read_json(run / 'plan.json')
        else:
            unique = uuid.uuid4().hex
            plan = {'name': 'Coverage runtime ' + unique, 'slug': 'coverage-runtime-' + unique,
                    'description': 'Isolated original sources for coverage runtime validation'}
            owner.save('sources.json', sources)
            owner.save('plan.json', plan)
        attempt = run / ('attempt-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        archive = DurableArchive(attempt)
        archive.secrets = owner.secrets
        client = PreparationClient(archive, owner)
        client.observe_only = bool(args.resume)
        outcome = {'completed': False, 'visible_gaps': []}
        try:
            if args.resume:
                resume_records(client, sources, plan)
            if client.request('GET', '/api/health') != {'status': 'ok'}:
                raise ValidationError('unexpected health response')
            client.login(username, password)
            # Models are read only and projected before any persistence.
            client.request('GET', '/api/models', project_response=lambda xs:
                           [pick(x, ('id', 'name', 'model_name', 'is_default', 'protocol')) for x in xs])
            manifest = prepare(client, paths, sources, plan, resume=bool(args.resume),
                               deadline=args.deadline, interval=args.poll_interval)
            archive.save('manifest.json', manifest)
            outcome.update(completed=True, project_id=client.project, space_id=client.space,
                           sources=[pick(x, ('filename', 'sha256', 'document_id', 'job_id')) for x in manifest['sources']])
        except ValidationError as exc:
            outcome['visible_gaps'].append(str(exc))
        except (ValueError, KeyError, TypeError, OSError):
            outcome['visible_gaps'].append('invalid API schema or local IO; manually reconcile recorded writes')
        finally:
            try:
                client.logout()
            except ValidationError:
                outcome['visible_gaps'].append('own session logout failed; in-memory cookies discarded')
            archive.save('outcome.json', dict(outcome, http_calls=archive.calls))
        print(json.dumps(dict(outcome, run=str(run), http_calls=archive.calls), ensure_ascii=False))
        return 0 if outcome['completed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValidationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print('preparation interrupted; inspect recorded IDs/status; no automatic retry', file=sys.stderr)
        raise SystemExit(1)
