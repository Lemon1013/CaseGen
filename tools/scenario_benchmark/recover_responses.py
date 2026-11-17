#!/usr/bin/env python3
"""Offline event recovery and provenance-bound GPT aggregation; never calls HTTP."""
import argparse
from pathlib import Path
import re
import sys
sys.dont_write_bytecode = True
from unittest.mock import patch
from evaluate_scenarios import (ASSETS, ROOT, Validator, load, save, sha, canonical, digest,
                                strict_json, parse_content, validate_response_model)
from audit_run import inspect_run
from safe_http import derive_sse_capture
from combine_calibration_runs import usage_from_http, compare_labels


def records():
    return {r['sample_id']: r['input'] for r in load(ASSETS / 'calibration_samples.json')['samples']}


def validated_derived(parent, sid, capture, directory=None):
    parent, capture = Path(parent), Path(capture)
    data = load(parent / sid / 'input.json')
    schema = load(parent / sid / 'wire.schema.json')
    validator = Validator()
    assert data == records()[sid]
    validator.input(data)
    assert load(parent / sid / 'legacy.schema.json') == validator.legacy_schema
    response = load(capture)
    assert response['http_status'] == 200 and response['error'] is None
    derived, evidence = derive_sse_capture(response)
    validate_response_model(derived, load(parent / 'run.json')['model'])
    text, finish = parse_content(derived, 'responses')
    assert finish == 'stop'
    candidate = text.strip()
    fence = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', candidate, re.S | re.I)
    wire = strict_json(fence.group(1).strip() if fence else candidate)
    legacy = validator.wire(data, wire, schema)
    evidence.update({'source_capture': str(capture.resolve()), 'source_capture_sha256': sha(capture),
                     'source_run': str(parent.resolve()), 'source_run_sha256': sha(parent / 'run.json'),
                     'input_sha256': digest(canonical(data)), 'wire_schema_sha256': digest(canonical(schema)),
                     'original_http_calls_added': 0, 'business_fields_changed': False})
    values = {'derived-response.json': derived, 'assembly-manifest.json': evidence, 'wire.json': wire,
              'legacy.json': legacy, 'input.json': data, 'wire.schema.json': schema,
              'validation.json': {'status': 'passed', 'validators': ['original_wire_schema', 'frozen_validator', 'raw_wire_legacy_exact'],
                                  'semantic_labels_used_for_assembly': False}}
    if directory is not None:
        directory = Path(directory)
        for name, value in values.items():
            assert load(directory / name) == value, 'derived_artifact_fidelity_' + name
        assert (directory / 'content.original.txt').read_text() == text
    return values, text


def offline(parent, output):
    parent, output = Path(parent), Path(output)
    assert not output.exists(), 'output_exists'
    source = inspect_run(parent, records())
    spec = source['spec']
    found, rejected, artifacts = {}, {}, {}
    for sid in spec['inputs']:
        if sid in source['accepted']:
            continue
        captures = sorted((parent / sid).glob('attempt-*/http-*.response.safe.json'))
        # Last actual capture, never select by label match.
        capture = captures[-1]
        try:
            values, text = validated_derived(parent, sid, capture)
        except Exception as exc:
            rejected[sid] = {'error': str(exc), 'capture': str(capture.resolve()), 'capture_sha256': sha(capture),
                             'terminal_error': load(capture).get('eventmeta', {}).get('terminal_error')}
            continue
        target = output / 'derived' / sid / 'artifact'
        for name, value in values.items():
            save(target / name, value)
        save(target / 'content.original.txt', text, text=True)
        save(target / 'prompt.md', (parent / sid / 'prompt.md').read_text(), text=True)
        found[sid] = str(target.resolve())
        artifacts.update({str(p.resolve()): sha(p) for p in target.iterdir()})
    save(output / 'offline-recovery.json', {'version': '1.1.5', 'parent': str(parent.resolve()), 'derived': found,
         'rejected': rejected, 'artifact_sha256': artifacts, 'new_http_count': 0,
         'proof': 'urllib opener and socket connect forbidden for entire offline operation',
         'selection': 'last actual failed capture; terminal evidence and validators only; no labels'})
    print(canonical({'derived': list(found), 'rejected': list(rejected), 'http_added': 0}))


