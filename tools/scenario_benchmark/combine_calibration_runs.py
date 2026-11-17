#!/usr/bin/env python3
"""Pure offline combine: old successes plus ONLY oldfailed S039 -> newvalid S039.
No model/authentication/network calls; references used only after source validation.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from pathlib import Path
import sys
sys.dont_write_bytecode = True
from audit_run import inspect_run
from evaluate_scenarios import ASSETS, ROOT, canonical, load, save, sha

FIELDS = {'baseline_judgments': ('baseline_id', ('coverage', 'business_consistency')),
          'generated_judgments': ('generated_id', ('relation', 'granularity', 'business_consistency'))}
CATEGORIES = ('AUC', 'AMT', 'ACC', 'OPN', 'CLS', 'CAN')


def usage_from_http(http):
    tokens = Counter()
    for h in http:
        for key, value in (h.get('usage') or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                tokens[key] += value
    return dict(sorted(tokens.items()))


def choose_sources(old, new, expected_ids):
    """No label-based best-of selection, not even for a semantically different recovery."""
    assert set(old['spec']['inputs']) == set(expected_ids)
    assert set(old['accepted']) == set(expected_ids) - {'S039'}
    assert old['evaluation_failed_samples'] == ['S039']
    assert set(new['spec']['inputs']) == {'S039'} and set(new['accepted']) == {'S039'}
    assert new['evaluation_failed_samples'] == []
    assert old['spec']['model'] == new['spec']['model'] == 'mimo-v2.6-flash'
    assert old['spec']['endpoint'] == new['spec']['endpoint']
    assert old['spec']['executor_version'] == 'scenario-executor-1.1.1'
    assert new['spec']['executor_version'] == 'scenario-executor-1.1.2'
    return {sid: 'recovery' if sid == 'S039' else 'original' for sid in expected_ids}


def compare_labels(actual, expected, records):
    agreement, differences, sample_exact, auxiliary = {}, [], {}, []
    coverage_classes = {}
    for sid, result in actual.items():
        per_sample = {}
        for field, (idfield, labels) in FIELDS.items():
            refs = {r[idfield]: r for r in expected[sid][field]}
            for label in labels:
                key = field + '.' + label
                stats = agreement.setdefault(key, {'rows_equal': 0, 'rows_compared': 0,
                                                   'samples_exact': 0, 'samples_compared': 0})
                equal = True
                for row in result[field]:
                    ref = refs[row[idfield]]
                    same = row[label] == ref[label]
                    stats['rows_compared'] += 1
                    stats['rows_equal'] += same
                    equal &= same
                    if label == 'coverage':
                        cls = coverage_classes.setdefault(ref[label], {'reference_rows': 0, 'equal_rows': 0, 'predicted_counts': {}})
                        cls['reference_rows'] += 1; cls['equal_rows'] += same
                        cls['predicted_counts'][row[label]] = cls['predicted_counts'].get(row[label], 0) + 1
                    if not same:
                        differences.append({'sample_id': sid, 'target_type': field, 'id': row[idfield],
                                            'label': label, 'actual': row[label], 'reference': ref[label]})
                # Empty-candidate samples do not inflate candidate-label sample denominator.
                if result[field]:
                    stats['samples_compared'] += 1; stats['samples_exact'] += equal
                    per_sample[key] = equal
        def duplicate_signature(items):
            return sorted(sorted(g['generated_ids']) for g in items)
        def review_signature(items):
            return sorted([g['target_type'], g['target_id'], g['issue_type']] for g in items)
        dupe = duplicate_signature(result['duplicate_groups']); refdupe = duplicate_signature(expected[sid]['duplicate_groups'])
        review = review_signature(result['review_queue']); refreview = review_signature(expected[sid]['review_queue'])
        auxiliary.append({'sample_id': sid, 'duplicate_members_equal': dupe == refdupe,
                          'actual_duplicate_members': dupe, 'reference_duplicate_members': refdupe,
                          'review_targets_equal': review == refreview,
                          'actual_review_targets': review, 'reference_review_targets': refreview})
        per_sample['duplicate_members'] = dupe == refdupe
        per_sample['review_targets'] = review == refreview
        sample_exact[sid] = per_sample
    category_stats = {}
    for category in CATEGORIES:
        sids = [sid for sid, data in records.items() if category in data['task_scope']['category_ids']]
        baselines = [(sid, row) for sid, data in records.items() for row in data['baseline'] if row['category'] == category]
        candidates = [(sid, row) for sid, data in records.items() for row in data['generated_scenarios'] if row.get('category') == category]
        coverage_equal = 0
        for sid, row in baselines:
            a = next(r for r in actual[sid]['baseline_judgments'] if r['baseline_id'] == row['id'])
            e = next(r for r in expected[sid]['baseline_judgments'] if r['baseline_id'] == row['id'])
            coverage_equal += a['coverage'] == e['coverage']
        category_stats[category] = {'scope_sample_denominator': len(sids), 'baseline_row_denominator': len(baselines),
                                    'generated_row_denominator': len(candidates), 'coverage_rows_equal': coverage_equal}
    return {'label_agreement': agreement, 'differences': differences, 'sample_exact_by_dimension': sample_exact,
            'coverage': {**agreement['baseline_judgments.coverage'], 'by_reference_class': coverage_classes},
            'auxiliary_comparison': auxiliary, 'six_category_denominators': category_stats,
            'candidate_category_unknown_rows': sum(r.get('category') not in CATEGORIES for d in records.values() for r in d['generated_scenarios']),
            'label_difference_sample_ids': sorted({d['sample_id'] for d in differences}),
            'any_comparison_difference_sample_ids': sorted(sid for sid, flags in sample_exact.items() if not all(flags.values()))}


def combine(old_run, recovery_run, output, dataset):
    old_run, recovery_run, output, dataset = map(Path, (old_run, recovery_run, output, dataset))
    if output.exists() or output.is_symlink():
        raise ValueError('output_exists_refuse_overwrite')
    inputs = {s['sample_id']: s['input'] for s in load(dataset)['samples']}
    assert len(inputs) == 40
    old = inspect_run(old_run, inputs)
    new = inspect_run(recovery_run, inputs)
    selected = choose_sources(old, new, inputs)
    # Verify frozen assets and saved public code snapshots. Historical source changes are expected.
    source_snapshots = {}
    for kind, checked in [('original', old), ('recovery', new)]:
        for source, expected_sha in checked['spec']['sources'].items():
            source_path = Path(source)
            if kind == 'original' and source_path.is_relative_to(ROOT / 'tools/scenario_benchmark'):
                archived = ROOT / 'tools/scenario_benchmark/.qa/v111-source' / source_path.relative_to(ROOT / 'tools/scenario_benchmark')
                assert sha(archived) == expected_sha, 'historical_source_snapshot_sha'
                source_snapshots[source] = {'historical_snapshot': str(archived.relative_to(ROOT)), 'sha256': expected_sha,
                                             'current_sha256': sha(source_path), 'current_matches_original': sha(source_path) == expected_sha}
            else:
                assert sha(source_path) == expected_sha, 'source_asset_sha'
    actual = {**old['accepted'], **new['accepted']}
    assert len(actual) == 40
    assert sum(len(r['baseline_judgments']) for r in actual.values()) == 42
    assert sum(len(r['generated_judgments']) for r in actual.values()) == 40
    # Offline comparison starts only now; labels never enter request/repair/selection.
    expected = {s['sample_id']: s['expected_judgments'] for s in load(dataset)['samples']}
    comparison = compare_labels(actual, expected, inputs)
    old_http, new_http = old['http_attempts'], new['http_attempts']
    assert len(old_http) == old['report']['http_count'] == 48
    assert usage_from_http(old_http) == old['report']['usage']
    assert old['report']['usage']['total_tokens'] == 290254
    assert usage_from_http(new_http) == new['report']['usage']
    old_success_states = [s for s in old['report']['sample_status'] if s['status'] == 'success']
    first = sum(s.get('format_repairs', 0) == 0 for s in old_success_states)
    repaired = len(old_success_states) - first
    assert (first, repaired) == (33, 6)
    report = {'report_version': '1.1.2', 'run_type': 'offline_mixed_version_evaluator_calibration_not_system_score',
              'technical_status': 'all_40_valid', 'sample_count': 40, 'baseline_judgment_count': 42,
              'generated_judgment_count': 40, 'source_versions': ['1.1.1', '1.1.2'],
              'provenance': {'original_first_success': first, 'original_repaired_success': repaired,
                             'later_recovered_samples': ['S039'], 'old_S039_failed_attempt_count': len(list((old_run / 'S039').glob('attempt-*'))),
                             'no_rerun_of_old_39': True, 'no_label_based_selection': True},
              'http_count': len(old_http) + len(new_http), 'usage_all_attempts': usage_from_http(old_http + new_http),
              'resource_sources': {'original': {'http_count': len(old_http), 'usage': usage_from_http(old_http)},
                                   'recovery': {'http_count': len(new_http), 'usage': usage_from_http(new_http)}},
              'S039_actual': actual['S039'], 'recovery_sample_status': new['report']['sample_status'],
              'technical_validation': ['input equals dataset.input', 'saved wire Draft202012', 'legacy Draft202012',
                                       'frozen structure/quote/link/source validator', 'raw response -> wire -> legacy exact',
                                       'model/endpoint/request schema/body/messages binding'],
              'reference_notice': '原基准用户确认；文档派生参考标签未逐条人工标注。仅离线比较，不是准确率或平台成绩。',
              'reason_text_compared': False, 'overall_accuracy_claimed': False,
              'technical_failure_as_missing': False, 'semantics_match_is_separate_from_technical_validity': True,
              **comparison}
    states = {kind: {s['sample_id']: s for s in checked['report']['sample_status']}
              for kind, checked in [('original', old), ('recovery', new)]}
    manifest = {'manifest_version': '1.1.2', 'offline_only': True, 'selection': 'original successes; only oldfailed S039 replaced by newvalid S039, no best-of labels',
                'source_runs': {'original': str(old_run.resolve()), 'recovery': str(recovery_run.resolve())},
                'source_specs': {'original': old['spec'], 'recovery': new['spec']}, 'public_historical_sources': source_snapshots,
                'dataset_sha256': sha(dataset), 'sample_sources': [],
                'source_file_sha256': {str(p.resolve()): sha(p) for run in (old_run, recovery_run) for p in run.rglob('*') if p.is_file()},
                'combined_tool_sha256': sha(Path(__file__)), 'audit_tool_sha256': sha(Path(__file__).with_name('audit_run.py'))}
    for sid in inputs:
        kind = selected[sid]; run = old_run if kind == 'original' else recovery_run
        state = states[kind][sid]
        manifest['sample_sources'].append({'sample_id': sid, 'source': kind, 'executor_version': (old if kind == 'original' else new)['spec']['executor_version'],
                                           'source_attempt_dir': str((run / state['attempt_dir']).resolve()),
                                           'legacy_sha256': state['legacy_sha256'], 'wire_sha256': state['wire_sha256']})
    output.mkdir(parents=True)
    save(output / 'report.json', report)
    save(output / 'source-manifest.json', manifest)
    with (output / 'differences.csv').open('x', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['sample_id', 'target_type', 'id', 'label', 'actual', 'reference'])
        writer.writeheader(); writer.writerows(report['differences'])
    lines = ['# 40样本混合版本离线校准报告 v1.1.2', '',
             '- 技术流程：40/40有效；42主判定、40候选判定。不是平台成绩或总体准确率。',
             '- 来源：v1.1.1 原批33首次成功＋6机器修复成功；v1.1.2仅新恢复S039。',
             '- 原S039全部3个失败attempt保留；不重发39成功样本，不按参考择优。',
             '- 旧保存schema/prompt原样重新本地核验；v1.1.1源码当前哈希已变化，历史公共快照核验通过。',
             '- raw→wire→legacy完全映射，input等于dataset.input，源model/endpoint/body/引用及盲化核验通过。',
             '- 原HTTP/token：' + str(len(old_http)) + '/' + str(usage_from_http(old_http)['total_tokens']),
             '- 新恢复HTTP/token：' + str(len(new_http)) + '/' + str(usage_from_http(new_http)['total_tokens']),
             '- 合计HTTP/token：' + str(report['http_count']) + '/' + str(report['usage_all_attempts']['total_tokens']),
             '- S039主coverage：' + ','.join(r['coverage'] for r in actual['S039']['baseline_judgments']),
             '- S039候选relation/granularity/business：' + canonical([{k: r[k] for k in ('generated_id', 'relation', 'granularity', 'business_consistency')} for r in actual['S039']['generated_judgments']]),
             '', '|比较维度|行相符/分母|样本exact/非空样本分母|', '|---|---:|---:|']
    for key, value in report['label_agreement'].items():
        lines.append(f"|{key}|{value['rows_equal']}/{value['rows_compared']}|{value['samples_exact']}/{value['samples_compared']}|")
    lines += ['', '- 重复集合相符：' + str(sum(x['duplicate_members_equal'] for x in report['auxiliary_comparison'])) + '/40。',
              '- 复核target集合相符：' + str(sum(x['review_targets_equal'] for x in report['auxiliary_comparison'])) + '/40。',
              '- 标签差异样本：' + ', '.join(report['label_difference_sample_ids']),
              '- 任一比较维度差异样本（含重复/复核集合）：' + ', '.join(report['any_comparison_difference_sample_ids']),
              '- 6类分母：' + canonical(report['six_category_denominators']),
              '- 覆盖参考类分母/预测分布：' + canonical(report['coverage']['by_reference_class']),
              '- 技术全valid不等于语义全相符；理由不做字符串比较。',
              '- 参考标签仅此离线report阶段读取；未进入模型、修复或结果选择。',
              '- 详细差异见differences.csv；全部标签/集合及来源见report.json与source-manifest.json。']
    assert len(lines) <= 100
    save(output / 'report.md', '\n'.join(lines) + '\n', text=True)
    print(canonical({'technical_status': report['technical_status'], 'samples': 40, 'baseline': 42, 'generated': 40,
                     'http_count': report['http_count'], 'usage': report['usage_all_attempts'],
                     'label_agreement': report['label_agreement'], 'difference_sample_ids': report['label_difference_sample_ids']}))
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--old-run', type=Path, required=True)
    p.add_argument('--recovery-run', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--dataset', type=Path, default=ASSETS / 'calibration_samples.json')
    args = p.parse_args()
    try:
        combine(args.old_run, args.recovery_run, args.output_dir, args.dataset)
    except Exception as exc:
        print('combine_failed_' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
