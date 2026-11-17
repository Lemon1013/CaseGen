"""Offline mock regressions only: no credential reads and no real model calls."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_scenarios as ev
import evaluate_project_scenarios as project
import fetch_scenarios as fetch
from safe_http import SafeFailure, Redactor, ResponsesSSE, inspect_sse_capture


def candidates(n):
    return [{'id': f'TASK{10 + i % 6}-POINT{i:03d}', 'category': None,
             'title': '真实测试意图', 'description': '', 'test_focus': '',
             'text': '真实测试意图\n甲；乙', 'source_task_id': 10 + i % 6} for i in range(n)]


def manifest():
    return {'project_id': 3, 'space_id': 2, 'status': 'complete',
            'tasks': [{'category': c, 'task_id': i + 10} for i, c in enumerate(project.CATEGORIES)]}


def wire_for(data):
    """scoped1.1: the wire carries exactly one stage judgment map and no non-stage field."""
    result = {'schema_version': project.SCHEMA_VERSION, 'input_id': data['context']['input_id'],
        'request_id': data['request_id'], 'evaluation_status': 'complete',
        'duplicate_groups': [], 'review_queue': [], 'limitations': []}
    if data['stage'] == 'A':
        result['baseline_judgments'] = {}
        for bid in data['targets']:
            result['baseline_judgments'][bid] = {'baseline_id': bid, 'coverage': 'missing',
                'business_consistency': 'not_assessed', 'matched_generated_ids': [], 'evidence': [],
                'covered_aspects': [], 'missing_aspects': ['未表达主意图'], 'source_refs': [], 'reason': 'mock结构测试'}
    else:
        result['generated_judgments'] = {}
        for gid in data['targets']:
            links = data['a_mapping']['candidate_to_baseline'][gid]
            result['generated_judgments'][gid] = {'generated_id': gid,
                'relation': 'matched' if links else 'new', 'matched_baseline_ids': links,
                'granularity': 'appropriate', 'business_consistency': 'not_assessed',
                'evidence': [{'generated_id': gid, 'quote': '真实测试意图'}], 'source_refs': [], 'reason': 'mock完整117范围待核查'}
            if not links:
                result['review_queue'].append({'target_type': 'generated', 'target_id': gid,
                    'issue_type': 'novelty', 'reason': '完整117范围下业务新增仍需核查'})
    return result


def sse_text(events):
    return ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                   for event in events)


def capture_from_events(events, http_status=200, error=None):
    raw = sse_text(events)
    parser = ResponsesSSE()
    parser.feed(raw.encode())
    parse_error = None
    try:
        parser.finish()
    except SafeFailure as exc:
        parse_error = str(exc)
    result = {'http_status': http_status, 'error': error or parse_error, 'body': parser.response,
              'eventmeta': {'protocol': 'responses-sse', 'raw_events_text': raw, 'events': parser.events,
                            'completed_count': parser.completed_count, 'terminal_error': parser.terminal_error}}
    if parser.derived_response is not None:
        result['derived_response'] = parser.derived_response
        result['assembly_manifest'] = parser.assembly_manifest
    return result


def success_events(model, text, usage=None):
    """Full realistic lifecycle: created/in_progress/item/part/deltas/done/completed."""
    rid, item_id = 'resp_mock', 'msg_mock'
    events = [
        {'type': 'response.created', 'response': {'id': rid, 'object': 'response', 'model': model,
            'status': 'in_progress', 'error': None, 'incomplete_details': None, 'output': []}},
        {'type': 'response.in_progress', 'response': {'id': rid, 'model': model, 'status': 'in_progress'}},
        {'type': 'response.output_item.added', 'output_index': 0,
         'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}},
        {'type': 'response.content_part.added', 'output_index': 0, 'content_index': 0, 'item_id': item_id,
         'part': {'type': 'output_text', 'text': ''}}]
    for start in range(0, max(len(text), 1), 40):
        events.append({'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0,
                       'item_id': item_id, 'delta': text[start:start + 40]})
    message = {'id': item_id, 'type': 'message', 'role': 'assistant', 'status': 'completed',
               'content': [{'type': 'output_text', 'text': text}]}
    events += [
        {'type': 'response.output_text.done', 'output_index': 0, 'content_index': 0, 'item_id': item_id, 'text': text},
        {'type': 'response.content_part.done', 'output_index': 0, 'content_index': 0, 'item_id': item_id,
         'part': {'type': 'output_text', 'text': text}},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
        {'type': 'response.completed', 'response': {'id': rid, 'object': 'response', 'model': model,
            'status': 'completed', 'error': None, 'incomplete_details': None, 'output': [message],
            'usage': usage or {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}}]
    for index, event in enumerate(events):
        event['sequence_number'] = index
    return events


def capture(wire, model=project.DEFAULT_MODEL, incomplete=False):
    text = ev.canonical(wire)
    if incomplete:
        events = [event for event in success_events(model, text) if event['type'] != 'response.completed']
        events.append({'type': 'response.incomplete', 'sequence_number': len(events),
            'response': {'id': 'resp_mock', 'object': 'response', 'model': model, 'status': 'incomplete',
                         'error': None, 'incomplete_details': {'reason': 'max_output_tokens'},
                         'output': [{'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                                     'content': [{'type': 'output_text', 'text': text}]}]}})
        return capture_from_events(events)
    return capture_from_events(success_events(model, text))


def completed_only_capture(wire, model=project.DEFAULT_MODEL):
    """Simplified capture with non-empty completed output and no item/part/delta lifecycle."""
    text = ev.canonical(wire)
    rid = 'resp_completed_only'
    output = [{'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'status': 'completed',
               'content': [{'type': 'output_text', 'text': text}]}]
    events = [
        {'type': 'response.created', 'sequence_number': 0,
         'response': {'id': rid, 'model': model, 'status': 'in_progress', 'error': None,
                      'incomplete_details': None, 'output': []}},
        {'type': 'response.completed', 'sequence_number': 1,
         'response': {'id': rid, 'object': 'response', 'model': model, 'status': 'completed', 'error': None,
                      'incomplete_details': None, 'output': output,
                      'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}}]
    return capture_from_events(events)


def renumber(events):
    for index, event in enumerate(events):
        event['sequence_number'] = index
    return events


def two_channel_events(text, model=project.DEFAULT_MODEL, reasoning='先分析：需要结构化输出。', usage=None):
    """Real deepseek-flash two-channel lifecycle: reasoning item, then the assistant message."""
    rid, ritem, mitem = 'resp_two_channel', 'rsn_mock', 'msg_mock'
    events = [
        {'type': 'response.created', 'response': {'id': rid, 'object': 'response', 'model': model,
            'status': 'in_progress', 'error': None, 'incomplete_details': None, 'output': []}},
        {'type': 'response.in_progress', 'response': {'id': rid, 'model': model, 'status': 'in_progress'}},
        {'type': 'response.output_item.added', 'output_index': 0,
         'item': {'id': ritem, 'type': 'reasoning', 'status': 'in_progress', 'content': [], 'summary': []}},
        {'type': 'response.content_part.added', 'output_index': 0, 'content_index': 0, 'item_id': ritem,
         'part': {'type': 'reasoning_text', 'text': ''}}]
    for start in range(0, max(len(reasoning), 1), 6):
        events.append({'type': 'response.reasoning_text.delta', 'output_index': 0, 'content_index': 0,
                       'item_id': ritem, 'delta': reasoning[start:start + 6]})
    reasoning_item = {'id': ritem, 'type': 'reasoning', 'status': 'completed', 'summary': [],
                      'content': [{'type': 'reasoning_text', 'text': reasoning}]}
    events += [
        {'type': 'response.reasoning_text.done', 'output_index': 0, 'content_index': 0, 'item_id': ritem,
         'text': reasoning},
        {'type': 'response.content_part.done', 'output_index': 0, 'content_index': 0, 'item_id': ritem,
         'part': {'type': 'reasoning_text', 'text': reasoning}},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': reasoning_item},
        {'type': 'response.output_item.added', 'output_index': 1,
         'item': {'id': mitem, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}},
        {'type': 'response.content_part.added', 'output_index': 1, 'content_index': 0, 'item_id': mitem,
         'part': {'type': 'output_text', 'text': ''}}]
    for start in range(0, max(len(text), 1), 40):
        events.append({'type': 'response.output_text.delta', 'output_index': 1, 'content_index': 0,
                       'item_id': mitem, 'delta': text[start:start + 40]})
    message = {'id': mitem, 'type': 'message', 'role': 'assistant', 'status': 'completed',
               'content': [{'type': 'output_text', 'text': text}]}
    events += [
        {'type': 'response.output_text.done', 'output_index': 1, 'content_index': 0, 'item_id': mitem, 'text': text},
        {'type': 'response.content_part.done', 'output_index': 1, 'content_index': 0, 'item_id': mitem,
         'part': {'type': 'output_text', 'text': text}},
        {'type': 'response.output_item.done', 'output_index': 1, 'item': message},
        {'type': 'response.completed', 'response': {'id': rid, 'object': 'response', 'model': model,
            'status': 'completed', 'error': None, 'incomplete_details': None, 'output': [reasoning_item, message],
            'usage': usage or {'input_tokens': 120, 'output_tokens': 30, 'total_tokens': 150,
                               'output_tokens_details': {'reasoning_tokens': 12}}}}]
    return renumber(events)


def reasoning_capture(wire, model=project.DEFAULT_MODEL, reasoning='先分析：需要结构化输出。'):
    """Exactly what the frozen Transport archives for a deepseek-flash reasoning-channel answer."""
    return capture_from_events(two_channel_events(ev.canonical(wire), model, reasoning))


class MockTransport:
    def __init__(self, endpoint='https://example.invalid/v1/responses', fail=None, model=None):
        self.endpoint = endpoint
        self.redactor = Redactor()
        self.calls = []
        self.fail = fail
        self.model = model or project.DEFAULT_MODEL

    def __call__(self, body):
        self.calls.append(body)
        data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
        if self.fail == '401':
            return {'http_status': 401, 'body': {}, 'error': None}
        if self.fail == 'no-lifecycle':
            return completed_only_capture(wire_for(data), model=self.model)
        if self.fail == 'reasoning':
            return reasoning_capture(wire_for(data), model=self.model)
        return capture(wire_for(data), model='wrong-model' if self.fail == 'model' else self.model,
                       incomplete=self.fail == 'incomplete')


class ProbeTransport:
    """Minimal probe transport: echoes the requested model, or fails / wrong-echoes."""

    def __init__(self, mode='echo', endpoint='https://example.invalid/v1/responses'):
        self.mode = mode
        self.endpoint = endpoint
        self.redactor = Redactor()
        self.calls = []

    def __call__(self, body):
        self.calls.append(body)
        if self.mode == 'fail-first' and len(self.calls) == 1:
            return {'http_status': 400, 'body': {'error': {'message': 'model not found'}}, 'error': None}
        return capture({'ok': True},
                       model='some-other-model' if self.mode == 'wrong' else body['model'])


class ScopedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = ev.Validator()
        cls.context = project.full_context(candidates(7), {'provenance': 'mock'})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def prepared(self, n=7, size=5, model=None):
        scenes = self.root / 'scenarios.json'
        source = self.root / 'manifest.json'
        ev.save(scenes, {'scenarios': candidates(n)})
        ev.save(source, manifest())
        output = self.root / 'assessment'
        project.prepare(scenes, source, output, size, 'https://example.invalid', 'MOCK_SELECTED_KEY', model=model)
        return output

    def data_A(self):
        return project.request_data(self.context, project.target_plan(self.context)[0])

    def accepted_A(self, context=None):
        context = context or self.context
        return [{'wire': wire_for(project.request_data(context, t)),
                 'origin': {'stage': 'A', 'request_id': t['request_id'], 'attempt': 'attempt-001'}}
                for t in project.target_plan(context) if t['stage'] == 'A']

    def data_B(self):
        a = self.accepted_A()
        mapping = project.inverse_mapping(self.context, a)
        return project.request_data(self.context, next(t for t in project.target_plan(self.context) if t['stage'] == 'B'), mapping)

    def valid(self, data, wire=None):
        schema = project.scoped_schema(data, self.validator.legacy_schema)
        return project.validate_scoped(data, wire or wire_for(data), schema, self.validator)

    def test_N0_rejected(self):
        with self.assertRaises(SafeFailure):
            project.full_context([], {})

    def test_N1_N7_N30_all117_exact_once_and_full_context(self):
        for n in (1, 7, 30):
            context = project.full_context(candidates(n), {})
            targets = project.target_plan(context)
            self.assertEqual([i for t in targets if t['stage'] == 'A' for i in t['targets']], [r['id'] for r in context['baseline']])
            self.assertEqual([i for t in targets if t['stage'] == 'B' for i in t['targets']], [r['id'] for r in context['generated_scenarios']])
            self.assertTrue(all(len(t['targets']) <= 5 for t in targets))
            mapping = project.inverse_mapping(context, self.accepted_A(context))
            for t in targets:
                self.assertEqual(project.request_data(context, t, mapping)['context'], context)

    def test_shared_schema_small_and_API_projection_only(self):
        data = self.data_A()
        schema = project.scoped_schema(data, self.validator.legacy_schema)
        encoded = ev.canonical(schema)
        self.assertLess(len(encoded.encode()), 50000)
        self.assertIn('external_source_id', schema['$defs'])
        self.assertNotIn('enum', schema['$defs']['external_source_id'])
        self.assertEqual(schema['$defs']['conflict_source_id']['anyOf'][0], {'$ref': '#/$defs/external_source_id'})
        self.assertIn('uniqueItems', encoded)
        projected = ev.api_schema(schema, 'omit-unique-items')
        self.assertNotIn('uniqueItems', ev.canonical(projected))
        payload = project.payload_for(data, schema, project.prompt_text())
        self.assertLess(len(ev.canonical(payload).encode()), project.MAX_REQUEST_BYTES)
        self.assertEqual(payload['model'], project.DEFAULT_MODEL)
        self.assertEqual(project.payload_for(data, schema, project.prompt_text(), model='sealed-judge-x')['model'], 'sealed-judge-x')
        self.assertTrue(payload['stream'])
        self.assertFalse(payload['store'])
        self.assertEqual(payload['max_output_tokens'], project.MAX_OUTPUT_TOKENS)
        self.assertEqual(payload['max_output_tokens'], 2048)
        # The wire carries the sealed thinking control exactly when the sealed rung sets one.
        effort = project.sealed_reasoning_effort()
        if effort is None:
            self.assertNotIn('reasoning', payload)
        else:
            self.assertEqual(effort, 'none')
            self.assertEqual(payload['reasoning'], {'effort': effort})

    def test_A_B_valid_and_full_legacy_assembly(self):
        accepted = self.accepted_A()
        for entry in accepted:
            data = project.request_data(self.context, next(t for t in project.target_plan(self.context) if t['request_id'] == entry['wire']['request_id']))
            self.valid(data, entry['wire'])
        mapping = project.inverse_mapping(self.context, accepted)
        for t in project.target_plan(self.context):
            if t['stage'] == 'B':
                data = project.request_data(self.context, t, mapping)
                wire = self.valid(data)
                accepted.append({'wire': wire, 'origin': {'stage': 'B', 'request_id': t['request_id']}})
        result = project.assembled_result(self.context, accepted)
        self.validator.legacy(self.context, result)
        self.assertEqual(len(result['baseline_judgments']), 117)
        self.assertEqual(len(result['generated_judgments']), 7)
        self.assertEqual(result['baseline_judgments'][0], accepted[0]['wire']['baseline_judgments'][self.context['baseline'][0]['id']])

    def test_missing_A_no_B_and_incomplete_no_scores(self):
        a = self.accepted_A()[:-1]
        with self.assertRaises(SafeFailure):
            project.inverse_mapping(self.context, a)
        report = project.summarize(self.context, a)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIsNone(report['coverage_scores'])
        self.assertIsNone(report['candidate_counts'])

    def test_B_mapping_strict_reverse_and_dispute_preserved(self):
        accepted = self.accepted_A()
        gid = self.context['generated_scenarios'][0]['id']
        bid = self.context['baseline'][0]['id']
        row = accepted[0]['wire']['baseline_judgments'][bid]
        row.update(coverage='full', matched_generated_ids=[gid], evidence=[{'generated_id': gid, 'quote': '真实测试意图'}], covered_aspects=['主意图'], missing_aspects=[])
        mapping = project.inverse_mapping(self.context, accepted)
        self.assertEqual(mapping['candidate_to_baseline'][gid], [bid])
        target = next(t for t in project.target_plan(self.context) if t['stage'] == 'B')
        data = project.request_data(self.context, target, mapping)
        wire = wire_for(data)
        wire['review_queue'].append({'target_type': 'generated', 'target_id': gid, 'issue_type': 'coverage', 'reason': 'A映射有疑点'})
        wire['limitations'].append('A映射有疑点，图检查不证明语义正确')
        self.valid(data, wire)
        wire['generated_judgments'][gid]['matched_baseline_ids'] = []
        with self.assertRaises(SafeFailure):
            self.valid(data, wire)

    def test_B_new_requires_novelty_review(self):
        data = self.data_B()
        wire = wire_for(data)
        wire['review_queue'] = []
        with self.assertRaises(SafeFailure):
            self.valid(data, wire)

    def test_unknown_ID_and_other_map_and_review_target_rejected(self):
        data = self.data_A()
        for mutate in (
                lambda w: w['baseline_judgments'].update(UNKNOWN={}),
                lambda w: w.update(generated_judgments={'UNKNOWN': {}}),
                lambda w: w.update(generated_judgments={}),
                lambda w: w['review_queue'].append({'target_type': 'baseline', 'target_id': 'CAN-000', 'issue_type': 'coverage', 'reason': 'bad'})):
            wire = wire_for(data)
            mutate(wire)
            with self.assertRaises(SafeFailure):
                self.valid(data, wire)

    def test_single_stage_judgment_field_only_and_no_empty_object_schema(self):
        """scoped1.1: the non-stage map is undeclared (not an empty object) and locally forbidden."""
        for stage, field, other in (('A', 'baseline_judgments', 'generated_judgments'),
                                    ('B', 'generated_judgments', 'baseline_judgments')):
            with self.subTest(stage=stage):
                data = self.data_A() if stage == 'A' else self.data_B()
                schema = project.scoped_schema(data, self.validator.legacy_schema)
                both = ('baseline_judgments', 'generated_judgments')
                self.assertEqual([k for k in both if k in schema['properties']], [field])
                self.assertEqual([k for k in both if k in schema['required']], [field])
                self.assertEqual(project.empty_object_paths(schema), [])
                self.assertNotIn('"' + other + '"', ev.canonical(schema['properties']))
                # The gateway-rejected shape (empty non-stage object) is now rejected locally too,
                # and the non-stage field is refused whether empty or populated.
                for wrong in ({}, {'SOME-ID': {}}):
                    wire = wire_for(data)
                    wire[other] = wrong
                    with self.assertRaises(SafeFailure):
                        self.valid(data, wire)
                missing = wire_for(data)
                del missing[field]
                with self.assertRaises(SafeFailure):
                    self.valid(data, missing)
                empty_stage = wire_for(data)
                empty_stage[field] = {}
                with self.assertRaises(SafeFailure):
                    self.valid(data, empty_stage)

    def test_false_quote_and_source_reference(self):
        data = self.data_B()
        gid = data['targets'][0]
        for ref in ({'source_id': 'baseline:AUC-000:description', 'quote': '不存在文本', 'explanation': 'bad'},
                    {'source_id': 'baseline:UNKNOWN:description', 'quote': '真实', 'explanation': 'bad'}):
            wire = wire_for(data)
            wire['generated_judgments'][gid]['source_refs'] = [ref]
            with self.assertRaises(SafeFailure):
                self.valid(data, wire)
        wire = wire_for(data)
        wire['generated_judgments'][gid]['evidence'][0]['quote'] = '虚假引文'
        with self.assertRaises(SafeFailure):
            self.valid(data, wire)

    def test_internal_conflict_two_real_fragments_ownership_and_nonconflict(self):
        data = self.data_B()
        gid = data['targets'][0]
        wire = wire_for(data)
        row = wire['generated_judgments'][gid]
        row['business_consistency'] = 'conflict'
        row['source_refs'] = [{'source_id': f'generated:{gid}:text', 'quote': q, 'explanation': '同一候选内部矛盾'} for q in ('甲', '乙')]
        wire['review_queue'].append({'target_type': 'generated', 'target_id': gid, 'issue_type': 'business_consistency', 'reason': '内部矛盾'})
        self.valid(data, wire)
        for mutation in ('single', 'nonconflict', 'other_owner'):
            bad = copy.deepcopy(wire)
            row = bad['generated_judgments'][gid]
            if mutation == 'single':
                row['source_refs'] = row['source_refs'][:1]
            elif mutation == 'nonconflict':
                row['business_consistency'] = 'consistent'
            else:
                for ref in row['source_refs']:
                    ref['source_id'] = f"generated:{data['targets'][1]}:text"
            with self.assertRaises(SafeFailure):
                self.valid(data, bad)

    def test_duplicate_owner_keeps_cross_task_IDs(self):
        data = self.data_B()
        allids = sorted(r['id'] for r in self.context['generated_scenarios'])
        members = [allids[0], allids[-1]]
        wire = wire_for(data)
        wire['duplicate_groups'] = [{'group_id': data['request_id'] + ':D1', 'generated_ids': members,
            'evidence': [{'generated_id': g, 'quote': '真实测试意图'} for g in members], 'reason': 'mock重复'}]
        self.valid(data, wire)
        data2 = copy.deepcopy(data)
        data2['targets'] = [members[-1]]
        bad = wire_for(data2)
        bad['duplicate_groups'] = wire['duplicate_groups']
        with self.assertRaises(SafeFailure):
            self.valid(data2, bad)

    def test_prepare_offline_no_credentials_network(self):
        with patch.object(project, 'get_provider', side_effect=AssertionError('credentials')), patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            output = self.prepared(1)
            report, _ = project.audit(output)
            self.assertEqual(report['http_count'], 0)
            self.assertEqual(report['status'], 'incomplete')

    def test_prepare_exclusive_and_lock(self):
        output = self.prepared(1)
        with project.pid_lock(output):
            with self.assertRaises(SafeFailure):
                with project.pid_lock(output):
                    pass
        self.assertFalse((output / '.active.lock').exists())
        with self.assertRaises(SafeFailure):
            project.prepare(self.root / 'scenarios.json', self.root / 'manifest.json', output)

    def test_complete_execute_resume_zero_resend_tamper_rejected(self):
        output = self.prepared(1)
        transport = MockTransport()
        report = project.execute(output, transport=transport)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(transport.calls), 25)
        self.assertEqual(report['http_count'], 25)
        import csv
        self.assertEqual(len(list(csv.DictReader((output / 'baseline-coverage.csv').open()))), 117)
        self.assertEqual(len(list(csv.DictReader((output / 'candidate-judgments.csv').open()))), 1)
        self.assertEqual(report['usage_all_attempts']['prompt_tokens'], 2500)
        report = project.execute(output, resume=True, transport=transport)
        self.assertEqual(len(transport.calls), 25)
        self.assertEqual(report['status'], 'complete')
        path = output / 'requests/A-001/attempt-001/content.original.txt'
        path.write_text('{}')
        with self.assertRaises(SafeFailure):
            project.execute(output, resume=True, transport=transport)
        self.assertEqual(len(transport.calls), 25)

    def test_SSE_incomplete_and_model_mismatch_no_missing_or_B(self):
        for fail in ('incomplete', 'model'):
            with self.subTest(fail=fail):
                output = self.prepared(1)
                transport = MockTransport(fail=fail)
                report = project.execute(output, transport=transport)
                self.assertEqual(report['status'], 'incomplete')
                self.assertIsNone(report['coverage_scores'])
                self.assertEqual(len(transport.calls), 3 if fail == 'incomplete' else 1)
                self.temp.cleanup()
                self.temp = tempfile.TemporaryDirectory()
                self.addCleanup(self.temp.cleanup)
                self.root = Path(self.temp.name)

    def test_401_cancels_and_no_resume_blind_resend(self):
        output = self.prepared(1)
        transport = MockTransport(fail='401')
        report = project.execute(output, transport=transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(report['status'], 'incomplete')
        project.execute(output, resume=True, transport=transport)
        self.assertEqual(len(transport.calls), 1)

    def test_sources_tamper_and_generation_contract(self):
        output = self.prepared(1)
        (self.root / 'manifest.json').write_text('{}')
        with self.assertRaises(SafeFailure):
            project.audit(output)
        for change in ({'project_id': 1}, {'space_id': 1}, {'status': 'pending'}, {'tasks': []}):
            m = manifest()
            m.update(change)
            with self.assertRaises(SafeFailure):
                project.validate_generation({'scenarios': candidates(1)}, m)

    def test_B_unresolved_links_requires_review_and_limitation(self):
        accepted = self.accepted_A()
        bid, gid = self.context['baseline'][0]['id'], self.context['generated_scenarios'][0]['id']
        accepted[0]['wire']['baseline_judgments'][bid].update(coverage='unresolved', matched_generated_ids=[gid], evidence=[{'generated_id': gid, 'quote': '真实测试意图'}])
        mapping = project.inverse_mapping(self.context, accepted)
        target = next(t for t in project.target_plan(self.context) if t['stage'] == 'B')
        data = project.request_data(self.context, target, mapping)
        wire = wire_for(data)
        wire['generated_judgments'][gid]['relation'] = 'unresolved'
        with self.assertRaises(SafeFailure):
            self.valid(data, wire)
        wire['review_queue'].append({'target_type': 'generated', 'target_id': gid, 'issue_type': 'coverage', 'reason': '含糊映射'})
        wire['limitations'] = ['含糊映射待复核']
        self.valid(data, wire)

    def test_bounded_transport_retries_and_format_repairs(self):
        output = self.prepared(1)
        delegate = MockTransport()
        count = [0]
        def flaky(body):
            count[0] += 1
            if count[0] <= 2:
                return {'http_status': 503, 'body': {}, 'error': None}
            response = delegate(body)
            if count[0] in (3, 4):
                data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
                bad = wire_for(data)
                bad['baseline_judgments'][data['targets'][0]]['reason'] = 3
                return capture(bad)
            return response
        scheduler = project.ScopedScheduler(output, flaky)
        target = scheduler.spec['target_plan'][0]
        self.assertIsNotNone(scheduler.evaluate(target))
        self.assertEqual(count[0], 5)
        self.assertIsNotNone(scheduler.evaluate(target))
        self.assertEqual(count[0], 5)
        self.assertTrue((output / 'requests/A-001/attempt-004/repair.json').exists())
        report, _ = project.audit(output)
        self.assertEqual(report['http_count'], 5)
        self.assertEqual(report['usage_all_attempts']['prompt_tokens'], 300)

    def test_absent_or_null_container_fields_filled_and_recorded(self):
        """Lead-authorized container normalization: absent/null root echo fields are filled, never guessed.

        -006 lost 12 attempts to `scoped_schema_invalid: <root>: 'request_id' is a required property`.
        The four root fields are redundant copies of sealed request/plan values, so an absent or null one
        is filled before schema validation, recorded per attempt and disclosed - while every business row
        stays exactly as the model wrote it.
        """
        output = self.prepared(1)
        delegate = MockTransport()
        sent = []
        count = [0]

        def transport(body):
            count[0] += 1
            sent.append(body)
            data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
            bad = wire_for(data)
            bad.pop('request_id')       # exactly what the real model omitted in -006
            bad.pop('schema_version')
            bad['input_id'] = None      # explicit null behaves like absence
            return capture(bad)

        scheduler = project.ScopedScheduler(output, transport)
        target = scheduler.spec['target_plan'][0]
        row_before = wire_for(project.request_data(scheduler.context, target))['baseline_judgments']
        entry = scheduler.evaluate(target)
        self.assertIsNotNone(entry)
        # Filled, so no repair generation was needed at all.
        self.assertEqual(count[0], 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(entry['wire']['schema_version'], project.SCHEMA_VERSION)
        self.assertEqual(entry['wire']['input_id'], scheduler.context['input_id'])
        self.assertEqual(entry['wire']['input_id'], 'project3-space2-001')
        self.assertEqual(entry['wire']['request_id'], target['request_id'])
        self.assertEqual(entry['wire']['evaluation_status'], 'complete')
        state = ev.load(output / 'requests/A-001/attempt-001/status.json')
        self.assertEqual(state['status'], 'success')
        self.assertEqual(state['container_fields_filled'], ['schema_version', 'input_id', 'request_id'])
        record = ev.load(output / 'requests/A-001/attempt-001/validation.json')
        self.assertEqual(record['status'], 'passed')
        self.assertEqual(record['container_fields_filled'], state['container_fields_filled'])
        # The fill is recorded as evidence, and the business rows are byte-identical to the model's own.
        self.assertEqual(ev.load(output / 'requests/A-001/attempt-001/wire.json')['request_id'], target['request_id'])
        self.assertEqual(entry['wire']['baseline_judgments'], row_before)
        report, _ = project.audit(output)
        self.assertEqual(report['container_normalization']['fields'],
                         ['schema_version', 'input_id', 'request_id', 'evaluation_status'])
        self.assertEqual(report['container_normalization']['total_attempts_filled'], 1)
        self.assertEqual(report['container_normalization']['attempts'][0]['fields'], state['container_fields_filled'])
        self.assertTrue(report['container_normalization']['attempts'][0]['path'].startswith('requests/A-001'))
        project.write_reports(output, report, None, scheduler.context)
        markdown = (output / 'report.md').read_text()
        self.assertIn('容器字段规范化', markdown)
        self.assertIn('generations_max=8', markdown)

    def test_wrong_container_value_still_rejected_and_repaired_with_exact_hint(self):
        """Only absent/null is filled: a present-but-wrong root value must keep failing, and the repair
        turn must carry the exact machine error plus the sealed container values (no label preselection).
        """
        output = self.prepared(1)
        delegate = MockTransport()
        sent = []
        count = [0]

        def transport(body):
            count[0] += 1
            sent.append(body)
            data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
            if count[0] == 1:
                bad = wire_for(data)
                bad['request_id'] = 'B-999'     # wrong, not absent: never silently overwritten
                return capture(bad)
            return delegate(body)

        scheduler = project.ScopedScheduler(output, transport)
        target = scheduler.spec['target_plan'][0]
        entry = scheduler.evaluate(target)
        self.assertIsNotNone(entry)
        self.assertEqual(count[0], 2)
        first = ev.load(output / 'requests/A-001/attempt-001/status.json')
        self.assertEqual(first['status'], 'evaluation_failed')
        self.assertEqual(first['failure_kind'], 'format')
        # jsonschema reports a `const` violation by naming the expected sealed value.
        self.assertEqual(first['errors'], ["scoped_schema_invalid: request_id: '"
                                           + target['request_id'] + "' was expected"])
        self.assertEqual(first['container_fields_filled'], [])
        # The wrong value is archived verbatim - never silently overwritten by the normalization.
        self.assertEqual(ev.load(output / 'requests/A-001/attempt-001/wire.json')['request_id'], 'B-999')
        self.assertEqual([m['role'] for m in sent[1]['input']], ['system', 'user', 'assistant', 'user'])
        repair = sent[1]['input'][-1]['content']
        # The machine error is echoed verbatim, first, and the appended hint names this request's values
        # (the hint is carried inside the same JSON `errors` list, so it is asserted in parsed form).
        self.assertIn("request_id: '" + target['request_id'] + "' was expected", repair)
        parsed = ev.strict_json(repair.split('机器错误：', 1)[1].split(' 引用源结构规则', 1)[0])['errors']
        self.assertEqual(parsed[:len(first['errors'])], first['errors'])
        self.assertEqual(len(parsed), len(first['errors']) + 2)
        self.assertIn('wire顶层必须逐字包含四个容器/回显字段', parsed[1])
        self.assertIn('request_id="' + target['request_id'] + '"', parsed[1])
        self.assertIn('evaluation_status="complete"', parsed[1])
        self.assertIn('输出必须是单个JSON对象', parsed[2])
        # repair.json archives the raw machine errors, unchanged by the prompt-side hint.
        self.assertEqual(ev.load(output / 'requests/A-001/attempt-002/repair.json'),
                         {'prior': sent[1]['input'][2]['content'], 'errors': first['errors']})
        # Re-running the same target replays the archived success: no resend, same wire.
        self.assertEqual(scheduler.evaluate(target)['wire'], entry['wire'])
        self.assertEqual(count[0], 2)

    def test_missing_request_id_and_target_row_repair_names_exact_machine_errors(self):
        """The observed real failure mode: the wire drops one target row (a real, non-container error).

        The bounded repair generation must be handed the exact machine errors - naming the exact target ID
        - and must recover inside the generation budget with no business label changed. The dropped root
        `request_id` is no longer an error (it is filled), which is exactly why the row error is asserted.
        """
        output = self.prepared(1)
        delegate = MockTransport()
        sent = []
        count = [0]

        def transport(body):
            count[0] += 1
            sent.append(body)
            data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
            if count[0] == 1:
                bad = wire_for(data)
                bad.pop('request_id')
                bad['baseline_judgments'].pop(data['targets'][0])
                return capture(bad)
            return delegate(body)

        scheduler = project.ScopedScheduler(output, transport)
        target = scheduler.spec['target_plan'][0]
        dropped = target['targets'][0]
        entry = scheduler.evaluate(target)
        self.assertIsNotNone(entry)
        self.assertEqual(count[0], 2)
        first = ev.load(output / 'requests/A-001/attempt-001/status.json')
        self.assertEqual(first['status'], 'evaluation_failed')
        self.assertEqual(first['failure_kind'], 'format')
        self.assertEqual(first['errors'], ["scoped_schema_invalid: baseline_judgments: '"
                                           + dropped + "' is a required property"])
        # The dropped container field was still filled and recorded on the failed attempt.
        self.assertEqual(first['container_fields_filled'], ['request_id'])
        # The repair turn is the third message of the second request and carries the machine errors.
        self.assertEqual([m['role'] for m in sent[1]['input']], ['system', 'user', 'assistant', 'user'])
        repair = sent[1]['input'][-1]['content']
        self.assertIn(dropped, repair)
        self.assertIn('required property', repair)
        parsed = ev.strict_json(repair.split('机器错误：', 1)[1].split(' 引用源结构规则', 1)[0])['errors']
        self.assertEqual(parsed[:len(first['errors'])], first['errors'])
        self.assertEqual(ev.load(output / 'requests/A-001/attempt-002/repair.json'),
                         {'prior': sent[1]['input'][2]['content'], 'errors': first['errors']})
        # Re-running the same target replays the archived success: no resend, same wire.
        self.assertEqual(scheduler.evaluate(target)['wire'], entry['wire'])
        self.assertEqual(count[0], 2)

    def test_generation_budget_eight_recovers_where_three_failed(self):
        """-006's A-018 exhausted the sealed 3-generation budget; the sealed budget is now 8 generations."""
        self.assertEqual(project.GENERATIONS_MAX, 8)
        self.assertEqual(project.FORMAT_REPAIRS_MAX, 7)
        sealed = project.sealed_parameters()
        self.assertEqual(sealed['generations_max'], 8)
        self.assertEqual(sealed['format_repairs_max'], 7)
        self.assertEqual(sealed['transport_retries_max'], 2)
        output = self.prepared(1)
        delegate = MockTransport()
        sent = []
        count = [0]

        def transport(body):
            count[0] += 1
            sent.append(body)
            data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
            if count[0] < 8:
                bad = wire_for(data)
                bad['baseline_judgments'][data['targets'][0]]['reason'] = 3
                return capture(bad)
            return delegate(body)

        scheduler = project.ScopedScheduler(output, transport)
        target = scheduler.spec['target_plan'][0]
        entry = scheduler.evaluate(target)
        self.assertIsNotNone(entry)
        self.assertEqual(count[0], 8)                       # 1 initial + 7 bounded repairs
        self.assertTrue((output / 'requests/A-001/attempt-008/repair.json').exists())
        self.assertFalse((output / 'requests/A-001/attempt-001/repair.json').exists())
        self.assertEqual([m['role'] for m in sent[-1]['input']], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(ev.load(output / 'requests/A-001/attempt-001/status.json')['failure_kind'], 'format')
        report, _ = project.audit(output)
        self.assertEqual(report['http_count'], 8)
        self.assertEqual(report['sealed_parameters']['generations_max'], 8)

    def test_generation_budget_exhaustion_stops_without_a_ninth_call(self):
        output = self.prepared(1)
        count = [0]

        def always_bad(body):
            count[0] += 1
            data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
            bad = wire_for(data)
            bad['baseline_judgments'][data['targets'][0]]['reason'] = 3
            return capture(bad)

        scheduler = project.ScopedScheduler(output, always_bad)
        self.assertIsNone(scheduler.evaluate(scheduler.spec['target_plan'][0]))
        self.assertEqual(count[0], project.GENERATIONS_MAX)
        report, assembled = project.audit(output)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIsNone(assembled)
        self.assertEqual(report['http_count'], 8)
        self.assertEqual(report['baseline_technical_unassessed'], 117)

    def test_stage_judgment_map_guard_names_missing_and_unexpected_ids(self):
        """Local guard behind the schema: it must name the exact missing/unexpected target IDs."""
        data = self.data_A()
        relaxed = copy.deepcopy(project.scoped_schema(data, self.validator.legacy_schema))
        relaxed['properties']['baseline_judgments']['required'] = []
        relaxed['properties']['baseline_judgments']['additionalProperties'] = True
        wire = wire_for(data)
        wire['baseline_judgments']['NOT-A-TARGET'] = copy.deepcopy(wire['baseline_judgments'][data['targets'][0]])
        wire['baseline_judgments'].pop(data['targets'][0])
        with self.assertRaises(SafeFailure) as caught:
            project.validate_scoped(data, wire, relaxed, self.validator)
        message = str(caught.exception)
        self.assertIn('stage_judgment_map_incomplete', message)
        self.assertIn('missing target IDs: ' + data['targets'][0], message)
        self.assertIn('unexpected IDs: NOT-A-TARGET', message)
        self.assertIn('required IDs: ' + ', '.join(map(str, data['targets'])), message)

    def test_row_attributed_machine_errors_name_the_offending_row(self):
        """The -006 A-018 failure mode: a bare rule token cannot be repaired.

        Every row/group-level machine error must name the exact JSON location the model has to fix,
        while still starting with the unchanged rule token.
        """
        data = self.data_A()
        rid = data['targets'][0]
        # coverage=full|partial without links/covered_aspects: exactly what A-018 emitted three times.
        wire = wire_for(data)
        wire['baseline_judgments'][rid]['coverage'] = 'partial'
        with self.assertRaises(SafeFailure) as caught:
            self.valid(data, wire)
        message = str(caught.exception)
        self.assertTrue(message.startswith('coverage_evidence_required: baseline_judgments[' + rid + ']:'))
        self.assertIn('coverage=missing is the only coverage value validated', message)
        # A quote that is not a verbatim substring of the quoted candidate.
        wire = wire_for(data)
        wire['baseline_judgments'][rid]['coverage'] = 'missing'
        wire['baseline_judgments'][rid]['evidence'] = [{'generated_id': self.context['generated_scenarios'][0]['id'],
                                                        'quote': '不存在的引文'}]
        with self.assertRaises(SafeFailure) as caught:
            self.valid(data, wire)
        self.assertTrue(str(caught.exception).startswith(
            'false_evidence_quote: baseline_judgments[' + rid + ']: evidence.generated_id='))
        self.assertIn('不存在的引文', str(caught.exception))
        # A declared source ID whose quote is not a verbatim substring of that source.
        declared = 'baseline:' + rid + ':' + self.validator.mod.BASELINE_FIELDS[0]
        wire = wire_for(data)
        wire['baseline_judgments'][rid]['source_refs'] = [{'source_id': declared, 'quote': '不存在的来源引文',
                                                           'explanation': '测试'}]
        with self.assertRaises(SafeFailure) as caught:
            self.valid(data, wire)
        self.assertTrue(str(caught.exception).startswith(
            'false_source_quote_or_ID: baseline_judgments[' + rid + ']: source_refs[0].source_id="' + declared + '"'))
        self.assertIn('is not a verbatim substring of that source', str(caught.exception))
        # Duplicate groups are attributed to the group, not to a bare token either.
        bdata = self.data_B()
        members = sorted(r['id'] for r in self.context['generated_scenarios'])[:2]
        group = {'group_id': bdata['request_id'] + ':D1', 'generated_ids': members, 'reason': 'mock重复',
                 'evidence': [{'generated_id': members[0], 'quote': '真实测试意图'}]}
        wire = wire_for(bdata)
        wire['duplicate_groups'] = [group]
        with self.assertRaises(SafeFailure) as caught:
            self.valid(bdata, wire)
        self.assertTrue(str(caught.exception).startswith(
            'duplicate_evidence_mismatch: duplicate_groups[0:' + bdata['request_id'] + ':D1]:'))

    def test_coverage_repair_recovers_from_a_row_attributed_error(self):
        """One bounded repair generation turns the -006 A-018 shape into a valid wire (no resend)."""
        output = self.prepared(1)
        delegate = MockTransport()
        count = [0]

        def transport(body):
            count[0] += 1
            data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
            if count[0] == 1:
                bad = wire_for(data)
                for row in bad['baseline_judgments'].values():
                    row['coverage'] = 'partial'
                return capture(bad)
            return delegate(body)

        scheduler = project.ScopedScheduler(output, transport)
        target = scheduler.spec['target_plan'][0]
        entry = scheduler.evaluate(target)
        self.assertIsNotNone(entry)
        self.assertEqual(count[0], 2)
        first = ev.load(output / 'requests/A-001/attempt-001/status.json')
        self.assertEqual(first['failure_kind'], 'format')
        self.assertTrue(all(text in first['errors'][0] for text in
                            ('coverage_evidence_required', 'baseline_judgments[' + target['targets'][0] + ']')))
        self.assertEqual(sorted(entry['wire']['baseline_judgments']), sorted(target['targets']))

    def interrupted(self, n=1):
        """Create n started-only attempts for A-001; returns (output, scheduler, target, transport)."""
        output = self.prepared(1)
        transport = MockTransport()
        scheduler = project.ScopedScheduler(output, transport)
        target = scheduler.spec['target_plan'][0]
        data = project.request_data(scheduler.context, target)
        schema = project.scoped_schema(data, scheduler.validator.legacy_schema)
        for index in range(1, n + 1):
            directory = output / 'requests/A-001' / f'attempt-{index:03d}'
            directory.mkdir(parents=True)
            ev.save(directory / 'input.json', data)
            ev.save(directory / 'schema.json', schema)
            ev.save(directory / 'request.json', project.payload_for(data, schema, scheduler.prompt, model=scheduler.model))
            ev.save(directory / 'started.json', {'pid': 123, 'request_id': 'A-001', 'model': scheduler.model})
        return output, scheduler, target, transport

    def test_started_only_attempt_is_counted_and_recovered_in_budget(self):
        output, scheduler, target, transport = self.interrupted(1)
        report, _ = project.audit(output)
        self.assertEqual(report['http_count'], 0)
        self.assertEqual(report['interrupted_unknown_cost'], ['requests/A-001/attempt-001'])
        self.assertEqual(report['http_attempts_total'], 1)
        entry = scheduler.evaluate(target)
        self.assertIsNotNone(entry)
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue((output / 'requests/A-001/attempt-002/status.json').exists())
        report, _ = project.audit(output)
        self.assertEqual(report['http_count'], 1)
        self.assertEqual(report['http_attempts_total'], 2)
        self.assertEqual(report['interrupted_unknown_cost'], ['requests/A-001/attempt-001'])

    def test_started_only_budget_exhaustion_stops_without_http(self):
        output, scheduler, target, transport = self.interrupted(3)
        self.assertIsNone(scheduler.evaluate(target))
        self.assertEqual(transport.calls, [])
        report, _ = project.audit(output)
        self.assertEqual(len(report['interrupted_unknown_cost']), 3)
        self.assertEqual(report['http_attempts_total'], 3)
        self.assertEqual(report['status'], 'incomplete')

    def test_captured_without_status_reverified_without_resend(self):
        output = self.prepared(1)
        transport = MockTransport()
        scheduler = project.ScopedScheduler(output, transport)
        target = scheduler.spec['target_plan'][0]
        data = project.request_data(scheduler.context, target)
        schema = project.scoped_schema(data, scheduler.validator.legacy_schema)
        directory = output / 'requests/A-001/attempt-001'
        directory.mkdir(parents=True)
        ev.save(directory / 'input.json', data)
        ev.save(directory / 'schema.json', schema)
        ev.save(directory / 'request.json', project.payload_for(data, schema, scheduler.prompt, model=scheduler.model))
        ev.save(directory / 'started.json', {'pid': 123, 'request_id': 'A-001', 'model': scheduler.model})
        ev.save(directory / 'response.safe.json', capture(wire_for(data), model=scheduler.model))
        report, _ = project.audit(output)
        self.assertEqual(report['incomplete_capture_unknown_status'], ['requests/A-001/attempt-001'])
        self.assertEqual(report['http_count'], 1)
        entry = scheduler.evaluate(target)
        self.assertIsNotNone(entry)
        self.assertEqual(transport.calls, [])
        self.assertEqual(ev.load(directory / 'status.json')['status'], 'success')
        report, _ = project.audit(output)
        self.assertEqual(report['incomplete_capture_unknown_status'], [])
        self.assertEqual(report['http_attempts_total'], 1)

    def test_completed_only_nonempty_capture_rejected_strict_assemble(self):
        data = self.data_A()
        response = completed_only_capture(wire_for(data))
        self.assertIsNone(inspect_sse_capture(response))
        with self.assertRaises(SafeFailure):
            project.strict_assemble_capture(response)
        with self.assertRaises(SafeFailure):
            project.verify_sse_capture(response)
        # No error is recorded, so the two-channel validator must use the frozen path only and reject.
        with self.assertRaises(SafeFailure) as caught:
            project.strict_two_channel_assemble(response)
        self.assertIn('sse_fidelity_', str(caught.exception))
        output = self.prepared(1)
        report = project.execute(output, transport=MockTransport(fail='no-lifecycle'))
        self.assertEqual(len(report['http_captures']), 1)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['technical_failures'][0]['status'], 'evaluation_failed')

    def test_plan_sealed_model_rejects_other_model_on_execute_and_audit(self):
        output = self.prepared(1, model='judge-sealed-x')
        self.assertEqual(ev.load(output / 'plan.json')['model'], 'judge-sealed-x')
        project.validate_prepared(output, 'judge-sealed-x')
        for other in ('judge-sealed-y', project.DEFAULT_MODEL, 'gpt-6.1-sol'):
            with self.subTest(other=other):
                self.assertRaises(SafeFailure, project.validate_prepared, output, other)
                self.assertRaises(SafeFailure, project.audit, output, other)
        transport = MockTransport(model='judge-sealed-x')
        self.assertRaises(SafeFailure, project.execute, output, 2, False, transport, 'judge-sealed-y')
        self.assertEqual(transport.calls, [])
        # The CLI must require an explicit model and refuse one that differs from the sealed plan.
        self.assertEqual(project.main(['execute', '--execute', '--run-dir', str(output),
                                       '--model', 'judge-sealed-y']), 1)
        self.assertEqual(transport.calls, [])
        with self.assertRaises(SystemExit):
            project.main(['execute', '--execute', '--run-dir', str(output)])
        self.assertEqual(transport.calls, [])
        report = project.execute(output, 2, False, transport, 'judge-sealed-x')
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['judge_model'], 'judge-sealed-x')

    def test_plan_sources_and_protected_cannot_be_deleted_or_emptied(self):
        mutations = ('sources_empty', 'sources_trim', 'protected_empty', 'protected_trim')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                output = self.prepared(1)
                plan_path = output / 'plan.json'
                plan = ev.load(plan_path)
                field = 'sources' if mutation.startswith('sources') else 'protected'
                plan[field] = {} if mutation.endswith('empty') else dict(list(plan[field].items())[1:])
                plan['fingerprint_sha256'] = ev.digest(ev.canonical(
                    {k: v for k, v in plan.items() if k != 'fingerprint_sha256'}))
                plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
                self.assertRaises(SafeFailure, project.validate_prepared, output)
                self.temp.cleanup()
                self.temp = tempfile.TemporaryDirectory()
                self.addCleanup(self.temp.cleanup)
                self.root = Path(self.temp.name)

    def test_audit_verifies_execution_started_plan_seal(self):
        output = self.prepared(1)
        project.audit(output)
        ev.save(output / 'execution.started.json', {'plan_sha256': 'deadbeef', 'workers': 2})
        self.assertRaises(SafeFailure, project.audit, output)
        self.assertRaises(SafeFailure, project.execute, output, 2, True, MockTransport())
        (output / 'execution.started.json').unlink()
        ev.save(output / 'execution.started.json', {'plan_sha256': ev.sha(output / 'plan.json'), 'workers': 2})
        project.audit(output)

    def test_consistent_requires_real_source_refs(self):
        data = self.data_A()
        bid = data['targets'][0]
        wire = wire_for(data)
        wire['baseline_judgments'][bid]['business_consistency'] = 'consistent'
        with self.assertRaises(SafeFailure):
            self.valid(data, wire)
        wire['baseline_judgments'][bid]['source_refs'] = [self.real_ref(bid)]
        self.valid(data, wire)

    def test_uncertain_granularity_requires_matching_review_item(self):
        data = self.data_B()
        gid = data['targets'][0]
        wire = wire_for(data)
        wire['generated_judgments'][gid]['granularity'] = 'uncertain'
        with self.assertRaises(SafeFailure):
            self.valid(data, wire)
        wire['review_queue'].append({'target_type': 'generated', 'target_id': gid,
                                     'issue_type': 'granularity', 'reason': '粒度含糊待复核'})
        self.valid(data, wire)

    def test_B_duplicate_group_id_namespaced_by_request_id(self):
        data = self.data_B()
        allids = sorted(r['id'] for r in self.context['generated_scenarios'])
        members = [allids[0], allids[-1]]
        base = {'generated_ids': members, 'reason': 'mock重复',
                'evidence': [{'generated_id': g, 'quote': '真实测试意图'} for g in members]}
        for group_id in ('D1', 'B-999:D1', data['request_id'], 'A-001:D1'):
            wire = wire_for(data)
            wire['duplicate_groups'] = [dict(base, group_id=group_id)]
            with self.assertRaises(SafeFailure):
                self.valid(data, wire)
        wire = wire_for(data)
        wire['duplicate_groups'] = [dict(base, group_id=data['request_id'] + ':D1')]
        self.valid(data, wire)

    def test_assembled_duplicate_group_ids_rejected(self):
        accepted = self.accepted_A()
        data = self.data_B()
        allids = sorted(r['id'] for r in self.context['generated_scenarios'])
        members = [allids[0], allids[-1]]
        group = {'group_id': data['request_id'] + ':D1', 'generated_ids': members, 'reason': 'mock重复',
                 'evidence': [{'generated_id': g, 'quote': '真实测试意图'} for g in members]}
        for index in (1, 2):
            wire = wire_for(data)
            wire['generated_judgments'] = {}
            wire['duplicate_groups'] = [copy.deepcopy(group)]
            accepted.append({'wire': wire, 'origin': {'stage': 'B', 'request_id': data['request_id'],
                                                      'attempt': f'attempt-00{index}'}})
        with self.assertRaises(SafeFailure):
            project.assembled_result(self.context, accepted)

    def test_A_target_chunk_size_is_five_max(self):
        context = project.full_context(candidates(7), {})
        self.assertRaises(SafeFailure, project.target_plan, context, 6)
        self.assertTrue(all(len(t['targets']) <= 5 for t in project.target_plan(context, 5)))
        data = self.data_A()
        data['targets'] = project.target_plan(self.context)[0]['targets'] + [self.context['baseline'][5]['id']]
        with self.assertRaises(SafeFailure):
            self.valid(data)

    def two_channel_rejection(self, events):
        """Return the scoped rejection reason for a synthetic two-channel capture."""
        with self.assertRaises(SafeFailure) as caught:
            project.strict_two_channel_assemble(capture_from_events(renumber(copy.deepcopy(events))))
        return str(caught.exception)

    def test_two_channel_reasoning_capture_accepted_and_pristine_untouched(self):
        data = self.data_A()
        cap = reasoning_capture(wire_for(data))
        # The archived capture is exactly the frozen-Transport shape: 200, completed body,
        # frozen fidelity error, no derived response.
        self.assertEqual(cap['http_status'], 200)
        self.assertEqual(cap['error'], 'sse_fidelity_segment_item_identity')
        self.assertNotIn('derived_response', cap)
        self.assertEqual(inspect_sse_capture(cap), 'sse_fidelity_segment_item_identity')
        derived, manifest = project.strict_two_channel_assemble(cap)
        self.assertEqual(manifest['channel_mode'], 'reasoning_channel_extension')
        self.assertEqual(manifest['validation_version'], 'sse-two-channel-1.0')
        expected_removed = [e['sequence_number'] for e in two_channel_events(ev.canonical(wire_for(data)))
                            if e['type'] in project.REASONING_PART_EVENTS and e['output_index'] == 0]
        self.assertEqual(manifest['removed_event_count'], 2)
        self.assertEqual(manifest['removed_event_sequence_numbers'], expected_removed)
        self.assertEqual(manifest['reasoning_item_count'], 1)
        self.assertEqual(manifest['recorded_capture_error'], 'sse_fidelity_segment_item_identity')
        self.assertEqual(manifest['frozen_assembler_version'], '1.1.5')
        self.assertEqual(manifest['frozen_assemble_manifest']['carrier_only'], True)
        self.assertEqual([s['item_id'] for s in manifest['frozen_assemble_manifest']['segments']], ['msg_mock'])
        self.assertEqual(derived, cap['body'])
        self.assertEqual(project.verify_sse_capture(cap)[1]['channel_mode'], 'reasoning_channel_extension')
        self.assertEqual(project.sse_validation_record(manifest)['removed_event_count'], 2)
        # A successful capture without any error never takes the filter path.
        pristine = capture(wire_for(data))
        derived2, manifest2 = project.strict_two_channel_assemble(pristine)
        self.assertEqual(manifest2['channel_mode'], 'pristine_frozen_assemble')
        self.assertEqual(manifest2['removed_event_count'], 0)
        self.assertIsNone(manifest2['removed_events_sha256'])
        self.assertEqual(manifest2['frozen_assemble_manifest']['version'], '1.1.5')
        self.assertEqual(derived2, pristine['body'])

    def test_two_channel_reasoning_chain_breaks_rejected(self):
        text = ev.canonical(wire_for(self.data_A()))
        missing_done = [e for e in two_channel_events(text) if e['type'] != 'response.reasoning_text.done']
        self.assertIn('reasoning', self.two_channel_rejection(missing_done))
        mismatched = two_channel_events(text)
        for event in mismatched:
            if event['type'] == 'response.reasoning_text.done':
                event['text'] = '与增量不一致的推理文本'
        self.assertIn('reasoning_delta_done_conflict', self.two_channel_rejection(mismatched))
        orphan = two_channel_events(text)
        for event in orphan:
            if event['type'] == 'response.content_part.done' and event['output_index'] == 0:
                event['content_index'] = 1
        self.assertIn('reasoning_part_done_order', self.two_channel_rejection(orphan))
        leaked = two_channel_events('判断结果：逐条给出结构化判断', reasoning='逐条给出结构化判断')
        self.assertIn('reasoning_text_inside_assistant_output', self.two_channel_rejection(leaked))
        unknown = two_channel_events(text)
        unknown.insert(3, {'type': 'response.reasoning_summary_text.delta', 'output_index': 0, 'content_index': 0,
                           'item_id': 'rsn_mock', 'delta': 'x'})
        self.assertIn('unknown_event_type', self.two_channel_rejection(unknown))
        # A capture whose recorded error is not a frozen fidelity error is never filtered.
        not_fidelity = reasoning_capture(wire_for(self.data_A()))
        not_fidelity['error'] = 'read_timeout'
        with self.assertRaises(SafeFailure) as caught:
            project.strict_two_channel_assemble(not_fidelity)
        self.assertIn('sse_unexpected_capture_error', str(caught.exception))
        # A recorded fidelity error that these bytes do not reproduce is refused, never filtered.
        unreproducible = reasoning_capture(wire_for(self.data_A()))
        unreproducible['error'] = 'sse_fidelity_sequence_gap'
        with self.assertRaises(SafeFailure) as caught:
            project.strict_two_channel_assemble(unreproducible)
        self.assertIn('sse_recorded_fidelity_error_not_reproducible', str(caught.exception))

    def test_two_channel_assistant_channel_breaks_rejected(self):
        text = ev.canonical(wire_for(self.data_A()))
        conflict = two_channel_events(text)
        for event in conflict:
            if event['type'] == 'response.output_text.done':
                event['text'] = '与增量冲突的assistant文本'
        self.assertIn('sse_fidelity_delta_done_conflict', self.two_channel_rejection(conflict))
        unfinished = [e for e in two_channel_events(text)
                      if not (e['type'] == 'response.content_part.done' and e['output_index'] == 1)]
        self.assertIn('sse_fidelity_item_before_part_done', self.two_channel_rejection(unfinished))
        no_item_done = [e for e in two_channel_events(text)
                        if not (e['type'] == 'response.output_item.done' and e['output_index'] == 1)]
        self.assertIn('sse_fidelity_unfinished_item', self.two_channel_rejection(no_item_done))
        body_conflict = reasoning_capture(wire_for(self.data_A()))
        for item in body_conflict['body']['output']:
            if item.get('type') == 'message':
                item['content'] = [{'type': 'output_text', 'text': '与SSE消息通道不一致的正文'}]
        self.assertIn('sse_fidelity_nonempty_completed_conflict',
                      str(self.two_channel_rejection_body(body_conflict)))

    def two_channel_rejection_body(self, capture_object):
        with self.assertRaises(SafeFailure) as caught:
            project.strict_two_channel_assemble(capture_object)
        return caught.exception

    def test_two_channel_live_capture_sealed_in_validation_and_reports(self):
        output = self.prepared(1)
        transport = MockTransport(fail='reasoning')
        report = project.execute(output, transport=transport)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(transport.calls), 25)
        self.assertEqual(report['sse_validation']['policy'], project.SSE_VALIDATION_POLICY)
        self.assertEqual(report['sse_validation']['version'], 'sse-two-channel-1.0')
        self.assertEqual(report['sse_validation']['channel_modes'], {'reasoning_channel_extension': 25})
        self.assertTrue(report['sse_validation']['reasoning_extension_used'])
        self.assertEqual(sorted(report['sse_validation']['frozen_sse_files_sha256']),
                         ['safe_http.py', 'sse_fidelity.py'])
        self.assertEqual(sorted({c['sse_channel_mode'] for c in report['http_captures']}),
                         ['reasoning_channel_extension'])
        record = ev.load(output / 'requests/A-001/attempt-001/validation.json')['sse_validation']
        self.assertEqual(record['channel_mode'], 'reasoning_channel_extension')
        self.assertEqual(record['removed_event_count'], 2)
        import csv
        self.assertEqual(len(list(csv.DictReader((output / 'baseline-coverage.csv').open()))), 117)
        self.assertEqual(len(list(csv.DictReader((output / 'candidate-judgments.csv').open()))), 1)
        markdown = (output / 'report.md').read_text()
        self.assertIn('sse-two-channel-1.0', markdown)
        self.assertIn('reasoning_channel_extension', markdown)
        self.assertIn('冻结safe_http.py与sse_fidelity.py字节未改', markdown)
        self.assertEqual(project.audit(output)[0]['sse_validation']['channel_modes'],
                         {'reasoning_channel_extension': 25})

    def test_plan_seals_sse_validation_policy(self):
        output = self.prepared(1)
        plan = ev.load(output / 'plan.json')
        self.assertEqual(plan['parameters']['sse_validation'], project.SSE_VALIDATION_POLICY)
        self.assertEqual(plan['version'], project.VERSION)
        self.assertEqual(plan['version'], 'project-scoped-evaluator-1.7')
        self.assertEqual(plan['schema_version'], 'scoped1.1')
        for value in ('frozen_only', None, 'sse-two-channel-1.0'):
            plan = ev.load(output / 'plan.json')
            plan['parameters']['sse_validation'] = value
            plan['fingerprint_sha256'] = ev.digest(ev.canonical(
                {k: v for k, v in plan.items() if k != 'fingerprint_sha256'}))
            (output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
            self.assertRaises(SafeFailure, project.validate_prepared, output)

    def test_plan_seals_output_budget_and_reasoning_variant(self):
        output = self.prepared(1)
        pristine = ev.load(output / 'plan.json')
        self.assertEqual(pristine['parameters'], project.sealed_parameters())
        self.assertEqual(pristine['parameters']['max_output_tokens'], project.MAX_OUTPUT_TOKENS)
        self.assertEqual(pristine['parameters']['max_output_tokens'], 2048)

        def sealed_copy():
            return copy.deepcopy(pristine)

        def rewrite(plan):
            plan['fingerprint_sha256'] = ev.digest(ev.canonical(
                {k: v for k, v in plan.items() if k != 'fingerprint_sha256'}))
            (output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')

        # The budget is a sealed parameter, not a local knob: any other value is protocol drift.
        # 32768 is the spent P1/P2/P3 rung and 8192 is the spent P4 / -006 value; neither is allowed now:
        # only 2048 keeps an over-long answer on the recoverable incomplete/max_output_tokens path.
        for value in (32768, 16384, 8192, 4096, 1024, None, '2048'):
            with self.subTest(max_output_tokens=value):
                plan = sealed_copy()
                plan['parameters']['max_output_tokens'] = value
                rewrite(plan)
                self.assertRaises(SafeFailure, project.validate_prepared, output)
        # The sealed thinking control travels in the same sealed dict: any other value (including the
        # spent 'low' rung) and an absent field (provider default) are both drift.
        self.assertEqual(pristine['parameters']['reasoning_effort'], 'none')
        for value in ('low', 'off', 'NONE', 'minimal', 'medium'):
            with self.subTest(reasoning_effort=value):
                plan = sealed_copy()
                plan['parameters']['reasoning_effort'] = value
                rewrite(plan)
                self.assertRaises(SafeFailure, project.validate_prepared, output)
        plan = sealed_copy()
        plan['parameters'].pop('reasoning_effort')
        rewrite(plan)
        self.assertRaises(SafeFailure, project.validate_prepared, output)
        # Restoring the pristine sealed plan restores validity: the drift failures above are caused by
        # the injected value, not by a broken fixture.
        rewrite(sealed_copy())
        project.validate_prepared(output)
        self.assertEqual(ev.load(output / 'plan.json'), pristine)

    def test_reasoning_off_rung_is_sealed_and_validated(self):
        data = self.data_A()
        schema = project.scoped_schema(data, self.validator.legacy_schema)
        # The sealed rung of this session: reasoning disabled, 2048 output budget, single channel.
        self.assertEqual(project.sealed_reasoning_effort(), 'none')
        self.assertEqual(project.sealed_parameters()['reasoning_effort'], 'none')
        self.assertEqual(project.sealed_parameters()['max_output_tokens'], 2048)
        payload = project.payload_for(data, schema, project.prompt_text())
        self.assertEqual(payload['reasoning'], {'effort': 'none'})
        self.assertEqual(payload['max_output_tokens'], 2048)
        # 'none' is a sealed value, not an absence: unset means the provider default (no field at all).
        with patch.object(project, 'REASONING_EFFORT', None):
            self.assertIsNone(project.sealed_reasoning_effort())
            self.assertNotIn('reasoning_effort', project.sealed_parameters())
            self.assertNotIn('reasoning', project.payload_for(data, schema, project.prompt_text()))
        for value in ('extreme', 'LOW', 'off', '', 1, 'None'):
            with self.subTest(value=value):
                with patch.object(project, 'REASONING_EFFORT', value):
                    self.assertRaises(SafeFailure, project.sealed_reasoning_effort)
                    self.assertRaises(SafeFailure, project.sealed_parameters)
                    self.assertRaises(SafeFailure, project.payload_for, data, schema, project.prompt_text())

    def test_archived_probe_gate_revalidates_deepseek_flash_offline(self):
        source = project.ev.ASSETS / 'runs/project3-space2-001/assessment-deepseek-flash-001/model-probe'
        if not source.is_dir():
            self.skipTest('archived deepseek-flash probe evidence is not present')
        import shutil
        run = self.root / 'run'
        shutil.copytree(source, run / 'model-probe')
        verdict = project.archived_probe_verdict(run, 'deepseek-flash')
        self.assertEqual(verdict['echoed_model'], 'deepseek-flash')
        self.assertEqual(verdict['http_status'], 200)
        self.assertEqual(verdict['sse_channel_mode'], 'reasoning_channel_extension')
        self.assertEqual(verdict['requests_sent'], 1)
        self.assertEqual(verdict['archived_probe_status_before_revalidation'], 'failed')
        self.assertRaises(SafeFailure, project.archived_probe_verdict, run, 'gpt-6.1-sol')
        project.check_model_probe(run, {'model': 'deepseek-flash', 'model_probe': verdict})
        self.assertRaises(SafeFailure, project.check_model_probe, run,
                          {'model': 'deepseek-flash', 'model_probe': dict(verdict, removed_event_count=0)})
        self.assertRaises(SafeFailure, project.check_model_probe, run, {'model': 'deepseek-flash'})
        capture_path = run / 'model-probe' / 'attempt-01' / 'response.safe.json'
        tampered = ev.load(capture_path)
        tampered['http_status'] = 400
        capture_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2) + '\n')
        self.assertRaises(SafeFailure, project.archived_probe_verdict, run, 'deepseek-flash')

    def real_ref(self, bid, field='description'):
        row = next(r for r in self.context['baseline'] if r['id'] == bid)
        chunks = self.validator.mod.baseline_chunks(row, field)
        return {'source_id': f'baseline:{bid}:{field}', 'quote': chunks[0][:24], 'explanation': 'mock真实引文'}

    def test_probe_records_evidence_and_seals_model(self):
        for index, (models, echo, expected) in enumerate((
                (['deepseek-flash'], 'echo', 'deepseek-flash'),
                (['deepseek-flash', 'deepseek-official/deepseek-flash'], 'fail-first',
                 'deepseek-official/deepseek-flash'),
                (['deepseek-flash', 'deepseek-official/deepseek-flash'], 'wrong', None))):
            with self.subTest(case=index):
                output = self.root / f'probe-{index}'
                ev.save(self.root / 'scenarios.json', {'scenarios': candidates(1)})
                ev.save(self.root / 'manifest.json', manifest())
                transport = ProbeTransport(echo)
                summary = project.probe_models(output, models, transport=transport)
                self.assertEqual(summary['selected_model'], expected)
                self.assertEqual(summary['status'], 'passed' if expected else 'failed')
                self.assertLessEqual(summary['requests_sent'], 2)
                self.assertTrue((output / 'model-probe' / 'probe.json').is_file())
                self.assertTrue((output / 'model-probe' / 'probe.log').is_file())
                self.assertTrue((output / 'model-probe' / 'attempt-01' / 'response.safe.json').is_file())
                self.assertNotIn('MOCK_SELECTED_KEY', (output / 'model-probe' / 'probe.log').read_text())
                if expected:
                    self.assertEqual(summary['echoed_model'], expected)
                    self.assertEqual(summary['http_status'], 200)
                    self.assertRaises(SafeFailure, project.probe_models, output, models, transport=transport)
                    spec = project.prepare(self.root / 'scenarios.json', self.root / 'manifest.json', output,
                                           model=expected)
                    self.assertEqual(spec['model'], expected)
                    self.assertEqual(spec['model_probe']['echoed_model'], expected)
                    project.validate_prepared(output, expected)
                    self.assertRaises(SafeFailure, project.prepare, self.root / 'scenarios.json',
                                      self.root / 'manifest.json', output, 5, None, None, 'other-judge-model')
                self.temp.cleanup()
                self.temp = tempfile.TemporaryDirectory()
                self.addCleanup(self.temp.cleanup)
                self.root = Path(self.temp.name)

    def test_B_smoke_failure_prevents_other_B_and_csv_row_counts(self):
        output = self.prepared(7)
        delegate = MockTransport()
        def transport(body):
            data = ev.strict_json(body['input'][1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
            if data['stage'] == 'B':
                delegate.calls.append(body)
                return {'http_status': 400, 'body': {'error': {'message': 'precise schema error'}}, 'error': None}
            return delegate(body)
        report = project.execute(output, transport=transport)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(len(delegate.calls), 25)
        self.assertFalse((output / 'requests/B-002').exists())

    def test_actual_adapter_and_tampered_scope_rejected(self):
        source = project.ev.ASSETS / 'runs/project3-space2-001/generation'
        m = ev.load(source / 'source-manifest.json')
        self.assertGreater(len(project.generation_sources(source / 'scenarios.json', source / 'source-manifest.json', m)), 80)
        import shutil
        copied = self.root / 'generation'
        shutil.copytree(source, copied)
        proposal_path = copied / 'scope-01/proposal.raw.json'
        proposal = ev.load(proposal_path)
        proposal['points'][0]['task_id'] = 999
        proposal_path.write_text(json.dumps(proposal, ensure_ascii=False))
        meta_path = copied / 'scope-01/manifest.json'
        meta = ev.load(meta_path)
        meta['proposal_sha256'] = ev.sha(proposal_path)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False))
        with self.assertRaises((SafeFailure, fetch.FetchError)):
            project.generation_sources(copied / 'scenarios.json', copied / 'source-manifest.json', m)

    def _fresh_root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def _rebound_generation(self, version):
        """Copy the frozen authoritative generation dir with a rewritten binding version.

        The binding sha/digest chain is recomputed exactly the way generate_project_scenarios.py
        computes it, so only the version string differs from the authoritative run.
        """
        import shutil
        source = project.ev.ASSETS / 'runs/project3-space2-001/generation'
        copied = self._fresh_root() / 'generation'
        shutil.copytree(source, copied)
        fingerprint = ev.load(copied / 'fingerprint.json')
        fingerprint['binding']['version'] = version
        encoded = (json.dumps(fingerprint['binding'], ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
        fingerprint['sha256'] = ev.digest(encoded)
        (copied / 'fingerprint.json').write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2) + '\n')
        manifest = ev.load(copied / 'source-manifest.json')
        manifest['fingerprint'] = fingerprint['sha256']
        (copied / 'source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        return copied, manifest

    def test_generation_version_binding_accepts_any_positive_generation(self):
        # Authoritative run stays accepted, and the counter may advance without a code change.
        for version in ('project-scenarios-1', 'project-scenarios-2', 'project-scenarios-3', 'project-scenarios-12'):
            with self.subTest(version=version):
                copied, manifest = self._rebound_generation(version)
                paths = project.generation_sources(copied / 'scenarios.json', copied / 'source-manifest.json', manifest)
                self.assertGreater(len(paths), 80)

    def test_generation_version_binding_rejects_non_generation_versions(self):
        for version in ('project-scenarios', 'project-scenarios-0', 'project-scenarios-01',
                        'project-scenarios-1x', 'project-scenarios-1-extra', 'other', '', None, 1, ['project-scenarios-1']):
            with self.subTest(version=version):
                copied, manifest = self._rebound_generation(version)
                with self.assertRaises(SafeFailure) as caught:
                    project.generation_sources(copied / 'scenarios.json', copied / 'source-manifest.json', manifest)
                self.assertEqual(str(caught.exception), 'generation_frozen_script_mismatch')

    def test_generation_version_binding_missing_key_rejected(self):
        import shutil
        source = project.ev.ASSETS / 'runs/project3-space2-001/generation'
        copied = self._fresh_root() / 'generation'
        shutil.copytree(source, copied)
        fingerprint = ev.load(copied / 'fingerprint.json')
        del fingerprint['binding']['version']
        encoded = (json.dumps(fingerprint['binding'], ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
        fingerprint['sha256'] = ev.digest(encoded)
        (copied / 'fingerprint.json').write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2) + '\n')
        manifest = ev.load(copied / 'source-manifest.json')
        manifest['fingerprint'] = fingerprint['sha256']
        (copied / 'source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        with self.assertRaises(SafeFailure) as caught:
            project.generation_sources(copied / 'scenarios.json', copied / 'source-manifest.json', manifest)
        self.assertEqual(str(caught.exception), 'generation_frozen_script_mismatch')

    def test_generation_script_sha_mismatch_still_rejected(self):
        import shutil
        source = project.ev.ASSETS / 'runs/project3-space2-001/generation'
        copied = self._fresh_root() / 'generation'
        shutil.copytree(source, copied)
        executed = copied / 'generator-executed.py'
        executed.write_bytes(executed.read_bytes() + b'# tampered frozen script\n')
        manifest = ev.load(copied / 'source-manifest.json')
        with self.assertRaises(SafeFailure) as caught:
            project.generation_sources(copied / 'scenarios.json', copied / 'source-manifest.json', manifest)
        self.assertEqual(str(caught.exception), 'generation_frozen_script_mismatch')

    def test_generation_version_binding_unit(self):
        self.assertTrue(project.generation_version_bound('project-scenarios-1'))
        self.assertTrue(project.generation_version_bound('project-scenarios-2'))
        self.assertTrue(project.generation_version_bound('project-scenarios-10'))
        self.assertTrue(project.generation_version_bound('project-scenarios-999'))
        for bad in ('project-scenarios', 'project-scenarios-', 'project-scenarios-0',
                    'project-scenarios-01', 'project-scenarios--1', 'project-scenarios-1 ', ' project-scenarios-1',
                    'other', 'project-scenarios-1x', 'project-scenario-1', 'PROJECT-SCENARIOS-1', '', None, 1, 1.5, ['x']):
            self.assertFalse(project.generation_version_bound(bad), bad)

    def test_full_reference_text_preserved(self):
        source = project.ev.ASSETS / 'runs/project3-space2-001/generation'
        preview = ev.load(source / 'document-2-preview.raw.json')
        context = project.full_context(candidates(1), {}, references=[{'source_id': 'project3-document2-rules', 'source': 'API解析原文', 'text': preview['text']}])
        self.assertEqual(len(context['reference_material'][0]['text']), 19475)
        self.assertEqual(context['reference_material'][0]['text'], preview['text'])



class TransportSentinel:
    def call(self):
        raise AssertionError


if __name__ == '__main__':
    unittest.main()
