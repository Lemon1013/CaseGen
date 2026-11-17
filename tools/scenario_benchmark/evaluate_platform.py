#!/usr/bin/env python3
"""Evaluate existing platform test points against all gold; never generate platform tasks.

prepare is readonly SQLite + dry-run. execute uses only the archived inputs.
No calibration records/expected labels are read by this module.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import copy
import csv
import io
import sqlite3
from contextlib import closing
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import evaluate_scenarios as ev
from audit_run import inspect_run
from fetch_scenarios import map_scenarios
from safe_http import SafeFailure, Transport, get_provider, provider_public

VERSION = 'platform-snapshot-evaluation-1.0'
CATEGORIES = ['AUC', 'AMT', 'ACC', 'OPN', 'CLS', 'CAN']
TASK_COLUMNS = ('id', 'requirement_id', 'project_id', 'status', 'generation_granularity',
                'created_at', 'updated_at')
REQUIREMENT_COLUMNS = ('id', 'project_id', 'title', 'description', 'focus_tags_json',
                       'source_filename', 'created_at', 'updated_at')
CHECKPOINT_COLUMNS = ('id', 'task_id', 'retrieval_checkpoint_id', 'attempt', 'version',
                      'status', 'created_at', 'updated_at')
POINT_COLUMNS = ('id', 'task_id', 'checkpoint_id', 'stable_key', 'title', 'verification_goal',
                 'dimension', 'priority', 'sort_order', 'is_selected', 'is_excluded',
                 'created_at', 'updated_at')
EVENT_DETAILS = ('checkpoint_id', 'count', 'selected_count', 'conflict_count',
                 'requires_knowledge_decision')


def now():
    return datetime.now(timezone.utc).isoformat()


def snapshot_sqlite(database, task_id):
    """One read transaction; constant allowlists, no SELECT *, no secret-bearing tables."""
    started = now()
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        def one(table, columns, where, values, order=''):
            sql = 'SELECT ' + ','.join(columns) + ' FROM ' + table + ' WHERE ' + where + order
            row = connection.execute(sql, values).fetchone()
            if row is None:
                raise SafeFailure('existing_platform_artifact_not_found')
            return dict(row)
        task = one('generation_tasks', TASK_COLUMNS, 'id=?', (task_id,))
        requirement = one('requirements', REQUIREMENT_COLUMNS, 'id=?', (task['requirement_id'],))
        if requirement['project_id'] != task['project_id']:
            raise SafeFailure('requirement_project_mismatch')
        checkpoint = one('task_test_point_checkpoints', CHECKPOINT_COLUMNS, 'task_id=?',
                         (task_id,), ' ORDER BY attempt DESC,id DESC LIMIT 1')
        sql = 'SELECT ' + ','.join(POINT_COLUMNS) + ' FROM test_points WHERE task_id=? AND checkpoint_id=? ORDER BY sort_order,id'
        points = [dict(row) for row in connection.execute(sql, (task_id, checkpoint['id']))]
        for point in points:
            for field in ('is_selected', 'is_excluded'):
                if point[field] not in (0, 1):
                    raise SafeFailure('invalid_point_boolean')
                point[field] = bool(point[field])
            point['citation_ids'] = [row[0] for row in connection.execute(
                'SELECT citation_id FROM test_point_citations WHERE test_point_id=? ORDER BY id', (point['id'],))]
        # Do not read arbitrary detail_json or error messages: SQL projects only known counters/IDs.
        event_columns = ['id', 'task_id', 'step', 'message', 'created_at']
        event_columns += [f"json_extract(detail_json,'$.{key}') AS {key}" for key in EVENT_DETAILS]
        events = [dict(row) for row in connection.execute(
            'SELECT ' + ','.join(event_columns) + " FROM task_events WHERE task_id=? AND step IN ('retrieve','test_points') ORDER BY id", (task_id,))]
        connection.rollback()
    payload = {**checkpoint, 'points': points}
    scenarios = map_scenarios(payload, task_id, None)
    snapshot = {'task': task, 'requirement': requirement, 'checkpoint': checkpoint,
                'points': points, 'event_evidence': events}
    manifest = {'schema_version': '1.0', 'provenance': 'current_database_snapshot',
                'method': 'sqlite_read_only', 'database_path': str(Path(database).resolve()),
                'sqlite_uri_mode': 'ro', 'query_only': True, 'single_read_transaction': True,
                'snapshot_started_utc': started, 'snapshot_completed_utc': now(),
                'source_task_id': task_id, 'project_id': task['project_id'],
                'checkpoint_id': checkpoint['id'], 'attempt': checkpoint['attempt'],
                'version': checkpoint['version'], 'checkpoint_status': checkpoint['status'],
                'service_task_status': task['status'], 'candidate_count': len(points),
                'human_edit_status': 'unknown', 'first_generation_unmodified': 'unknown',
                'not_api_get': True, 'not_original_model_response': True,
                'task_scope_category_ids': ['AUC'], 'task_scope_source': 'operator evaluation plan; requirement title/description support auction-price task',
                'all_points_preserved': True, 'cases_or_drafts_read': False,
                'event_limit': 'retrieve/test_points only; known scalar counters/IDs, no arbitrary detail_json/error',
                'snapshot_canonical_sha256': ev.digest(ev.canonical(snapshot)),
                'point_canonical_sha256': {str(p['id']): ev.digest(ev.canonical(p)) for p in points},
                'text_mapping': 'verbatim title + newline + verification_goal; no rewriting',
                'business_column_allowlists': {'task': TASK_COLUMNS, 'requirement': REQUIREMENT_COLUMNS,
                                             'checkpoint': CHECKPOINT_COLUMNS, 'points': POINT_COLUMNS,
                                             'event_details': EVENT_DETAILS}}
    return snapshot, scenarios, manifest


def partition_inputs(gold, scenarios, manifest, block_size=5):
    if not 1 <= block_size <= 6:
        raise SafeFailure('block_size_out_of_bounds')
    rows = gold['scenarios']
    if len(rows) != 117 or Counter(r['category'] for r in rows)['AUC'] != 19:
        raise SafeFailure('unexpected_gold_inventory')
    inputs = []
    for label, selected in [('AUC', [r for r in rows if r['category'] == 'AUC']),
                            ('OTHER', [r for r in rows if r['category'] != 'AUC'])]:
        for start in range(0, len(selected), block_size):
            block = selected[start:start + block_size]
            inputs.append({'input_id': f'TASK{manifest["source_task_id"]}-{label}-{start // block_size + 1:03d}',
                           'baseline': copy.deepcopy(block),
                           'generated_scenarios': copy.deepcopy(scenarios['scenarios']),
                           'reference_material': [],
                           'task_scope': {'category_ids': CATEGORIES, 'baseline_ids': [r['id'] for r in block],
                                          'reference_scope': 'selected_subset',
                                          'novelty_scope': 'Unmatched/new is relative ONLY to this subset; global novelty requires valid comparison with all 117 gold rows. All real candidates remain present.'},
                           'generation_provenance': {k: manifest[k] for k in ('provenance', 'method', 'human_edit_status', 'first_generation_unmodified', 'source_task_id')}})
    validate_partition(inputs, gold, scenarios['scenarios'])
    return inputs


def validate_partition(inputs, gold, candidates):
    counts = Counter(r['id'] for data in inputs for r in data['baseline'])
    if counts != Counter(r['id'] for r in gold['scenarios']):
        raise SafeFailure('gold_partition_not_exact_once')
    validator = ev.Validator()
    for data in inputs:
        if data['generated_scenarios'] != candidates:
            raise SafeFailure('candidate_partition_loss_or_mutation')
        if set(data['task_scope']['category_ids']) != set(CATEGORIES):
            raise SafeFailure('all_category_scope_required')
        validator.input(data)


def protected_hashes(exclude):
    """Protect frozen assets, original tools/tests, full historical runs and calibration."""
    paths = list((ev.ROOT / 'data/evaluation/bidding-v0.1').rglob('*'))
    paths += list(ev.ASSETS.rglob('*'))
    paths += [ev.PROMPT, Path(ev.__file__), Path(ev.__file__).with_name('safe_http.py'),
              Path(ev.__file__).with_name('sse_fidelity.py'), Path(ev.__file__).with_name('fetch_scenarios.py'),
              Path(ev.__file__).parent / 'tests/test_fetch_scenarios.py']
    exclude = Path(exclude).resolve()
    return {str(p.resolve()): ev.sha(p) for p in paths if p.is_file() and not p.resolve().is_relative_to(exclude)}


def assert_protected(protected):
    changes = [p for p, h in protected.items() if not Path(p).is_file() or ev.sha(p) != h]
    if changes:
        raise SafeFailure('protected_sources_changed')
    return len(protected)


def single_args(input_path, output, execute=False, resume=False):
    argv = ['single', '--input', str(input_path), '--output-dir', str(output),
            '--model', 'gpt-6.1-sol', '--harness-provider', 'new-api',
            '--api-protocol', 'responses', '--stream', '--responses-schema-compat', 'omit-unique-items',
            '--workers', '1', '--max-repairs', '2']
    if execute:
        argv += ['--execute']
    if resume:
        argv += ['--resume']
    return ev.parser().parse_args(argv)


def prepare(database, task_id, output, block_size=5):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise SafeFailure('output_exists_refuse_overwrite')
    snapshot, scenarios, manifest = snapshot_sqlite(database, task_id)
    if task_id == 6 and len(scenarios['scenarios']) != 7:
        raise SafeFailure('task6_expected_seven_current_points')
    gold = ev.load(ev.GOLD)
    inputs = partition_inputs(gold, scenarios, manifest, block_size)
    protected = protected_hashes(output)
    output.mkdir(parents=True)
    ev.save(output / 'snapshot.json', snapshot)
    ev.save(output / 'scenarios.json', scenarios)
    manifest['files'] = {name: {'sha256': ev.sha(output / name), 'bytes': (output / name).stat().st_size}
                         for name in ('snapshot.json', 'scenarios.json')}
    ev.save(output / 'source-manifest.json', manifest)
    ev.save(output / 'protected-before.json', protected)
    (output / 'inputs').mkdir()
    for data in inputs:
        ev.save(output / 'inputs' / (data['input_id'] + '.json'), data)
        code = ev.run(single_args(output / 'inputs' / (data['input_id'] + '.json'), output / 'dry-run' / data['input_id']))
        if code != 0:
            raise SafeFailure('dry_run_failed')
    files = [output / name for name in ('snapshot.json', 'scenarios.json', 'source-manifest.json', 'protected-before.json')]
    files += list((output / 'inputs').glob('*.json'))
    plan = {'tool_version': VERSION, 'tool_sha256': ev.sha(Path(__file__)),
            'provider_public': ev.load(output / 'dry-run' / inputs[0]['input_id'] / 'run.json')['provider_public'],
            'prepared_utc': now(), 'block_size': block_size,
            'input_ids': [d['input_id'] for d in inputs], 'candidate_ids': [r['id'] for r in scenarios['scenarios']],
            'gold_sha256': ev.sha(ev.GOLD), 'gold_count': 117, 'task_main_denominator': 19,
            'other_diagnostic_denominator': 98, 'candidate_denominator': len(scenarios['scenarios']),
            'files': {str(p.relative_to(output)): ev.sha(p) for p in files},
            'dry_run_validated_all_blocks': True, 'dry_run_credentials_or_http': False,
            'smoke_input_id': inputs[0]['input_id'], 'max_workers': 2,
            'technical_recovery': 'one split into <=3-row child blocks only for sse_response_incomplete; no semantic retry',
            'protocol': 'responses', 'stream': True, 'model': 'gpt-6.1-sol', 'responses_schema_compat': 'omit-unique-items'}
    assert_protected(protected)
    ev.save(output / 'plan.json', plan)
    print(ev.canonical({'prepared': str(output), 'blocks': len(inputs), 'candidates': plan['candidate_denominator'], 'gold': 117}))
    return 0


def validate_prepared(output):
    output = Path(output)
    plan = ev.load(output / 'plan.json')
    if plan['tool_sha256'] != ev.sha(Path(__file__)) or plan['gold_sha256'] != ev.sha(ev.GOLD):
        raise SafeFailure('platform_plan_fingerprint_mismatch')
    for path, expected in plan['files'].items():
        if ev.sha(output / path) != expected:
            raise SafeFailure('platform_prepared_artifact_changed')
    protected = ev.load(output / 'protected-before.json')
    assert_protected(protected)
    inputs = [ev.load(output / 'inputs' / (sid + '.json')) for sid in plan['input_ids']]
    scenes = ev.load(output / 'scenarios.json')['scenarios']
    validate_partition(inputs, ev.load(ev.GOLD), scenes)
    return plan, inputs, protected


class CancellableTransport:
    """Share auth/quota cancellation across independent single-input runs."""
    def __init__(self, transport):
        self.transport = transport
        self.redactor = transport.redactor
        self.endpoint = transport.endpoint
        self.cancel = Event()

    def __call__(self, body):
        if self.cancel.is_set():
            raise SafeFailure('cancelled_auth_or_quota_failure')
        response = self.transport(body)
        if response.get('http_status') == 401 or ev.quota_exhausted(response):
            self.cancel.set()
        return response


def evaluate_block(output, data, transport, resume=False, allow_split=True):
    sid = data['input_id']
    if transport.cancel.is_set():
        return {'input_id': sid, 'run': None, 'exit_code': 1,
                'baseline_ids': data['task_scope']['baseline_ids'], 'accepted': None, 'children': [],
                'audit': {'http_attempts': [], 'samples': [{'sample_id': sid, 'errors': ['cancelled_auth_or_quota_failure']}]}}
    input_path = output / 'inputs' / (sid + '.json')
    run = output / 'evaluations' / sid
    if not input_path.exists():
        ev.save(input_path, data)
    code = ev.run(single_args(input_path, run, True, resume=resume and run.exists()), transport=transport)
    checked = inspect_run(run, {sid: data}, redactor=transport.redactor, expected_endpoint=transport.endpoint)
    node = {'input_id': sid, 'run': str(run.relative_to(output)), 'exit_code': code,
            'baseline_ids': data['task_scope']['baseline_ids'], 'audit': {k: v for k, v in checked.items() if k not in ('accepted', 'spec', 'report')},
            'accepted': checked['accepted'].get(sid), 'children': []}
    errors = checked['report']['sample_status'][0].get('errors', [])
    if not node['accepted'] and errors == ['sse_response_incomplete'] and allow_split and len(data['baseline']) > 1 and not transport.cancel.is_set():
        size = (len(data['baseline']) + 1) // 2
        for index, start in enumerate(range(0, len(data['baseline']), size), 1):
            child = copy.deepcopy(data)
            child['input_id'] = sid + f'-R{index}'
            child['baseline'] = data['baseline'][start:start + size]
            child['task_scope']['baseline_ids'] = [r['id'] for r in child['baseline']]
            ev.Validator().input(child)
            node['children'].append(evaluate_block(output, child, transport, resume, allow_split=False))
    return node


def summarize(inputs, nodes):
    gold = [row for data in inputs for row in data['baseline']]
    candidates = inputs[0]['generated_scenarios']
    baseline_results, by_candidate, http, failures, duplicates, reviews = {}, {r['id']: [] for r in candidates}, [], [], {}, []
    def visit(node):
        http.extend(node['audit']['http_attempts'])
        if not node['accepted']:
            failures.append({'input_id': node['input_id'], 'baseline_ids': node['baseline_ids'],
                             'errors': node['audit']['samples'][0].get('errors', []), 'recovered_by_children': bool(node['children'])})
        if node['children']:
            for child in node['children']:
                visit(child)
            return
        if not node['accepted']:
            return
        result = node['accepted']
        for judgment in result['baseline_judgments']:
            bid = judgment['baseline_id']
            if bid in baseline_results:
                raise SafeFailure('duplicate_accepted_baseline')
            baseline_results[bid] = {'input_id': node['input_id'], **judgment}
        for judgment in result['generated_judgments']:
            by_candidate[judgment['generated_id']].append({'input_id': node['input_id'], **judgment})
        for group in result['duplicate_groups']:
            members = tuple(sorted(group['generated_ids']))
            duplicates.setdefault(members, []).append({'input_id': node['input_id'], **group})
        reviews.extend({'input_id': node['input_id'], **row} for row in result['review_queue'])
    for node in nodes:
        visit(node)
    complete = len(baseline_results) == 117
    def coverage(rows):
        valid = [baseline_results[r['id']] for r in rows if r['id'] in baseline_results]
        counts = Counter(j['coverage'] for j in valid)
        return {'planned_denominator': len(rows), 'valid_rows': len(valid), 'technical_unassessed': len(rows) - len(valid),
                'counts': dict(counts), 'full_over_planned': counts['full'] / len(rows),
                'full_or_partial_over_planned': (counts['full'] + counts['partial']) / len(rows),
                'notice': 'technical_unassessed never missing; partial is not full; ratios preliminary when incomplete'}
    summaries = []
    for candidate in candidates:
        rows = by_candidate[candidate['id']]
        links = sorted({bid for row in rows for bid in row['matched_baseline_ids']})
        business = Counter(r['business_consistency'] for r in rows)
        granularity = Counter(r['granularity'] for r in rows)
        disagreements = len(business) > 1 or len(granularity) > 1
        summaries.append({'generated_id': candidate['id'], 'title': candidate['title'],
                          'global_relation': 'matched_to_gold_union' if links else 'global_unmatched_pending_review' if complete else 'incomplete_not_global_new',
                          'global_matched_baseline_ids': links, 'raw_links': [{'input_id': row['input_id'], 'matched_baseline_ids': row['matched_baseline_ids']} for row in rows],
                          'relation_distribution': dict(Counter(r['relation'] for r in rows)),
                          'business_consistency_distribution': dict(business), 'granularity_distribution': dict(granularity),
                          'requires_cross_block_review': disagreements or not links,
                          'cross_block_policy': 'no majority/best-label selection; exact per-block judgments retained',
                          'per_block_judgments': rows})
    tokens = Counter()
    for row in http:
        for key, value in (row.get('usage') or {}).items():
            if isinstance(value, int):
                tokens[key] += value
    return {'status': 'complete' if complete else 'incomplete', 'run_type': 'real_existing_platform_scenario_evaluation_not_calibration',
            'candidate_denominator': len(candidates), 'planned_baseline_denominator': 117,
            'task_scope_auc19': coverage([r for r in gold if r['category'] == 'AUC']),
            'other98_diagnostic_not_task_missing': coverage([r for r in gold if r['category'] != 'AUC']),
            'full117_diagnostic_not_task_pass_rate': coverage(gold),
            'baseline_rows': [{**row, 'judgment': baseline_results.get(row['id']),
                               'technical_status': 'valid' if row['id'] in baseline_results else 'technical_unassessed'} for row in gold],
            'candidate_summaries': summaries, 'candidate_global_matched_count': sum(bool(r['global_matched_baseline_ids']) for r in summaries),
            'candidate_global_unmatched_pending_review_count': sum(r['global_relation'] == 'global_unmatched_pending_review' for r in summaries),
            'duplicate_groups_union': [{'generated_ids': list(members), 'per_block_evidence': items} for members, items in sorted(duplicates.items())],
            'duplicate_policy': 'exact member-set deduplication only; no global semantic rejudgment/transitive merge',
            'review_queue_per_block': reviews, 'technical_failures_and_recovery': failures,
            'http_attempts': http, 'http_count': len(http), 'usage_all_http_attempts': dict(tokens),
            'http_without_returned_usage': sum(not bool(row.get('usage_original')) for row in http),
            'billing_notice': 'returned usage only; absent usage/timeout may still be billed',
            'no_execution_data_or_assertion_scoring': True, 'overall_platform_score_claimed': False,
            'technical_failures_never_missing': True, 'all117_required_for_global_unmatched': True}


def write_reports(directory, report):
    ev.save(directory / 'report.json', report)
    def csv_text(fields, rows):
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()
    rows = []
    for row in report['baseline_rows']:
        j = row['judgment'] or {}
        rows.append({'baseline_id': row['id'], 'category': row['category'], 'technical_status': row['technical_status'],
                     'input_id': j.get('input_id', ''), 'coverage': j.get('coverage', ''),
                     'business_consistency': j.get('business_consistency', ''),
                     'matched_generated_ids': ev.canonical(j.get('matched_generated_ids', [])),
                     'covered_aspects': ev.canonical(j.get('covered_aspects', [])),
                     'missing_aspects': ev.canonical(j.get('missing_aspects', [])), 'reason': j.get('reason', '')})
    ev.save(directory / 'baseline-coverage.csv', csv_text(list(rows[0]), rows), text=True)
    rows = [{'generated_id': r['generated_id'], 'title': r['title'], 'global_relation': r['global_relation'],
             'global_matched_baseline_ids': ev.canonical(r['global_matched_baseline_ids']),
             'relation_distribution': ev.canonical(r['relation_distribution']),
             'business_consistency_distribution': ev.canonical(r['business_consistency_distribution']),
             'granularity_distribution': ev.canonical(r['granularity_distribution']),
             'requires_cross_block_review': r['requires_cross_block_review']} for r in report['candidate_summaries']]
    ev.save(directory / 'candidate-summary.csv', csv_text(list(rows[0]), rows), text=True)
    text = ['# 平台现有真实测试点评测', '', f'- 状态：{report["status"]}；真实候选分母：{report["candidate_denominator"]}（不按块重复计数）。',
            '- 主裁判：gpt-6.1-sol / NewAPI native Responses SSE / store:false / omit-unique-items API投影，本地schema完整保留。',
            '- 来源：current_database_snapshot / sqlite_read_only；当前产物，人工编辑状态 unknown，非原始LLM响应；仅评测试场景，不评执行数据/断言。',
            '', '## 主指标：任务范围 AUC 19', ev.canonical(report['task_scope_auc19']), '',
            '## 其余98：完整gold诊断，不是任务漏测', ev.canonical(report['other98_diagnostic_not_task_missing']), '',
            '## 全117诊断：不是集合竞价任务合格率或平台总体评分', ev.canonical(report['full117_diagnostic_not_task_pass_rate']), '',
            '## 候选关系：7个原始身份、跨块union、保留冲突分布',
            '|候选|全局关系|匹配gold|业务分布|粒度分布|需复核|', '|---|---|---|---|---|---|']
    for r in report['candidate_summaries']:
        text.append('| ' + ' | '.join([r['generated_id'], r['global_relation'], ','.join(r['global_matched_baseline_ids']) or '无',
                                      ev.canonical(r['business_consistency_distribution']), ev.canonical(r['granularity_distribution']), str(r['requires_cross_block_review'])]) + ' |')
    text += ['', '- 不多数择优；原始逐块relation/business/granularity/evidence和links保存在JSON。无匹配仅在全部117有效时标记全局待核查，不自动认可为合理新增。',
             '- 重复关系仅按成员集合跨块去重，披露非全局重判；子集的新意图/复核项不是新增候选数量。',
             '', '## 技术、资源与审计', f'- HTTP调用：{report["http_count"]}；所有HTTP返回usage：{ev.canonical(report["usage_all_http_attempts"])}。',
             f'- 无返回usage的HTTP：{report["http_without_returned_usage"]}；不能假称这些无费用。',
             '- 技术失败永不计missing；单块incomplete最多拆成更小的两个独立run恢复，原失败保留，不按语义重跑。',
             '- 各evaluations目录含原SSE、HTTP请求/安全响应、精确model、usage、原content、wire/legacy、机器验证、status与恢复证据。',
             '- credential只进选中provider进程Authorization，不保存auth header、不送模型。',
             '- 受保护旧资产/历史run哈希核验：' + str(report.get('protected_hashes_unchanged', 'see audit')), '',
             '### 原失败与恢复', ev.canonical(report['technical_failures_and_recovery'])]
    ev.save(directory / 'report.md', '\n'.join(text) + '\n', text=True)


def execute(output, workers=2, resume=False, transport=None):
    output = Path(output)
    if not 1 <= workers <= 2:
        raise SafeFailure('platform_workers_max_two')
    plan, inputs, protected = validate_prepared(output)
    public = provider_public('new-api')
    projected_public = {**public, 'endpoint': public['endpoint'].removesuffix('/chat/completions') + '/responses'}
    if projected_public != plan['provider_public']:
        raise SafeFailure('provider_changed_since_platform_dry_run')
    if (output / 'execution.started.json').exists() and not resume:
        raise SafeFailure('execution_exists_use_explicit_resume')
    if not (output / 'execution.started.json').exists():
        ev.save(output / 'execution.started.json', {'started_utc': now(), 'plan_sha256': ev.sha(output / 'plan.json'), 'workers': workers})
    if transport is None:
        public = provider_public('new-api')
        endpoint = public['endpoint'].removesuffix('/chat/completions') + '/responses'
        key, resolved, config = get_provider('new-api')
        if resolved.removesuffix('/chat/completions') + '/responses' != endpoint:
            raise SafeFailure('provider_endpoint_changed_after_preflight')
        transport = Transport(key, endpoint)
    transport = CancellableTransport(transport)
    smoke = evaluate_block(output, inputs[0], transport, resume)
    smoke_complete = smoke['accepted'] is not None or (smoke['children'] and all(n['accepted'] for n in smoke['children']))
    nodes = [smoke]
    if smoke_complete and not transport.cancel.is_set():
        with ThreadPoolExecutor(max_workers=workers) as pool:
            nodes += list(pool.map(lambda data: evaluate_block(output, data, transport, resume), inputs[1:]))
    report = summarize(inputs, nodes)
    report['prepared_plan_sha256'] = ev.sha(output / 'plan.json')
    report['protected_file_count'] = assert_protected(protected)
    report['protected_hashes_unchanged'] = True
    report['smoke_completed_before_remaining_queue'] = bool(smoke_complete)
    report['completed_utc'] = now()
    echo = [str(p.relative_to(output)) for p in output.rglob('*') if p.is_file() and transport.redactor.clean(p.read_text(encoding='utf-8')) != p.read_text(encoding='utf-8')]
    if echo:
        raise SafeFailure('platform_artifact_auth_echo')
    report['selected_credential_echo_scan_passed'] = True
    session = output / f'platform-session-{len(list(output.glob("platform-session-*"))) + 1:03d}'
    session.mkdir()
    ev.save(session / 'block-execution-audit.json', {'nodes': [{k: v for k, v in n.items() if k != 'accepted'} for n in nodes]})
    write_reports(session, report)
    print(ev.canonical({k: report[k] for k in ('status', 'candidate_denominator', 'task_scope_auc19', 'other98_diagnostic_not_task_missing', 'http_count', 'usage_all_http_attempts')}))
    return 0 if report['status'] == 'complete' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--database', type=Path, required=True)
    p.add_argument('--task-id', type=int, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--block-size', type=int, default=5)
    p = sub.add_parser('execute')
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--workers', type=int, default=2)
    p.add_argument('--model', choices=['gpt-6.1-sol'], required=True)
    p.add_argument('--harness-provider', choices=['new-api'], required=True)
    p.add_argument('--api-protocol', choices=['responses'], required=True)
    p.add_argument('--stream', action='store_true', required=True)
    p.add_argument('--responses-schema-compat', choices=['omit-unique-items'], required=True)
    p.add_argument('--execute', action='store_true', required=True)
    p.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            if args.task_id < 1:
                raise SafeFailure('positive_existing_task_id_required')
            return prepare(args.database, args.task_id, args.output_dir, args.block_size)
        return execute(args.run_dir, args.workers, args.resume)
    except Exception as exc:
        print('error: ' + (str(exc) if isinstance(exc, SafeFailure) else 'local_' + type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
