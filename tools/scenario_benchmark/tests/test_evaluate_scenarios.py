"""Offline regression suite; every transport is mocked, never a real model API."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import evaluate_scenarios as ev
import safe_http as sh


def to_wire(result):
    result = copy.deepcopy(result)
    result['schema_version'] = 'wire1.1'
    for field, idfield in (('baseline_judgments', 'baseline_id'), ('generated_judgments', 'generated_id')):
        result[field] = {row[idfield]: row for row in result[field]}
    return result


def completion(wire, finish='stop', model='mock-model'):
    return {'http_status': 200, 'request_ids': {'x-request-id': 'mock-id'}, 'error': None,
            'duration_seconds': 0.01, 'body': {'model': model, 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30},
            'choices': [{'finish_reason': finish, 'message': {'role': 'assistant', 'content': json.dumps(wire, ensure_ascii=False) if not isinstance(wire, str) else wire}}]}}


class Fake:
    def __init__(self, responses, secret='mock-secret-not-real'):
        self.responses = list(responses)
        self.calls = []
        self.redactor = sh.Redactor([secret])

    def __call__(self, body):
        self.calls.append(copy.deepcopy(body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class EvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ev.Validator()
        cls.records = {s['sample_id']: s for s in ev.load(ev.ASSETS / 'calibration_samples.json')['samples']}
        cls.prompt = ev.PROMPT.read_text()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.data = copy.deepcopy(self.records['S001']['input'])
        self.wire = to_wire(self.records['S001']['expected_judgments'])
        self.harness_profile = self.output / 'mock-public-profile.yaml'
        self.harness_profile.write_text('- config:\n    providers:\n      new-api:\n        baseURL: https://example.invalid\n        apiKeyEnv: MOCK_REF\n')
        def public(**kwargs):
            kwargs['profile'] = kwargs.get('profile') or self.harness_profile
            return sh.provider_public(**kwargs)
        patcher = patch.object(ev, 'provider_public', side_effect=public)
        patcher.start()
        self.addCleanup(patcher.stop)

    def validate(self, wire=None, data=None):
        data = data or self.data
        return self.validator.wire(data, wire or self.wire, ev.wire_schema(data, self.validator.legacy_schema))

    def runner(self, responses, data=None, repairs=2, negotiate=True):
        transport = Fake(responses)
        runner = ev.Runner(self.output, self.validator, self.prompt, 'mock-model', repairs, transport, sleep=lambda _: None)
        result = runner.sample(data or self.data, negotiate=negotiate)
        return result, transport, runner

    def test_responses_api_projection_keeps_full_local_schema(self):
        schema = ev.wire_schema(self.data, self.validator.legacy_schema)
        original = copy.deepcopy(schema)
        projected = ev.api_schema(schema, 'omit-unique-items')
        self.assertEqual(schema, original)
        self.assertIn('"uniqueItems": true', json.dumps(schema))
        self.assertNotIn('uniqueItems', json.dumps(projected))
        body = ev.body_for('mock-model', [], schema, 'json_schema', protocol='responses', schema_compatibility='omit-unique-items')
        self.assertEqual(body['text']['format']['schema'], projected)
        self.assertEqual(ev.api_schema(schema), schema)

    def native_response(self, output=None, model='mock-model'):
        text = json.dumps(self.wire, ensure_ascii=False)
        body = {'model': model, 'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30},
                'output': output if output is not None else [{'type': 'reasoning', 'content': [{'type': 'output_text', 'text': 'IGNORE'}]},
                    {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text[:20]}, {'type': 'output_text', 'text': text[20:]}]}]}
        return {'http_status': 200, 'error': None, 'duration_seconds': 0.01, 'body': body}

    def test_responses_native_body_format_and_usage(self):
        for mode in ev.MODES:
            body = ev.body_for('mock-model', [], {}, mode, protocol='responses')
            self.assertIs(body['store'], False)
            self.assertIs(body['stream'], False)
            self.assertEqual(body['input'], [])
            self.assertEqual(body['max_output_tokens'], 8192)
            self.assertNotIn('response_format', body)
            self.assertNotIn('messages', body)
            if mode != 'prompt':
                self.assertEqual(body['text']['format']['type'], mode)
                self.assertNotIn('json_schema', body['text']['format'])
        native = self.native_response()['body']
        self.assertEqual(ev.usage_for(native, 'responses'), {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30})
        self.assertEqual(native['usage']['input_tokens'], 10)

    def test_responses_only_assistant_output_text_and_audit_resume(self):
        import audit_run
        args = self.cli_args(api_protocol='responses')
        fake = Fake([self.native_response()])
        fake.endpoint = 'https://example.invalid/v1/responses'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
            args.resume = True
            self.assertEqual(ev.run(args, fake), 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(json.loads(fake.calls[0]['input'][1]['content'].split('\n')[1]), self.data)
        self.assertEqual(ev.load(args.output_dir / 'run.json')['api_protocol'], 'responses')
        checked = audit_run.inspect_run(args.output_dir, {'S001': self.data})
        self.assertEqual(len(checked['accepted']), 1)
        self.assertEqual(checked['http_attempts'][0]['usage']['prompt_tokens'], 10)
        args.api_protocol = 'chat-completions'
        with self.assertRaisesRegex(sh.SafeFailure, 'resume_fingerprint_mismatch'):
            ev.run(args, fake)

    def test_responses_empty_and_no_assistant_rejected(self):
        for output in ([], [{'type': 'message', 'role': 'user', 'content': [{'type': 'output_text', 'text': 'x'}]}],
                       [{'type': 'function_call', 'text': 'x'}], [{'type': 'message', 'role': 'assistant', 'content': []}]):
            with self.assertRaisesRegex(ValueError, 'response_content_empty'):
                ev.parse_content(self.native_response(output)['body'], 'responses')

    def test_responses_wrong_model_no_repair(self):
        fake = Fake([self.native_response(model='wrong-model')])
        result = ev.Runner(self.output, self.validator, self.prompt, 'mock-model', transport=fake, protocol='responses').sample(self.data)
        self.assertEqual(result['errors'], ['response_model_mismatch'])
        self.assertEqual(len(fake.calls), 1)

    def test_first_protocol_400_stops_remaining_queue(self):
        args = self.cli_args(('S001', 'S002', 'S004'))
        fake = Fake([{'http_status': 400, 'error': None, 'body': {'error': {'message': 'Store must be set to false'}}}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 1)
        self.assertEqual(len(fake.calls), 1)
        report = ev.load(args.output_dir / 'session-001/report.json')
        self.assertEqual(len(report['final_failed']), 3)
        self.assertEqual(report['sample_status'][1]['errors'], ['cancelled_first_protocol_failure'])

    def test_store_false_body_and_spec_resume_guard(self):
        for mode in ev.MODES:
            self.assertIs(ev.body_for('mock-model', [], {}, mode)['store'], False)
        args = self.cli_args()
        fake = Fake([completion(self.wire)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
        spec_path = args.output_dir / 'run.json'
        spec = ev.load(spec_path)
        self.assertIs(spec['parameters']['store'], False)
        spec['parameters']['store'] = True
        spec_path.write_text(json.dumps(spec))
        args.resume = True
        with self.assertRaisesRegex(sh.SafeFailure, 'resume_fingerprint_mismatch'):
            ev.run(args, fake)
        self.assertEqual(len(fake.calls), 1)

    def test_all_40_reference_shapes_and_wire_mapping_offline(self):
        for sid, record in self.records.items():
            with self.subTest(sample=sid):
                data = record['input']
                schema = ev.wire_schema(data, self.validator.legacy_schema)
                wire = to_wire(record['expected_judgments'])
                self.assertEqual(self.validator.wire(data, wire, schema), record['expected_judgments'])
                for field, source in (('baseline_judgments', 'baseline'), ('generated_judgments', 'generated_scenarios')):
                    self.assertEqual(set(schema['properties'][field]['properties']), {r['id'] for r in data[source]})
                    self.assertEqual(set(schema['properties'][field]['required']), {r['id'] for r in data[source]})

    def test_resume_archived_prompt_hash_mismatch(self):
        result, fake, runner = self.runner([completion(self.wire)])
        (self.output / 'S001/prompt.md').write_text('tampered prompt')
        with self.assertRaises(sh.SafeFailure):
            runner.sample(self.data)
        self.assertEqual(len(fake.calls), 1)

    def test_cli_source_hash_mismatch_refuses_resume(self):
        source = self.output / 'dataset.json'
        ev.save(source, {'samples': [self.records['S001']]})
        args = ev.parser().parse_args(['calibrate', '--dataset', str(source), '--output-dir', str(self.output / 'run'), '--model', 'mock-model', '--execute'])
        fake = Fake([completion(self.wire)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
        source.write_text(source.read_text() + ' ')
        args.resume = True
        with self.assertRaises(sh.SafeFailure): ev.run(args, fake)
        self.assertEqual(len(fake.calls), 1)

    def test_cli_stale_lock_resume_and_live_lock_refusal(self):
        output = self.output / 'run'
        args = ev.parser().parse_args(['calibrate', '--dataset', str(ev.ASSETS / 'calibration_samples.json'), '--sample-ids', 'S001', '--output-dir', str(output), '--model', 'mock-model', '--execute'])
        fake = Fake([completion(self.wire)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
        args.resume = True
        lock = output / '.active.lock'; lock.write_text('123456789')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
        lock.write_text(str(os.getpid()))
        with self.assertRaises(sh.SafeFailure): ev.run(args, fake)
        self.assertEqual(len(fake.calls), 1)

    def test_explicit_env_harness_does_not_fallback_to_store(self):
        profile = self.output / 'profile.yaml'
        profile.write_text('- config:\n    providers:\n      new-api:\n        baseURL: https://example.invalid\n        apiKeyEnv: DEFAULT\n')
        with patch.dict(os.environ, {}, clear=True), patch.object(sh.yaml, 'compose', side_effect=AssertionError('must not read secrets')):
            with self.assertRaises(sh.SafeFailure):
                sh.get_provider(api_key_env='EXPLICIT', profile=profile)

    def cli_args(self, sample_ids=('S001',), **changes):
        args = ev.parser().parse_args(['calibrate', '--dataset', str(ev.ASSETS / 'calibration_samples.json'),
            '--sample-ids', *sample_ids, '--output-dir', str(self.output / 'run'), '--model', 'mock-model', '--execute'])
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    def test_profile_endpoint_drift_resume_refuses_before_auth_or_http(self):
        args = self.cli_args()
        fake = Fake([completion(self.wire)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
        args.resume = True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
        self.assertEqual(len(fake.calls), 1)
        self.harness_profile.write_text(self.harness_profile.read_text().replace('example.invalid', 'changed.invalid'))
        with patch.object(ev, 'get_provider', side_effect=AssertionError('must not read authentication')), self.assertRaisesRegex(sh.SafeFailure, 'resume_fingerprint_mismatch'):
            ev.run(args, fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(ev.load(self.output / 'run/run.json')['endpoint'], 'https://example.invalid/v1/chat/completions')

    def test_resume_changed_parameters_refused_without_http(self):
        args = self.cli_args()
        fake = Fake([completion(self.wire)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
        args.resume = True
        args.max_repairs = 1
        with self.assertRaisesRegex(sh.SafeFailure, 'resume_fingerprint_mismatch'):
            ev.run(args, fake)
        self.assertEqual(len(fake.calls), 1)

    def test_harness_dry_run_public_endpoint_without_credentials(self):
        args = self.cli_args(execute=False)
        with patch.object(ev, 'get_provider', side_effect=AssertionError('authentication forbidden')), patch.object(sh.yaml, 'compose', side_effect=AssertionError('credential store forbidden')), patch.object(sh.Transport, '__call__', side_effect=AssertionError('HTTP forbidden')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args), 0)
        spec = ev.load(self.output / 'run/run.json')
        self.assertEqual(spec['endpoint'], 'https://example.invalid/v1/chat/completions')
        self.assertEqual(spec['provider_public']['credential_ref_name'], 'MOCK_REF')

    def test_response_model_mismatch_and_missing_are_technical_not_repair(self):
        for model in ('wrong-model', None, 12):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as temp:
                response = completion(self.wire, model=model)
                if model is None:
                    del response['body']['model']
                fake = Fake([response])
                runner = ev.Runner(Path(temp), self.validator, self.prompt, 'mock-model', 2, fake, sleep=lambda _: None)
                result = runner.sample(self.data)
                self.assertEqual(result['status'], 'evaluation_failed')
                self.assertTrue(result['errors'][0].startswith('response_model_'))
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(result['format_repairs'], 0)
                self.assertFalse((Path(temp) / 'S001/attempt-001/legacy.json').exists())
                self.assertEqual(ev.load(Path(temp) / 'S001/attempt-001/response.original-business.json'), response['body'])

    def test_resume_checks_provider_response_model_not_only_status(self):
        result, fake, runner = self.runner([completion(self.wire)])
        path = self.output / result['attempt_dir'] / 'http-01.response.safe.json'
        response = ev.load(path)
        response['body']['model'] = 'wrong-model'
        path.write_text(json.dumps(response))
        with self.assertRaisesRegex(sh.SafeFailure, 'response_model_mismatch'):
            runner.sample(self.data)
        self.assertEqual(len(fake.calls), 1)

    def test_explicit_base_cli_routes_bound_transport_and_resume_no_http(self):
        args = self.cli_args(base_url='https://explicit.invalid/prefix/v1/', api_key_env='EXPLICIT')
        fake = Fake([completion(self.wire)])
        fake.endpoint = 'https://explicit.invalid/prefix/v1/chat/completions'
        with patch.dict(os.environ, {'EXPLICIT': 'mock-key-value'}), patch.object(ev, 'Transport', return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args), 0)
            args.resume = True
            self.assertEqual(ev.run(args), 0)
        self.assertEqual(len(fake.calls), 1)
        spec = ev.load(self.output / 'run/run.json')
        self.assertEqual(spec['endpoint'], fake.endpoint)
        self.assertNotIn('mock-key-value', ev.canonical(spec))

    def test_auth_resolution_endpoint_drift_refuses_without_http(self):
        args = self.cli_args()
        fake = Fake([completion(self.wire)])
        with patch.object(ev, 'get_provider', return_value=('mock-key-value', 'https://changed.invalid/v1/chat/completions', {})), patch.object(ev, 'Transport', return_value=fake), self.assertRaisesRegex(sh.SafeFailure, 'provider_endpoint_changed_after_preflight'):
            ev.run(args)
        self.assertEqual(fake.calls, [])

    def test_later_worker_401_cancels_not_started_queue(self):
        args = self.cli_args(('S001', 'S002', 'S004'), workers=1)
        fake = Fake([completion(self.wire), {'http_status': 401, 'error': None, 'body': {'error': {'message': 'unauthorized'}}}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 1)
        self.assertEqual(len(fake.calls), 2)
        report = ev.load(self.output / 'run/session-001/report.json')
        states = {item['sample_id']: item for item in report['sample_status']}
        self.assertEqual(states['S002']['errors'], ['http_401'])
        self.assertEqual(states['S004']['errors'], ['cancelled_auth_failure'])
        self.assertEqual(report['coverage']['rows_compared'], 1)
        self.assertFalse(report['technical_failure_as_missing'])

    def test_counts_placeholder_unknown_ids_rejected(self):
        for name in ('missing', 'placeholder', 'unknown', 'wrong_const', 'array_duplicate'):
            wire = copy.deepcopy(self.wire)
            rows = wire['baseline_judgments']
            if name == 'missing':
                rows.clear()
            elif name == 'placeholder':
                rows['AUC-002'] = {'baseline_id_placeholder_note': None}
            elif name == 'unknown':
                rows['AUC-999'] = copy.deepcopy(rows['AUC-002'])
            elif name == 'wrong_const':
                rows['AUC-002']['baseline_id'] = 'AUC-999'
            else:
                wire['baseline_judgments'] = list(rows.values()) * 2
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.validate(wire)

    def test_json_duplicate_nan_nonobject_rejected(self):
        for text in ('{"id":"x","id":"y"}', '{"x":NaN}', '[]', 'null', '{"x":Infinity}'):
            with self.assertRaises(ValueError):
                ev.strict_json(text)

    def test_empty_candidates_valid_explicit_missing(self):
        data = copy.deepcopy(self.data)
        data['generated_scenarios'] = []
        wire = copy.deepcopy(self.wire)
        wire['generated_judgments'] = {}
        row = wire['baseline_judgments']['AUC-002']
        row.update(coverage='missing', matched_generated_ids=[], evidence=[], covered_aspects=[], missing_aspects=['无候选'])
        result = self.validate(wire, data)
        self.assertEqual(result['generated_judgments'], [])

    def test_nonconflict_candidate_source_refs_rejected_without_label_repair(self):
        for consistency in ('consistent', 'uncertain', 'not_assessed'):
            wire = copy.deepcopy(self.wire)
            row = wire['generated_judgments']['S001-G01']
            row['business_consistency'] = consistency
            row['source_refs'] = [{'source_id': 'generated:S001-G01:text',
                                   'quote': self.data['generated_scenarios'][0]['text'], 'explanation': 'mock'}]
            with self.subTest(consistency=consistency), self.assertRaisesRegex(ValueError, 'wire_anyOf'):
                self.validate(wire)

    def test_internal_conflict_two_real_fragments_remains_legal(self):
        data = copy.deepcopy(self.data)
        candidate = data['generated_scenarios'][0]
        candidate['text'] += '同一条件撤单必定成功；同一条件撤单必定失败。'
        wire = copy.deepcopy(self.wire)
        refs = [{'source_id': 'generated:S001-G01:text', 'quote': q, 'explanation': '同一情境矛盾'}
                for q in ('同一条件撤单必定成功', '同一条件撤单必定失败')]
        for field, ident, target in (('baseline_judgments', 'AUC-002', 'baseline'),
                                     ('generated_judgments', 'S001-G01', 'generated')):
            wire[field][ident].update(business_consistency='conflict', source_refs=copy.deepcopy(refs))
            wire['review_queue'].append({'target_type': target, 'target_id': ident,
                                         'issue_type': 'business_consistency', 'reason': 'mock'})
        self.validate(wire, data)
        wire['generated_judgments']['S001-G01']['source_refs'] = refs[:1]
        with self.assertRaisesRegex(ValueError, 'two distinct'):
            self.validate(wire, data)

    def test_unknown_external_source_rejected_and_scope_not_expanded(self):
        wire = copy.deepcopy(self.wire)
        wire['baseline_judgments']['AUC-002']['source_refs'] = [
            {'source_id': 'baseline:AUC-999:notes', 'quote': 'mock', 'explanation': 'mock'}]
        with self.assertRaisesRegex(ValueError, 'wire_anyOf'):
            self.validate(wire)
        schema = ev.wire_schema(self.data, self.validator.legacy_schema)
        branches = schema['properties']['baseline_judgments']['properties']['AUC-002']['anyOf']
        for branch in branches:
            self.assertFalse(branch['additionalProperties'])
            self.assertEqual(set(branch['required']), set(branch['properties']))
            allowed = branch['properties']['source_refs']['items']['properties']['source_id']['enum']
            self.assertNotIn('baseline:AUC-003:description', allowed)

    def test_empty_source_enum_uses_maxitems_zero(self):
        data = {'input_id': 'EMPTY', 'baseline': [], 'generated_scenarios': [
                {'id': 'ONLY', 'text': 'mock'}], 'reference_material': []}
        schema = ev.wire_schema(data, self.validator.legacy_schema)
        nonconflict = schema['properties']['generated_judgments']['properties']['ONLY']['anyOf'][0]
        self.assertEqual(nonconflict['properties']['source_refs']['maxItems'], 0)
        self.assertNotIn('"enum": []', json.dumps(schema))
        empty = copy.deepcopy(self.data); empty['generated_scenarios'] = []
        schema = ev.wire_schema(empty, self.validator.legacy_schema)
        self.assertEqual(schema['properties']['generated_judgments']['required'], [])

    def test_auditor_budget_exhausted_failure_has_no_canonical(self):
        import audit_run
        args = self.cli_args()
        broken = copy.deepcopy(self.wire)
        broken['baseline_judgments']['AUC-002']['source_refs'] = [
            {'source_id': 'baseline:AUC-002:description', 'quote': '伪造规则', 'explanation': 'mock'}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, Fake([completion(broken)] * 3)), 1)
        result = audit_run.inspect_run(args.output_dir, {'S001': self.data})
        self.assertEqual(result['evaluation_failed_samples'], ['S001'])
        self.assertEqual(result['accepted'], {})
        self.assertEqual(len(result['http_attempts']), 3)
        self.assertEqual(len(result['machine_errors_before_repair']), 3)
        self.assertFalse(any(args.output_dir.glob('S001/attempt-*/legacy.json')))

    def test_auditor_empty_response_safety_pass_not_all_valid(self):
        import audit_run
        args = self.cli_args()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, Fake([completion('')])), 1)
        protected = self.output / 'protected.json'; ev.save(protected, {})
        log = self.output / 'tests.log'; log.write_text('Ran 1 tests\nOK\n')
        with patch.object(audit_run, 'get_provider', return_value=(
                'mock-secret-not-real', 'https://example.invalid/v1/chat/completions', {})), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit_run.audit(args.output_dir, protected, 0, 0, log), 0)
        result = ev.load(args.output_dir / 'safety-audit-v1.1.2-final.json')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['technical_success_count'], 0)
        self.assertEqual(result['evaluation_failed_samples'], ['S001'])
        self.assertTrue(result['safety_pass_is_not_evaluation_all_valid'])

    def test_auditor_prior_content_with_latest_empty_response_error(self):
        import audit_run
        args = self.cli_args()
        broken = copy.deepcopy(self.wire)
        broken['baseline_judgments']['AUC-002']['evidence'][0]['quote'] = '伪造文本'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, Fake([completion(broken), completion(''), completion(self.wire)])), 0)
        result = audit_run.inspect_run(args.output_dir, {'S001': self.data})
        self.assertEqual(result['technical_success_count'], 1)
        self.assertEqual(len(result['http_attempts']), 3)
        self.assertEqual(len(result['machine_errors_before_repair']), 2)

    def test_multi_parent_multi_generated_joint_and_partial(self):
        for sid in ('S002', 'S004', 'S005', 'S018'):
            record = self.records[sid]
            self.validate(to_wire(record['expected_judgments']), record['input'])
        # Explicit many-to-many structural case, no semantic claim.
        data = copy.deepcopy(self.data)
        other = copy.deepcopy(self.validator.gold_rows['AUC-003'])
        data['baseline'].append(other)
        data['task_scope']['baseline_ids'].append(other['id'])
        candidate = copy.deepcopy(data['generated_scenarios'][0]); candidate['id'] = 'SECOND'
        data['generated_scenarios'].append(candidate)
        wire = copy.deepcopy(self.wire)
        first = wire['baseline_judgments']['AUC-002']
        first['matched_generated_ids'].append('SECOND')
        first['evidence'].append({'generated_id': 'SECOND', 'quote': candidate['text']})
        second = copy.deepcopy(first); second['baseline_id'] = 'AUC-003'
        wire['baseline_judgments']['AUC-003'] = second
        for row in wire['generated_judgments'].values():
            row['matched_baseline_ids'] = ['AUC-002', 'AUC-003']
        row = copy.deepcopy(next(iter(wire['generated_judgments'].values())))
        row.update(generated_id='SECOND', evidence=[{'generated_id': 'SECOND', 'quote': candidate['text']}])
        wire['generated_judgments']['SECOND'] = row
        self.validate(wire, data)

    def test_semantic_business_differences_do_not_retry(self):
        wire = copy.deepcopy(self.wire)
        wire['baseline_judgments']['AUC-002']['business_consistency'] = 'consistent'
        wire['generated_judgments']['S001-G01']['business_consistency'] = 'consistent'
        result, fake, _ = self.runner([completion(wire)])
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(fake.calls), 1)
        report = ev.aggregate(self.output, [result], ev.ASSETS / 'calibration_samples.json')
        self.assertEqual(len(report['differences']), 2)
        self.assertTrue(report['business_difference_flag'])
        self.assertEqual(report['coverage']['rows_equal'], 1)

    def test_fake_quote_repaired_original_preserved(self):
        broken = copy.deepcopy(self.wire)
        broken['baseline_judgments']['AUC-002']['evidence'][0]['quote'] = '伪造文本'
        result, fake, _ = self.runner([completion(broken), completion(self.wire)])
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['format_repairs'], 1)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn('伪造文本', (self.output / 'S001/attempt-001/content.original.txt').read_text())
        repair = fake.calls[1]['messages']
        self.assertEqual(json.loads(repair[1]['content'].split('\n')[1]), self.data)
        self.assertIn('Evidence quote is not original text', repair[-1]['content'])

    def test_fake_source_ref_repair_and_cap_exhaustion(self):
        broken = copy.deepcopy(self.wire)
        broken['baseline_judgments']['AUC-002']['source_refs'] = [{'source_id': 'baseline:AUC-002:description', 'quote': '伪造规则', 'explanation': 'mock'}]
        result, fake, _ = self.runner([completion(broken)] * 3)
        self.assertEqual(result['status'], 'evaluation_failed')
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(len(list(self.output.glob('S001/attempt-*'))), 3)
        report = ev.aggregate(self.output, [result], ev.ASSETS / 'calibration_samples.json')
        self.assertEqual(report['coverage']['rows_compared'], 0)
        self.assertFalse(report['technical_failure_as_missing'])

    def test_invalid_json_and_links_repair_success(self):
        broken = copy.deepcopy(self.wire)
        broken['generated_judgments']['S001-G01']['matched_baseline_ids'] = []
        result, fake, _ = self.runner([completion('NOT_JSON'), completion(broken), completion(self.wire)])
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['format_repairs'], 2)
        self.assertEqual(len(fake.calls), 3)

    def test_unsupported_fallback_bounded_and_logged(self):
        error = {'http_status': 400, 'error': None, 'body': {'error': {'message': 'response_format json_schema unsupported'}}}
        other = {'http_status': 422, 'error': None, 'body': {'error': {'message': 'json_object not supported'}}}
        result, fake, runner = self.runner([error, other, completion(self.wire)])
        self.assertEqual(result['status'], 'success')
        self.assertEqual(runner.mode, 'prompt')
        self.assertEqual(len(result['compatibility_changes']), 2)
        self.assertEqual(len(fake.calls), 3)
        self.assertNotIn('response_format', fake.calls[-1])
        self.assertEqual(result['format_repairs'], 0)

    def test_auth_limits_server_errors_not_capability_fallback(self):
        for code, calls in ((401, 1), (403, 1), (429, 3), (500, 3)):
            responses = [{'http_status': code, 'error': None, 'body': {'error': {'message': 'json_schema unsupported'}}}] * calls
            with tempfile.TemporaryDirectory() as tmp:
                fake = Fake(responses)
                result = ev.Runner(Path(tmp), self.validator, self.prompt, 'mock-model', 2, fake, sleep=lambda _: None).sample(self.data, True)
                self.assertEqual(result['status'], 'evaluation_failed')
                self.assertEqual(len(fake.calls), calls)
                self.assertEqual(result['mode'], 'json_schema')
                self.assertFalse(result['compatibility_changes'])

    def test_transport_timeout_retries_bounded(self):
        timeout = {'http_status': None, 'body': None, 'error': 'transport_TimeoutError'}
        result, fake, _ = self.runner([timeout, timeout, completion(self.wire)])
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['transport_retries'], 2)
        self.assertEqual(len(fake.calls), 3)

    def test_finish_length_one_budget_increase(self):
        result, fake, _ = self.runner([completion(self.wire, 'length'), completion(self.wire)])
        self.assertEqual(result['status'], 'success')
        self.assertEqual([b['max_tokens'] for b in fake.calls], [8192, 16384])

    def test_resume_runner_skips_revalidates_and_refuses_tampering(self):
        result, fake, runner = self.runner([completion(self.wire)])
        resumed = runner.sample(self.data)
        self.assertTrue(resumed['skipped'])
        self.assertEqual(len(fake.calls), 1)
        path = self.output / result['attempt_dir'] / 'legacy.json'
        value = ev.load(path); value['limitations'] = ['tampered']
        path.write_text(json.dumps(value))
        with self.assertRaises(sh.SafeFailure):
            runner.sample(self.data)

    def test_failed_resume_adds_new_attempt_without_overwrite(self):
        broken = copy.deepcopy(self.wire); broken['baseline_judgments'].clear()
        fake = Fake([completion(broken), sh.SafeFailure('mock_interrupt')])
        runner = ev.Runner(self.output, self.validator, self.prompt, 'mock-model', 2, fake, sleep=lambda _: None)
        result = runner.sample(self.data)
        self.assertEqual(result['status'], 'evaluation_failed')
        before = ev.sha(self.output / 'S001/attempt-001/content.original.txt')
        fake.responses.append(completion(self.wire))
        result = runner.sample(self.data)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['generation'], 3)
        self.assertEqual(before, ev.sha(self.output / 'S001/attempt-001/content.original.txt'))

    def test_cli_resume_fingerprint_and_exclusive_guard(self):
        output = self.output / 'run'
        argv = ['calibrate', '--dataset', str(ev.ASSETS / 'calibration_samples.json'), '--sample-ids', 'S001', '--output-dir', str(output), '--model', 'mock-model', '--execute']
        args = ev.parser().parse_args(argv)
        fake = Fake([completion(self.wire)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args, fake), 0)
            args.resume = True
            self.assertEqual(ev.run(args, fake), 0)
        self.assertEqual(len(fake.calls), 1)
        args.model = 'changed-model'
        with self.assertRaises(sh.SafeFailure): ev.run(args, fake)
        args.resume = False
        with self.assertRaises(sh.SafeFailure): ev.run(args, fake)

    def test_wrapper_injection_not_in_messages(self):
        data = copy.deepcopy(self.records['S012']['input'])
        wire = to_wire(self.records['S012']['expected_judgments'])
        result, fake, _ = self.runner([completion(wire)], data)
        self.assertEqual(result['status'], 'success')
        original = fake.calls[0]['messages'][1]['content']
        self.assertNotIn('expected_judgments', original)
        self.assertNotIn('"tags":', original)
        self.assertNotIn('"derivation":', original)
        self.assertEqual(json.loads(original.split('\n')[1]), data)
        with self.assertRaises(sh.SafeFailure):
            self.validator.input({**data, 'expected_judgments': {}})

    def test_dry_run_no_auth_or_http(self):
        args = ev.parser().parse_args(['calibrate', '--dataset', str(ev.ASSETS / 'calibration_samples.json'), '--sample-ids', 'S001', '--output-dir', str(self.output / 'dry'), '--model', 'mock-model'])
        with patch.object(ev, 'get_provider', side_effect=AssertionError('must not authenticate')), patch.object(sh.Transport, '__call__', side_effect=AssertionError('no HTTP')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ev.run(args), 0)
        report = ev.load(self.output / 'dry/session-001/report.json')
        self.assertEqual(report['status'], 'dry_run')
        self.assertEqual(report['http_count'], 0)

    def test_secret_echo_safe_artifacts_and_no_stdout(self):
        secret = 'mock-secret-not-real'
        echo = completion(self.wire)
        echo['body']['debug'] = secret
        result, fake, _ = self.runner([echo])
        self.assertEqual(result['status'], 'evaluation_failed')
        self.assertEqual(result['errors'], ['credential_echo_redacted'])
        for p in self.output.rglob('*'):
            if p.is_file():
                self.assertNotIn(secret, p.read_text())
                self.assertNotIn('"Authorization":', p.read_text())

    def test_pipeline_snapshot_stage_no_generation(self):
        snapshot = self.output / 'scenarios.json'
        ev.save(snapshot, {'source_task_id': 123, 'scenarios': self.data['generated_scenarios']})
        data = ev.assemble_snapshot(snapshot, 'TASK-AUC', ev.GOLD, ev.ASSETS / 'tasks.json')
        self.validator.input(data)
        self.assertEqual(len(data['baseline']), 19)
        self.assertEqual(data['generation_provenance']['human_edit_status'], 'unknown')
        self.assertNotIn('generator_request', data)

    def test_explicit_env_no_credentials_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sh.yaml, 'compose', side_effect=AssertionError('must not read secrets')):
            with self.assertRaises(sh.SafeFailure):
                sh.get_provider(base_url='https://example.invalid', api_key_env='EXPLICIT')
        with patch.dict(os.environ, {'EXPLICIT': 'mock-key-value'}):
            key, endpoint, _ = sh.get_provider(base_url='https://example.invalid/v1', api_key_env='EXPLICIT')
            self.assertEqual(key, 'mock-key-value')
            self.assertEqual(endpoint, 'https://example.invalid/v1/chat/completions')

    def test_redirect_refused_and_https_required(self):
        self.assertIsNone(sh.NoRedirect().redirect_request(None, None, 302, None, None, 'https://other.invalid'))
        for base in ('http://example.invalid', 'https://u:p@example.invalid', 'https://example.invalid?a=1', 'https://example.invalid/../x'):
            with self.assertRaises(sh.SafeFailure): sh.api_url(base)

    def test_transport_bounded_no_headers_archive(self):
        class Response:
            status = 200
            headers = {'x-request-id': 'safe-id', 'authorization': 'never-save'}
            def __init__(self, raw): self.raw = raw
            def read(self, size): out, self.raw = self.raw[:size], self.raw[size:]; return out
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Opener:
            def __init__(self, raw): self.raw = raw; self.req = None
            def open(self, req, timeout): self.req = req; return Response(self.raw)
        opener = Opener(b'x' * (sh.LIMIT + 1))
        transport = sh.Transport('mock-key-value', 'https://example.invalid/v1/chat/completions')
        with patch.object(sh.urllib.request, 'build_opener', return_value=opener):
            result = transport({'model': 'mock', 'messages': []})
        self.assertEqual(result['error'], 'body_limit_exceeded')
        self.assertLessEqual(result['captured_bytes'], sh.LIMIT)
        self.assertNotIn('authorization', result['request_ids'])
        self.assertEqual(opener.req.get_header('Authorization'), 'Bearer mock-key-value')


if __name__ == '__main__':
    unittest.main()
