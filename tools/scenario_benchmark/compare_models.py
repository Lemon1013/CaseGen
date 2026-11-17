#!/usr/bin/env python3
"""Audited offline GPT/MiMo comparison; no HTTP and no label-based selection."""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from datetime import datetime
from pathlib import Path
import re
import sys
import urllib.parse
sys.dont_write_bytecode = True
from audit_run import inspect_run
from combine_calibration_runs import compare_labels, usage_from_http
from evaluate_scenarios import ASSETS, ROOT, canonical, load, save, sha
from safe_http import get_provider, Redactor


def resources(checked):
    calls = checked['http_attempts']
    states = checked['report']['sample_status']
    starts, ends = [], []
    run = Path(checked['run_path'])
    for path in run.glob('*/attempt-*/http-*.started.json'):
        tick = datetime.fromisoformat(load(path)['utc']).timestamp()
        response = path.with_name(path.name.replace('.started.json', '.response.safe.json'))
        starts.append(tick)
        if response.exists():
            ends.append(tick + (load(response).get('duration_seconds') or 0))
    if not starts:
        for call in calls:
            if call.get('started_utc'):
                tick = datetime.fromisoformat(call['started_utc']).timestamp()
                starts.append(tick); ends.append(tick + (call.get('duration_seconds') or 0))
    return {'http_count': len(calls), 'http_status_counts': dict(Counter(str(x['status']) for x in calls)),
            'usage_all_attempts': usage_from_http(calls),
            'responses_with_usage': sum(isinstance(h.get('usage'), dict) for h in calls),
            'usage_notice': '未返回usage的失败请求不能推断token为零；统计只汇总实际返回usage。',
            'http_duration_seconds_sum': round(sum(x.get('duration_seconds') or 0 for x in calls), 6),
            'observed_request_window_seconds': round(max(ends) - min(starts), 6) if starts and ends else None,
            'first_format_success': sum(s['status'] == 'success' and s.get('format_repairs', 0) == 0 for s in states),
            'repaired_success': sum(s['status'] == 'success' and s.get('format_repairs', 0) > 0 for s in states),
            'valid_sample_count': len(checked['accepted']), 'failed_samples': checked['evaluation_failed_samples'],
            'monetary_cost': None, 'price_notice': '单价未知；不编造货币成本，token含全部修复/失败HTTP usage。'}