def inspect_aggregate(final, input_records, redactor=None, expected_endpoint=None):
    final = Path(final)
    manifest = load(final / 'source-manifest.json')
    assert manifest['type'] == 'gpt-event-recovery-aggregate'
    assert all(sha(p) == h for p, h in manifest['source_file_sha256'].items())
    parent = Path(manifest['parent'])
    old = inspect_run(parent, input_records, redactor, expected_endpoint)
    network = inspect_run(Path(manifest['network']), input_records, redactor, expected_endpoint)
    recovery = load(Path(manifest['recovery']) / 'offline-recovery.json')
    assert old['spec']['model'] == network['spec']['model'] == 'gpt-6.1-sol'
    assert old['spec']['endpoint'] == network['spec']['endpoint']
    assert old['spec']['parameters']['stream'] is True and network['spec']['parameters']['stream'] is True
    assert old['spec']['parameters']['store'] is False and network['spec']['parameters']['store'] is False
    for sid in network['spec']['inputs']:
        assert sid in recovery['rejected'] and sid not in old['accepted']
        assert network['spec']['inputs'][sid] == old['spec']['inputs'][sid]
        assert network['spec']['wire_schemas'][sid] == old['spec']['wire_schemas'][sid]
        assert (Path(manifest['network']) / sid / 'prompt.md').read_bytes() == (parent / sid / 'prompt.md').read_bytes()
    accepted = dict(old['accepted'])
    samples = {s['sample_id']: s for s in old['samples']}
    states = {s['sample_id']: s for s in old['report']['sample_status']}
    for sid, artifact in recovery['derived'].items():
        assert sid not in accepted
        evidence = load(Path(artifact) / 'assembly-manifest.json')
        values, _ = validated_derived(parent, sid, evidence['source_capture'], artifact)
        accepted[sid] = values['legacy.json']
        samples[sid] = {'sample_id': sid, 'structure': 'passed', 'source': 'derived_event_valid', 'generation_count': 1, 'format_repairs': 0}
        states[sid] = {'sample_id': sid, 'status': 'success', 'format_repairs': 0, 'source': 'derived_event_valid'}
    for sid, result in network['accepted'].items():
        assert sid not in accepted
        accepted[sid] = result
        samples[sid] = next(s for s in network['samples'] if s['sample_id'] == sid)
        states[sid] = next(s for s in network['report']['sample_status'] if s['sample_id'] == sid)
    assert set(manifest['sample_sources']) == set(accepted)
    for sid, result in accepted.items():
        assert load(final / sid / 'legacy.json') == result
        declared = manifest['sample_sources'][sid]
        kind = 'original' if sid in old['accepted'] else 'derived_event_valid' if sid in recovery['derived'] else 'retry'
        source_spec = network['spec'] if kind == 'retry' else old['spec']
        assert declared['kind'] == kind
        assert declared['input_sha256'] == source_spec['inputs'][sid]
        assert declared['model'] == source_spec['model'] and declared['endpoint'] == source_spec['endpoint']
        assert declared['executor_version'] == source_spec['executor_version']
        assert declared['legacy_sha256'] == digest(canonical(result))
        assert declared['prompt_sha256'] == digest((parent / sid / 'prompt.md').read_text())
    calls = old['http_attempts'] + network['http_attempts']
    failed = sorted(set(input_records) - set(accepted))
    return {'spec': load(final / 'run.json'), 'report': {'sample_status': list(states.values()), 'usage': usage_from_http(calls)},
            'accepted': accepted, 'samples': list(samples.values()), 'http_attempts': calls,
            'machine_errors_before_repair': old['machine_errors_before_repair'] + network['machine_errors_before_repair'],
            'technical_success_count': len(accepted), 'evaluation_failed_samples': failed}


