#!/usr/bin/env python3
"""Rules-only isolated preparation: upload ONLY the trading-rules DOCX.

Derived from prepare_sources.py but restricted to a single non-checklist source.
The user's regression checklist (竞价基本功能测试.md) is explicitly refused: this
run exists to evaluate generation WITHOUT the answer key.

The upstream observe() treats document index 0 as the checklist and enforces a
section marker; that assertion is specific to the two-document coverage flow and
does not apply here, so this module performs the same chunk/preview verification
with checklist=False.
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
import sys
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import credential_runner as credentials
import prepare_sources as base
from validate_coverage_flow import ValidationError

ROOT = Path(__file__).resolve().parents[2]
RULES = Path('/home/xinyu/下载/1_上海证券交易所交易规则（2026年修订）.docx')
RULES_SHA = 'fc922c433438b2636cb631eab25cca405209712acbb6aaded768c45456ff8888'
RULES_CHARS = 19475
FORBIDDEN = ('竞价基本功能测试.md',)


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def source_info(path: Path) -> dict:
    require(path.is_file() and not path.is_symlink(), 'rules-only source missing or symlink')
    require(path.name not in FORBIDDEN, 'regression checklist is forbidden in rules-only preparation')
    data = path.read_bytes()
    require(0 < len(data) <= base.MAX_UPLOAD, 'rules-only source empty or too large')
    digest = hashlib.sha256(data).hexdigest()
    require(digest == RULES_SHA, 'rules-only source hash differs from the approved document')
    return {'filename': path.name, 'path': str(path), 'size': len(data), 'sha256': digest}


def prepare_rules_only(client, path, info, plan, *, deadline, interval):
    """Same write ordering/markers as base.prepare, without the checklist assertion."""
    project = client.write_once('project', '/api/projects', plan, base.PROJECT_FIELDS)
    base.bind_project(client, project, plan)
    base.check_isolation(client, plan)
    existing = client.request('GET', '/api/documents',
                              project_response=lambda xs: [base.pick(x, base.DOC_FIELDS) for x in xs])
    require({d['id'] for d in existing} == client.documents,
            'unexpected document in isolated space; refused')
    doc = client.upload(0, path, info)
    base.check_document(client, doc, info)
    require(doc['id'] not in client.documents, 'duplicate new document ID')
    client.documents.add(doc['id'])
    job = client.write_once('ingest-0', f"/api/documents/{doc['id']}/ingest", None, base.JOB_FIELDS)
    base.check_job(client, job, doc['id'])
    client.jobs.add(job['id'])
    summary = observe_rules_only(client, doc, job, info, deadline=deadline, interval=interval)
    base.check_isolation(client, plan)
    require(doc['char_count'] == RULES_CHARS, 'rules-only character count changed')
    return {'completed': True, 'project_id': client.project, 'space_id': client.space,
            'sources': [summary], 'section': None, 'checklist_present': False,
            'answer_key_excluded': True,
            'scenario_quality_judged': False,
            'model_effectiveness_validated': False,
            'model_calls': 'unknown; normal ingest may invoke the configured model'}


def observe_rules_only(client, doc, recorded_job, info, *, deadline, interval):
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        job = client.request('GET', f"/api/ingest-jobs/{recorded_job['id']}",
                             project_response=lambda x: base.pick(x, base.JOB_FIELDS))
        base.check_job(client, job, doc['id'])
        require(job['id'] == recorded_job['id'], 'observed job ID changed')
        if job['status'] in {'queued', 'running'} and not job.get('cancel_requested'):
            time.sleep(min(interval, max(0, until - time.monotonic())))
            continue
        require(job['status'] in {'success', 'success_with_warnings'} and not job.get('cancel_requested'),
                'ingest failed/cancelled/unexpected state; no retry')
        current = client.request('GET', f"/api/documents/{doc['id']}",
                                 project_response=lambda x: base.pick(x, base.DOC_FIELDS))
        base.check_document(client, current, info)
        require(current['id'] == doc['id'], 'observed document ID changed')
        require(current['status'] == 'ready', 'successful job without ready document')
        # checklist=False: this document has no regression section marker.
        summary = base.verify_chunks(client, current, info, False)
        return dict(summary, job_id=job['id'], job_status=job['status'])
    raise ValidationError('bounded ingest observation expired; resume observes recorded IDs only')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--config', type=Path,
                        default=ROOT / 'data/private/casegen-validation/credentials.json')
    parser.add_argument('--deadline', type=float, default=1800)
    parser.add_argument('--poll-interval', type=float, default=3)
    args = parser.parse_args(argv)
    if not (math.isfinite(args.deadline) and 0 < args.deadline <= 7200
            and math.isfinite(args.poll_interval) and 1 <= args.poll_interval <= 60):
        parser.error('invalid bounded timing')
    info = source_info(RULES)
    if not args.execute:
        print(json.dumps({'executed': False, 'http_requests': 0, 'credential_reads': 0,
                          'sources': [info['filename']], 'sha256': info['sha256'],
                          'forbidden_sources': list(FORBIDDEN),
                          'checklist_assertion': 'disabled (rules-only)'}, ensure_ascii=False))
        return 0

    config = credentials.load_config(args.config)
    validation = (ROOT / 'data/validation').resolve()
    require(validation.is_dir(), 'existing data/validation directory required')
    run = validation / ('rules-only-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-')
                        + uuid.uuid4().hex[:8])
    run.mkdir(mode=0o700)
    lock_fd = os.open(run / '.lock', os.O_CREAT | os.O_WRONLY, 0o600)
    outcome = {'completed': False, 'sources': [info], 'visible_gaps': []}
    with os.fdopen(lock_fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValidationError('another rules-only preparation owns this run') from None
        owner = base.DurableArchive(run)
        owner.secrets.extend([config['username'], config['password']])
        unique = uuid.uuid4().hex
        plan = {'name': 'RulesOnly runtime ' + unique,
                'slug': 'rules-only-runtime-' + unique,
                'description': 'Isolated rules-only source for answer-free coverage evaluation'}
        owner.save('plan.json', plan)
        owner.save('sources.json', [info])
        attempt = run / ('attempt-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        archive = base.DurableArchive(attempt)
        archive.secrets = owner.secrets
        client = base.PreparationClient(archive, owner)
        try:
            require(client.request('GET', '/api/health') == {'status': 'ok'}, 'unexpected health response')
            client.login(config['username'], config['password'])
            client.request('GET', '/api/models', project_response=lambda xs:
                           [base.pick(x, ('id', 'name', 'model_name', 'is_default', 'protocol')) for x in xs])
            manifest = prepare_rules_only(client, RULES, info, plan,
                                          deadline=args.deadline, interval=args.poll_interval)
            archive.save('manifest.json', manifest)
            outcome.update(completed=True, project_id=client.project, space_id=client.space,
                           document_id=manifest['sources'][0]['document_id'], manifest=manifest)
        except ValidationError as exc:
            outcome['visible_gaps'].append(str(exc))
        except (ValueError, KeyError, TypeError, OSError):
            outcome['visible_gaps'].append('invalid API schema or local IO; inspect safe call records')
        finally:
            try:
                client.logout()
            except ValidationError:
                outcome['visible_gaps'].append('own session logout failed; in-memory cookies discarded')
            archive.save('outcome.json', outcome)
    print(json.dumps({k: v for k, v in outcome.items() if k != 'manifest'}, ensure_ascii=False))
    return 0 if outcome['completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