def compare(gpt_run, mimo_final, output, protected, tests_log, smoke_runs):
    output, gpt_run, mimo_final = map(Path, (output, gpt_run, mimo_final))
    if output.exists() or output.is_symlink():
        raise ValueError('output_exists_refuse_overwrite')
    before = load(protected)
    changed = [p for p, h in before.items() if not Path(p).is_file() or sha(p) != h]
    assert not changed, 'protected_sources_or_old_runs_changed'
    dataset = ASSETS / 'calibration_samples.json'
    wrappers = load(dataset)['samples']
    records = {s['sample_id']: s['input'] for s in wrappers}
    expected = {s['sample_id']: s['expected_judgments'] for s in wrappers}
    assert len(records) == 40
    assert sum(len(s['baseline']) for s in records.values()) == 42
    assert sum(len(s['generated_scenarios']) for s in records.values()) == 40
    # Authentication resolution ONLY selects the scalar to scan local artifacts. No HTTP.
    key, chat_endpoint, public = get_provider(provider='new-api')
    redactor = Redactor([key])
    manifest = load(mimo_final / 'source-manifest.json')
    assert all(Path(p).is_file() and sha(p) == h for p, h in manifest['source_file_sha256'].items()), 'mimo_source_manifest_changed'
    old_path = Path(manifest['source_runs']['original'])
    recovery_path = Path(manifest['source_runs']['recovery'])
    old = inspect_run(old_path, records, redactor, chat_endpoint)
    recovery = inspect_run(recovery_path, records, redactor, chat_endpoint)
    assert set(old['accepted']) == set(records) - {'S039'}
    assert set(recovery['accepted']) == {'S039'}
    mimo = {**old['accepted'], **recovery['accepted']}
    assert old['spec']['model'] == recovery['spec']['model'] == 'mimo-v2.6-flash'
    gpt_spec = load(gpt_run / 'run.json')
    endpoint = chat_endpoint.removesuffix('/chat/completions') + '/responses' if gpt_spec.get('api_protocol') == 'responses' else chat_endpoint
    aggregate_manifest = gpt_run / 'source-manifest.json'
    is_aggregate = aggregate_manifest.is_file() and load(aggregate_manifest).get('type') == 'gpt-event-recovery-aggregate'
    if is_aggregate:
        from recover_responses import inspect_aggregate
        gpt = inspect_aggregate(gpt_run, records, redactor, endpoint)
    else:
        gpt = inspect_run(gpt_run, records, redactor, endpoint)
    assert gpt_spec['model'] == 'gpt-6.1-sol'
    assert all(sha(p) == h for p, h in gpt_spec['sources'].items()), 'gpt_source_fingerprint_changed'
    checked_smokes = []
    for run in map(Path, smoke_runs):
        item = inspect_run(run, records, redactor)
        item['run_path'] = str(run.resolve())
        checked_smokes.append(item)
    valid_records = {sid: records[sid] for sid in gpt['accepted']}
    gpt_metrics = compare_labels(gpt['accepted'], expected, valid_records) if valid_records else {}
    mimo_metrics = compare_labels(mimo, expected, records)
    gpt_vs_mimo = compare_labels(gpt['accepted'], mimo, valid_records) if valid_records else {}
    old['run_path'], recovery['run_path'], gpt['run_path'] = map(lambda p: str(p.resolve()), (old_path, recovery_path, gpt_run))
    gpt_resources = resources(gpt)
    smokes_resources = [resources(c) for c in checked_smokes]
    all_new_http = gpt['http_attempts'] + [h for c in checked_smokes for h in c['http_attempts']]
    prediction_counts = {name: dict(Counter(row['coverage'] for result in results.values() for row in result['baseline_judgments']))
                         for name, results in [('gpt_valid_subset', gpt['accepted']), ('mimo_same_subset', {sid: mimo[sid] for sid in gpt['accepted']}), ('mimo_full', mimo)]}
    failed_http = []
    gpt_http_runs = [gpt_run]
    if is_aggregate:
        gm = load(aggregate_manifest)
        gpt_http_runs = [Path(gm['parent']), Path(gm['network'])]
    for run in [*gpt_http_runs, *map(Path, smoke_runs)]:
        for file in sorted(run.glob('*/attempt-*/http-*.response.safe.json')):
            response = load(file)
            if response.get('http_status') != 200 or response.get('error'):
                failed_http.append({'source': str(file.resolve()), 'status': response.get('http_status'), 'request_ids': response.get('request_ids'),
                                    'transport_error': response.get('error'), 'sse_terminal_error': response.get('eventmeta', {}).get('terminal_error'),
                                     'error': response.get('body', {}).get('error') if isinstance(response.get('body'), dict) else response.get('error')})
    report = {'report_version': '1.1.5', 'run_type': 'offline_evaluator_comparison_not_platform_score',
              'dataset_denominators': {'samples': 40, 'baseline_rows': 42, 'generated_rows': 40, 'empty_generated_samples': 6, 'nonempty_generated_samples': 34},
              'executed_source_sample_count': len(gpt_spec['inputs']),
              'full40_executed': set(gpt_spec['inputs']) == set(records),
              'gateway_error_evidence': failed_http,
              'coverage_prediction_counts': prediction_counts,
              'conservative_comparison_notice': '无有效GPT输出，不能判定更保守' if not gpt['accepted'] else '仅在同一有效subset比较missing/unresolved预测数量，不扩大为全局或准确率结论',
              'valid_denominators': {'gpt_samples': len(gpt['accepted']), 'gpt_baseline_rows': sum(len(r['baseline_judgments']) for r in gpt['accepted'].values()),
                                     'gpt_generated_rows': sum(len(r['generated_judgments']) for r in gpt['accepted'].values()), 'mimo_samples': 40},
              'technical_status': 'all_40_valid' if len(gpt['accepted']) == 40 else 'technical_failure_not_missing',
              'gpt': {'metrics': gpt_metrics, 'resources': gpt_resources, 'source_spec': gpt_spec},
              'mimo': {'metrics': mimo_metrics, 'resources': {'original': resources(old), 'recovery': resources(recovery),
                        'http_count': len(old['http_attempts']) + len(recovery['http_attempts']),
                        'usage_all_attempts': usage_from_http(old['http_attempts'] + recovery['http_attempts'])}},
              'gpt_vs_mimo_on_same_valid_subset': gpt_vs_mimo,
              'smoke_resources': smokes_resources, 'historical_plus_recovery_and_smoke_total_http': len(all_new_http), 'this_recovery_new_http': len(gpt['http_attempts']) - len(inspect_run(Path(gm['parent']), records)['http_attempts']) if is_aggregate else len(gpt['http_attempts']), 'this_round_total_http': len(all_new_http),
              'this_round_usage': usage_from_http(all_new_http),
              'protocol_disclosure': 'GPT v1.1.4使用原生Responses SSE /responses，stream:true/store:false/native text.format/max_output_tokens:8192（技术长度修复至16384）；API schema投影仅去uniqueItems，本地/原prompt完整schema不变。旧MiMo v1.1.1+S039 v1.1.2使用Chat/nonstream /chat/completions，无store参数；动态schema和结构协议存在版本差异，不是严格只换模型的A/B。业务policy、原prompt、sample.input和派生参考不变。',
              'reference_notice': '文档派生参考未逐条人工审阅；相符率非准确率或平台成绩。',
              'technical_failure_as_missing': False, 'overall_accuracy_claimed': False,
              'reason_text_compared': False, 'no_new_mimo_http': True, 'no_label_based_retry_or_selection': True}
    paths = [p for run in [gpt_run, *gpt_http_runs, *map(Path, smoke_runs), old_path, recovery_path, mimo_final] for p in run.rglob('*') if p.is_file()]
    if is_aggregate:
        paths += [p for p in Path(gm['recovery']).rglob('*') if p.is_file()]
    paths = list(dict.fromkeys(paths))
    paths += [p for p in (ROOT / 'tools/scenario_benchmark/.qa').glob('*.log')]
    spellings = {key, urllib.parse.quote(key, safe=''), __import__('json').dumps(key, ensure_ascii=False)[1:-1]}
    echoes = [str(p.resolve()) for p in paths if any(s.encode() in p.read_bytes() for s in spellings if s)]
    assert not echoes, 'selected_credential_echo'
    text = Path(tests_log).read_text()
    match = re.search(r'Ran (\d+) tests', text)
    assert match and re.search(r'^OK$', text, re.M), 'tests_not_passed'
    safety = {'status': 'passed', 'protected_file_count': len(before), 'protected_files_changed': changed,
              'selected_credential_echo_paths': echoes, 'echo_scanned_file_count': len(paths), 'test_count': int(match.group(1)), 'tests_exit_code': 0,
              'all_acceptance_revalidated_from_raw': True, 'input_dataset_verbatim': True, 'core_validator_passed': True,
              'raw_wire_legacy_exact': True, 'request_response_model_bound': True,
              'native_usage_original_preserved': True, 'blind_original_messages': True,
              'tls_verification': True, 'redirects': 'refused', 'timeout_seconds': 120, 'response_limit_bytes': 2*1024*1024,
              'credential_in_model_context': False, 'auth_headers_archived': False, 'no_pending_jobs': True,
              'safety_pass_is_not_semantic_accuracy': True,
              'machine_errors_before_repair': gpt['machine_errors_before_repair'],
              'failed_samples': gpt['evaluation_failed_samples'], 'profile_api_public': public.get('profile_api')}
    new_manifest = {'offline_only': True, 'dataset_sha256': sha(dataset), 'gpt_source_spec': gpt_spec,
                    'mimo_manifest_sha256': sha(mimo_final / 'source-manifest.json'),
                    'selection': 'GPT original-valid + fully-completed-event-carriers + bounded transport recovery; MiMo existing39+1; never best-of labels',
                    'mimo_sample_sources': manifest['sample_sources'],
                    'gpt_recovery_manifest': load(aggregate_manifest) if is_aggregate else None, 'gpt_samples': gpt['samples'], 'source_file_sha256': {str(p.resolve()): sha(p) for p in paths},
                    'tool_sha256': {str(p.resolve()): sha(p) for p in (Path(__file__), Path(__file__).with_name('audit_run.py'))},
                    'tools_modified': ['evaluate_scenarios.py', 'safe_http.py', 'audit_run.py', 'tests/test_evaluate_scenarios.py', 'tests/test_compare_models.py', 'tests/test_responses_sse.py', 'README.md', 'compare_models.py'],
                    'protected_before_sha256': sha(protected)}
    for metrics in (gpt_metrics, mimo_metrics, gpt_vs_mimo):
        nonempty = [x for x in metrics.get('auxiliary_comparison', []) if records[x['sample_id']]['generated_scenarios']]
        if metrics:
            metrics['nonempty_duplicate_groups'] = {'samples_compared': len(nonempty), 'samples_equal': sum(x['duplicate_members_equal'] for x in nonempty), 'empty_samples_excluded': 6}
    output.mkdir(parents=True)
    if is_aggregate:
        report['gpt']['source_composition'] = dict(Counter(s['kind'] for s in gm['sample_sources'].values()))
        report['historical_source_fingerprint_changes'] = gm['historical_source_fingerprints']
        report['derived_event_fidelity_notice'] = '11例仅从完整且无错误的原终结事件恢复assistant output_text；空原completed.output不改；derived不是伪造raw completion，新增HTTP0。'
    save(output / 'report.json', report)
    save(output / 'source-manifest.json', new_manifest)
    save(output / 'safety-audit.json', safety)
    with (output / 'differences.csv').open('x', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['comparison','sample_id','target_type','id','label','actual','reference'])
        writer.writeheader()
        for name, metrics in (('GPT vs derived reference', gpt_metrics), ('MiMo vs derived reference', mimo_metrics), ('GPT vs MiMo valid subset', gpt_vs_mimo)):
            writer.writerows({'comparison': name, **d} for d in metrics.get('differences', []))
    lines = ['# GPT 与 MiMo 真实执行离线对照 v1.1.5 SSE恢复', '',
             '- 直接GPT技术结果：' + report['technical_status'] + '；有效分母见下表，完整数据40样本/42主行/40候选行。',
             '- ' + report['protocol_disclosure'], '- ' + report['reference_notice'],
             '- 标签差异不触发重试；技术失败不转missing；旧MiMo不重跑。', '- 组装来源：' + canonical(report['gpt'].get('source_composition',{})) + '。原27保留；11完整事件离线恢复；2超载独立网络恢复。', '- 原completed.output空是载体遗漏，不是JSON标签修复；严格done/delta/item/part/model/ID/sequence及唯一终结一致后仅派生text；原SSE不覆盖。', '- 历史run工具hash与当前升级不一致已披露；旧公开代码snapshot封存并核验历史SHA，未改旧run保护证据。',
             '', '|指标|GPT 相符/有效分母|旧MiMo 相符/分母|', '|---|---:|---:|']
    for keyname, stats in mimo_metrics['label_agreement'].items():
        actual = gpt_metrics.get('label_agreement', {}).get(keyname, {'rows_equal': 0, 'rows_compared': 0})
        gpt_cell = f"{actual['rows_equal']}/{actual['rows_compared']}" if actual['rows_compared'] else '不可测（有效n=0）'
        lines.append(f"|{keyname}|{gpt_cell}|{stats['rows_equal']}/{stats['rows_compared']}|")
    for name, metrics in (('GPT', gpt_metrics), ('MiMo', mimo_metrics)):
        nonempty = [x for x in metrics.get('auxiliary_comparison', []) if records[x['sample_id']]['generated_scenarios']]
        metrics['nonempty_duplicate_groups'] = {'samples_compared': len(nonempty), 'samples_equal': sum(x['duplicate_members_equal'] for x in nonempty), 'notice': '排除6个空候选样本；全集40仅作附带核对，非空有效分母34'}
        cov = metrics.get('coverage', {})
        lines += ['', f"- {name} coverage sampleExact：{cov.get('samples_exact',0)}/{cov.get('samples_compared',0)}。",
                  f"- {name} 六类分母及coverage：" + canonical(metrics.get('six_category_denominators', {})),
                  f"- {name} 按reference coverage类相符/预测分布：" + canonical(cov.get('by_reference_class', {})),
                  f"- {name} 重复无序成员集合（ignore group ID）相符：" + str(sum(x['duplicate_members_equal'] for x in metrics.get('auxiliary_comparison', []))) + '/' + str(len(metrics.get('auxiliary_comparison', []))),
                  f"- {name} 非空候选重复成员集合相符：" + str(sum(x['duplicate_members_equal'] for x in nonempty)) + '/' + str(len(nonempty)),
                   f"- {name} 辅助review target集合相符：" + str(sum(x['review_targets_equal'] for x in metrics.get('auxiliary_comparison', []))) + '/' + str(len(metrics.get('auxiliary_comparison', [])))]
    lines += ['', '- 是否执行完整40：' + str(report['full40_executed']) + '；实际源run包含样本数：' + str(report['executed_source_sample_count']),
              '- GPT是否更保守：' + report['conservative_comparison_notice'] + '；预测counts：' + canonical(prediction_counts),
              '- GPT 所选源run实际HTTP/时间/token/首次及修复（并不默认完成40）：' + canonical(gpt_resources),
              '- GPT历史41+新增网络及单列smoke全部HTTP：' + str(report['this_round_total_http']) + '；全部usage：' + canonical(report['this_round_usage']),
              '- 单价未知，不编造货币成本；native input/output tokens映射统计，原usage安全归档；没有usage不能声称token为0。',
              '- 安全：raw→wire→legacy、输入/规则隔离、source型号/endpoint、本地结构/引用/links全复核；旧源/旧runs SHA未变，选中key扫描无回显。',
              '- 测试：' + str(safety['test_count']) + ' tests pass，exit0；执行器/安全模块/audit/测试/README/离线compare工具有明确修改，不声称tools未变。',
              '- 全部详细字段、有效分母、不同集合、来源SHA见report.json/differences.csv/source-manifest.json/safety-audit.json；无pending。']
    assert len(lines) <= 90, 'report_line_limit'
    save(output / 'report.md', '\n'.join(lines) + '\n', text=True)
    print(canonical({'technical_status': report['technical_status'], 'gpt_resources': gpt_resources,
                     'gpt_coverage': gpt_metrics.get('coverage'), 'mimo_coverage': mimo_metrics['coverage'],
                     'tests': safety['test_count'], 'protected_changed': changed, 'echoes': echoes}))
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gpt-run', required=True, type=Path)
    p.add_argument('--mimo-final', required=True, type=Path)
    p.add_argument('--output-dir', required=True, type=Path)
    p.add_argument('--protected', required=True, type=Path)
    p.add_argument('--tests-log', required=True, type=Path)
    p.add_argument('--smoke-runs', nargs='+', required=True, type=Path)
    args = p.parse_args()
    try:
        compare(args.gpt_run, args.mimo_final, args.output_dir, args.protected, args.tests_log, args.smoke_runs)
    except Exception as exc:
        print('compare_failed_' + type(exc).__name__ + ':' + str(exc), file=sys.stderr)
        sys.exit(1)