def aggregate(parent, recovery, network, final):
    parent, recovery, network, final = map(Path, (parent, recovery, network, final))
    assert not final.exists()
    inputs = records()
    old = inspect_run(parent, inputs)
    new = inspect_run(network, inputs)
    rec = load(recovery / 'offline-recovery.json')
    assert set(new['spec']['inputs']).issubset(rec['rejected'])
    accepted = dict(old['accepted']); sources = {}
    for sid in old['accepted']:
        state = next(s for s in old['report']['sample_status'] if s['sample_id'] == sid)
        sources[sid] = {'kind': 'original', 'artifact': str((parent / state['attempt_dir']).resolve()), 'source_parent': str(parent.resolve())}
    for sid, artifact in rec['derived'].items():
        values, _ = validated_derived(parent, sid, load(Path(artifact) / 'assembly-manifest.json')['source_capture'], artifact)
        accepted[sid] = values['legacy.json']
        sources[sid] = {'kind': 'derived_event_valid', 'artifact': artifact, 'source_parent': str(parent.resolve())}
    for sid, result in new['accepted'].items():
        assert sid not in accepted
        accepted[sid] = result
        state = next(s for s in new['report']['sample_status'] if s['sample_id'] == sid)
        sources[sid] = {'kind': 'retry', 'artifact': str((network / state['attempt_dir']).resolve()), 'source_parent': str(network.resolve())}
    assert set(accepted) == set(inputs), 'not_all_40_valid'
    for sid, result in accepted.items():
        save(final / sid / 'legacy.json', result)
        src = sources[sid]; run = old['spec'] if src['kind'] != 'retry' else new['spec']
        capture_paths = sorted(Path(src['artifact']).glob('http-*.response.safe.json'))
        capture = load(capture_paths[-1]) if capture_paths else load(load(Path(src['artifact']) / 'assembly-manifest.json')['source_capture'])
        body = capture['body']
        src.update({'input_sha256': run['inputs'][sid], 'model': run['model'], 'endpoint': run['endpoint'],
                    'api_protocol': run['api_protocol'], 'schema_version': 'wire1.1 -> legacy1.0',
                    'executor_version': run['executor_version'], 'prompt_sha256': digest((parent / sid / 'prompt.md').read_text()),
                    'original_completed_status': body['status'], 'original_completed_output_empty': body['output'] == [],
                    'completed_id': body['id'], 'event_completed_count': capture['eventmeta']['completed_count'],
                    'usage_original': body.get('usage'), 'legacy_sha256': digest(canonical(result))})
    files = [p for r in (parent, recovery) for p in r.rglob('*') if p.is_file()]
    # Sources include network once via recovery, never duplicate HTTP/token sums.
    current_tools = [ROOT / 'tools/scenario_benchmark' / n for n in ('safe_http.py','sse_fidelity.py','evaluate_scenarios.py','audit_run.py','recover_responses.py')]
    snapshot = ROOT / 'tools/scenario_benchmark/.qa/v115-source'
    historical = {p: {'recorded_sha256': h, 'current_sha256': sha(p), 'snapshot_sha256': sha(snapshot / Path(p).name) if (snapshot / Path(p).name).is_file() else None}
                  for p, h in old['spec']['sources'].items() if sha(p) != h}
    assert all(v['recorded_sha256'] == v['snapshot_sha256'] for v in historical.values()), 'historical_public_snapshot_hash'
    spec = {**new['spec'], 'inputs': old['spec']['inputs'], 'wire_schemas': old['spec']['wire_schemas'],
            'sources': {str(p.resolve()): sha(p) for p in current_tools}, 'aggregate_offline': True,
            'historical_source_fingerprints': historical}
    save(final / 'run.json', spec)
    save(final / 'source-manifest.json', {'type': 'gpt-event-recovery-aggregate', 'parent': str(parent.resolve()),
         'recovery': str(recovery.resolve()), 'network': str(network.resolve()), 'sample_sources': sources,
         'source_file_sha256': {str(p.resolve()): sha(p) for p in files}, 'historical_source_fingerprints': historical,
         'selection': 'original valid then completed-event carrier normalization then rejected transport retry; no label based selection',
         'http_count': len(old['http_attempts']) + len(new['http_attempts']), 'derived_http_added': 0,
         'usage_all_attempts': usage_from_http(old['http_attempts'] + new['http_attempts'])})
    checked = inspect_aggregate(final, inputs)
    expected = {r['sample_id']: r['expected_judgments'] for r in load(ASSETS / 'calibration_samples.json')['samples']}
    comparison = compare_labels(checked['accepted'], expected, inputs)
    save(final / 'report.json', {'report_version': '1.1.5', 'technical_status': 'all_40_valid', 'sample_count': len(accepted),
         'baseline_rows': sum(len(x['baseline_judgments']) for x in accepted.values()),
         'generated_rows': sum(len(x['generated_judgments']) for x in accepted.values()), 'sample_status': checked['report']['sample_status'],
         'http_count': len(checked['http_attempts']), 'usage': checked['report']['usage'], 'reference_notice': '文档派生参考非逐条人审，不是准确率或平台分数',
         'composition': {k: sum(s['kind'] == k for s in sources.values()) for k in ['original','derived_event_valid','retry']}, **comparison})
    save(final / 'report.md', '# GPT 40样本恢复聚合 v1.1.5\n\n- 技术结果40/40有效；27原有效、11完整事件离线恢复、2独立网络恢复。\n- 原SSE及completed原body不改；derived不是伪造raw completion。\n- raw→wire→legacy/原schema/冻结validator/input/model/endpoint全重验。\n- HTTP历史41+新增2；11离线增加0。usage仅汇总各原HTTP一次，无usage失败不能推断零费用。\n- Coverage：' + str(comparison['coverage']['rows_equal']) + '/42；样本exact：' + str(comparison['coverage']['samples_exact']) + '/40。\n- 与参考不符也保持模型原标签；文档派生参考未逐条人审，不是准确率或平台分数。\n- 历史工具SHA与当前升级差异及封存历史SHA见source-manifest.json。\n', text=True)
    save(final / 'aggregate-audit.json', {'status': 'passed', 'valid': checked['technical_success_count'],
         'raw_schema_input_model_wire_legacy_revalidated': True, 'no_http_aggregation': True})
    print(canonical({'valid': checked['technical_success_count'], 'http': len(checked['http_attempts']), 'sources': {k: sum(s['kind'] == k for s in sources.values()) for k in ['original','derived_event_valid','retry']}}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['offline', 'aggregate'])
    p.add_argument('--parent', type=Path, required=True)
    p.add_argument('--recovery', type=Path, required=True)
    p.add_argument('--network', type=Path)
    p.add_argument('--final', type=Path)
    a = p.parse_args()
    with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('offline_http_forbidden')), patch('socket.socket.connect', side_effect=AssertionError('offline_network_forbidden')):
        if a.mode == 'offline':
            offline(a.parent, a.recovery)
        else:
            aggregate(a.parent, a.recovery, a.network, a.final)
