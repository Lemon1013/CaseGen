#!/usr/bin/env python3
"""Local safety/provenance audit; passing is NOT all evaluations valid."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys
import urllib.parse
sys.dont_write_bytecode = True
from evaluate_scenarios import (ASSETS, ROOT, Validator, canonical, digest, load, save,
                               sha, messages_for, validate_response_model, strict_json, parse_content,
                               body_for, MODES, usage_for)
from safe_http import get_provider, Redactor, inspect_sse_capture, effective_response


def inspect_run(run, records, redactor=None, expected_endpoint=None):
    """Read saved input/schema/prompt, never substitute the current wire schema."""
    run = Path(run)
    spec = load(run / 'run.json')
    protocol = spec.get('api_protocol', 'chat-completions')
    if expected_endpoint is not None:
        assert spec['endpoint'] == expected_endpoint, 'endpoint_spec_binding'
    report = load(sorted(run.glob('session-*/report.json'))[-1])
    states = {s['sample_id']: s for s in report['sample_status']}
    assert set(states) == set(spec['inputs'])
    validator = Validator()
    samples, http, issues, accepted = [], [], [], {}
    for sid in spec['inputs']:
        directory = run / sid
        state = states[sid]
        attempts = sorted(directory.glob('attempt-*'))
        # A cancelled before-start sample need not have canonical input or attempts.
        if not directory.exists():
            assert state['status'] != 'success'
            samples.append({'sample_id': sid, 'structure': 'evaluation_failed',
                            'errors': state.get('errors', []), 'generation_count': 0})
            continue
        data = load(directory / 'input.json')
        assert data == records[sid], 'input_verbatim'
        validator.input(data)
        schema = load(directory / 'wire.schema.json')
        assert digest(canonical(data)) == spec['inputs'][sid]
        assert digest(canonical(schema)) == spec['wire_schemas'][sid]
        assert load(directory / 'legacy.schema.json') == validator.legacy_schema
        prompt = (directory / 'prompt.md').read_text()
        assert digest(prompt) == spec['sources'][str(ROOT / 'tools/scenario_benchmark/prompts/evaluator_v1.1.md')]
        for attempt in attempts:
            if (attempt / 'validation.json').exists():
                validation = load(attempt / 'validation.json')
                if validation['errors']:
                    issues.append({'sample_id': sid, 'attempt': attempt.name, 'errors': validation['errors']})
            messages = load(attempt / 'messages.json')['messages']
            assert messages[:2] == messages_for(data, prompt, schema), 'blind_input_messages'
            assert len(messages) in (2, 4)
            if len(messages) == 4:
                index = attempts.index(attempt)
                previous = next((a for a in reversed(attempts[:index]) if (a / 'content.original.txt').exists()), None)
                assert previous is not None
                assert messages[2] == {'role': 'assistant', 'content': (previous / 'content.original.txt').read_text()}
                assert messages[3]['role'] == 'user'
                # In one invocation, empty response retains earlier content but supplies
                # the latest machine errors. Resume uses the last content+validation pair.
                error_sources = {previous, attempts[index - 1]}
                assert any(canonical({'errors': load(a / 'validation.json')['errors']}) in messages[3]['content']
                           for a in error_sources if (a / 'validation.json').exists())
            if redactor:
                assert redactor.clean(messages) == messages, 'secret_in_messages'
            for request_path in sorted(attempt.glob('http-*.request.json')):
                request = load(request_path)
                mode = load(request_path.with_name(request_path.name.replace('.request.json', '.started.json')))['mode']
                assert mode in MODES
                token_field = 'max_output_tokens' if protocol == 'responses' else 'max_tokens'
                expected_body = body_for(spec['model'], messages, schema, mode, request[token_field], protocol, spec['parameters'].get('responses_schema_compat', 'none'), spec['parameters'].get('stream', False))
                if spec['executor_version'] in ('scenario-executor-1.1.1', 'scenario-executor-1.1.2'):
                    expected_body.pop('store')
                else:
                    assert spec['parameters']['store'] is False
                assert request == expected_body, 'request_body_schema_binding'
                assert request[token_field] in (8192, 16384)
                if redactor:
                    assert redactor.clean(request) == request
                response_path = request_path.with_name(request_path.name.replace('.request.json', '.response.safe.json'))
                if not response_path.exists():
                    issues.append({'sample_id': sid, 'attempt': attempt.name, 'errors': ['incomplete_http_capture']})
                    continue
                response = load(response_path)
                body = effective_response(response)
                if spec['parameters'].get('stream') and response.get('http_status') == 200:
                    inspect_sse_capture(response)
                if response.get('http_status') == 200 and not response.get('error'):
                    validate_response_model(body, spec['model'])
                assert not response.get('redaction_applied', False)
                assert set(response.get('request_ids', {})).issubset({'x-request-id', 'request-id', 'x-oneapi-request-id', 'x-newapi-request-id'})
                http.append({'sample_id': sid, 'attempt': attempt.name, 'status': response['http_status'],
                             'mode': mode, 'request_ids': response.get('request_ids', {}),
                              'source_capture': str(response_path.resolve()), 'source_capture_sha256': sha(response_path),
                              'raw_sse_sha256': digest(response['eventmeta']['raw_events_text']) if 'eventmeta' in response else None,
                              'started_utc': load(request_path.with_name(request_path.name.replace('.request.json', '.started.json')))['utc'],
                             'response_model': body.get('model') if isinstance(body, dict) else None,
                             'duration_seconds': response.get('duration_seconds'),
                             'usage': usage_for(body, protocol), 'transport_error': response.get('error'),
                             'eventmeta': {k: v for k, v in response.get('eventmeta', {}).items() if k not in ('raw_events_text', 'events')},
                              'usage_original': body.get('usage') if isinstance(body, dict) else None})
        if state['status'] != 'success':
            # No attempt_dir lookup or invented canonical result on exhausted failure.
            samples.append({'sample_id': sid, 'structure': 'evaluation_failed', 'errors': state.get('errors', []),
                            'generation_count': len(attempts), 'format_repairs': state.get('format_repairs', 0),
                            'input_verbatim_from_sample_input': True})
            continue
        attempt = run / state['attempt_dir']
        assert attempt in attempts
        saved_state = load(attempt / 'status.json')
        assert saved_state['status'] == 'success'
        response = load(sorted(attempt.glob('http-*.response.safe.json'))[-1])
        assert response['http_status'] == 200 and not response.get('error')
        validate_response_model(effective_response(response), spec['model'])
        assert load(attempt / 'response.original-business.json') == response['body']
        if 'derived_response' in response:
            assert load(attempt / 'derived-response.json') == response['derived_response']
            assert load(attempt / 'assembly-manifest.json') == response['assembly_manifest']
        content, finish = parse_content(effective_response(response), protocol)
        assert finish in ('stop', None)
        assert (attempt / 'content.original.txt').read_text() == content
        candidate = content.strip()
        fence = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', candidate, re.S | re.I)
        wire = strict_json(fence.group(1).strip() if fence else candidate)
        assert wire == load(attempt / 'wire.json'), 'raw_wire_exact'
        legacy = validator.wire(data, wire, schema)
        assert legacy == load(attempt / 'legacy.json'), 'wire_legacy_exact'
        mapping = load(attempt / 'mapping.json')
        for label, value in [('wire', wire), ('legacy', legacy)]:
            h = digest(canonical(value))
            assert h == state[label + '_sha256'] == saved_state[label + '_sha256'] == mapping[label + '_sha256']
        assert mapping['business_fields_changed'] is False
        assert load(attempt / 'validation.json')['status'] == 'passed'
        assert saved_state['response_model'] == spec['model']
        accepted[sid] = legacy
        samples.append({'sample_id': sid, 'structure': 'passed', 'generation_count': len(attempts),
                        'format_repairs': state.get('format_repairs', 0),
                        'coverage': [r['coverage'] for r in legacy['baseline_judgments']],
                        'input_verbatim_from_sample_input': True})
    return {'spec': spec, 'report': report, 'samples': samples, 'http_attempts': http,
            'machine_errors_before_repair': issues, 'accepted': accepted,
            'technical_success_count': len(accepted), 'evaluation_failed_samples': [s['sample_id'] for s in samples if s['structure'] != 'passed']}


def audit(run, protected, smoke_exit, tests_exit, tests_log=None):
    run = Path(run)
    before = load(protected)
    changes = [path for path, expected in before.items() if not Path(path).is_file() or sha(path) != expected]
    spec = load(run / 'run.json')
    key, endpoint, _ = get_provider(provider=spec['provider'])
    if spec.get('api_protocol') == 'responses':
        endpoint = endpoint.removesuffix('/chat/completions') + '/responses'
    redactor = Redactor([key])
    spellings = {key, urllib.parse.quote(key, safe=''), json.dumps(key, ensure_ascii=False)[1:-1]}
    files = [p for p in run.rglob('*') if p.is_file()]
    logs = list((ROOT / 'tools/scenario_benchmark/.qa').glob('*.log'))
    echo_paths = [str(p.relative_to(ROOT)) for p in files + logs
                  if any(s.encode() in p.read_bytes() for s in spellings if s)]
    records = {s['sample_id']: s['input'] for s in load(ASSETS / 'calibration_samples.json')['samples']}
    inspected = inspect_run(run, records, redactor, endpoint)
    test_text = Path(tests_log or ROOT / 'tools/scenario_benchmark/.qa/v112-tests.stderr.log').read_text()
    match = re.search(r'Ran (\d+) tests', test_text)
    test_count = int(match.group(1)) if match else 0
    current_sources_changed = [p for p, h in spec['sources'].items() if not Path(p).is_file() or sha(p) != h]
    result = {k: v for k, v in inspected.items() if k not in ('spec', 'report', 'accepted')}
    result.update({'status': 'passed' if not changes and not echo_paths and smoke_exit == 0 and tests_exit == 0 and test_count else 'failed',
                   'safety_pass_is_not_evaluation_all_valid': True,
                   'smoke_exit_code': smoke_exit, 'test_exit_code': tests_exit, 'test_count': test_count,
                   'protected_file_count': len(before), 'protected_files_changed': changes,
                   'selected_credential_echo_paths': echo_paths, 'echo_scanned_file_count': len(files) + len(logs),
                   'current_source_fingerprint_changes': current_sources_changed,
                   'historical_schema_policy': 'saved schema and prompt; current source differences disclosed, not overwritten',
                   'http_count': len(inspected['http_attempts']), 'usage_all_attempts': inspected['report']['usage'],
                   'credential_saved_or_sent_to_model': False, 'headers_in_artifacts': False,
                   'input_message_audit': 'verbatim input only; no calibration wrappers', 'reference_triggered_retry': False,
                   'all_new_files_sha256': {str(p.relative_to(run)): sha(p) for p in files}})
    save(run / 'safety-audit-v1.1.2-final.json', result)
    print(canonical({k: result[k] for k in ('status', 'technical_success_count', 'evaluation_failed_samples', 'test_count', 'http_count', 'protected_files_changed', 'selected_credential_echo_paths')}))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--protected-before', type=Path, required=True)
    p.add_argument('--smoke-exit', type=int, required=True)
    p.add_argument('--tests-exit', type=int, required=True)
    p.add_argument('--tests-log', type=Path)
    a = p.parse_args()
    try:
        sys.exit(audit(a.run, a.protected_before, a.smoke_exit, a.tests_exit, a.tests_log))
    except Exception as e:
        print('audit_failed_' + type(e).__name__)
        sys.exit(1)
