"""v1.1.5 completed-event fidelity and bounded overload tests, no live HTTP."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import safe_http as sh
import evaluate_scenarios as ev
from test_responses_sse import event
import test_responses_sse


def fixture(texts=(("中", "文"), ("多", "块")), nonempty=False):
    model, rid = 'mock-model', 'resp_test'
    rows = [{'type': 'response.created', 'response': {'id': rid, 'model': model, 'status': 'in_progress'}}]
    output = []
    for oi, blocks in enumerate(texts):
        item_id = 'msg_' + str(oi)
        rows.append({'type': 'response.output_item.added', 'output_index': oi,
                     'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}})
        parts = []
        for ci, text in enumerate(blocks):
            fields = {'output_index': oi, 'content_index': ci, 'item_id': item_id}
            part = {'type': 'output_text', 'text': text, 'annotations': []}
            rows.append({'type': 'response.content_part.added', **fields, 'part': {**part, 'text': ''}})
            for c in text:
                rows.append({'type': 'response.output_text.delta', **fields, 'delta': c})
            rows.append({'type': 'response.output_text.done', **fields, 'text': text})
            rows.append({'type': 'response.content_part.done', **fields, 'part': part})
            parts.append(part)
        item = {'id': item_id, 'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': parts}
        rows.append({'type': 'response.output_item.done', 'output_index': oi, 'item': item})
        output.append(item)
    rows.append({'type': 'response.completed', 'response': {'id': rid, 'model': model, 'status': 'completed', 'output': output if nonempty else [], 'usage': {'input_tokens': 1, 'output_tokens': 2, 'total_tokens': 3}}})
    for i, row in enumerate(rows): row['sequence_number'] = i
    return rows


def raw(rows):
    return b''.join(event(r['type'], **{k: v for k, v in r.items() if k != 'type'}) for r in rows)


class FidelityTests(unittest.TestCase):
    def parse(self, rows):
        parser = sh.ResponsesSSE(); parser.feed(raw(rows)); parser.finish(); return parser

    def test_multiblock_indices_and_byte_fragmentation(self):
        data = raw(fixture()); parser = sh.ResponsesSSE()
        for byte in data: parser.feed(bytes([byte]))
        original = parser.finish()
        self.assertEqual(original['output'], [])
        self.assertEqual(ev.parse_content(parser.derived_response, 'responses')[0], '中文多块')
        self.assertEqual([(s['output_index'],s['content_index']) for s in parser.assembly_manifest['segments']], [(0,0),(0,1),(1,0),(1,1)])
        self.assertTrue(all(s['delta_equals_done_equals_part_equals_item'] for s in parser.assembly_manifest['segments']))

    def test_item_done_boolean_index_rejected(self):
        for value in (False, True):
            rows = fixture(texts=(("中",),))
            next(r for r in rows if r['type'] == 'response.output_item.done')['output_index'] = value
            with self.subTest(index=value), self.assertRaisesRegex(sh.SafeFailure, 'item_done_order'):
                self.parse(rows)

    def test_item_done_float_index_rejected(self):
        for value in (0.0, 1.0):
            rows = fixture(texts=(("中",),))
            next(r for r in rows if r['type'] == 'response.output_item.done')['output_index'] = value
            with self.subTest(index=value), self.assertRaisesRegex(sh.SafeFailure, 'item_done_order'):
                self.parse(rows)

    def test_item_done_negative_index_rejected(self):
        rows = fixture()
        next(r for r in rows if r['type'] == 'response.output_item.done')['output_index'] = -1
        with self.assertRaisesRegex(sh.SafeFailure, 'item_done_order'):
            self.parse(rows)

    def test_created_failed_rejected_despite_completed(self):
        rows = fixture(); rows[0]['response']['status'] = 'failed'
        with self.assertRaisesRegex(sh.SafeFailure, 'created_status'):
            self.parse(rows)

    def test_created_incomplete_and_nonactive_status_rejected(self):
        for value in ('incomplete', 'completed', 'queued', '', None):
            rows = fixture(); rows[0]['response']['status'] = value
            with self.subTest(status=value), self.assertRaisesRegex(sh.SafeFailure, 'created_status'):
                self.parse(rows)
        rows = fixture(); del rows[0]['response']['status']
        with self.assertRaisesRegex(sh.SafeFailure, 'created_status'):
            self.parse(rows)

    def test_created_nonempty_error_rejected(self):
        rows = fixture(); rows[0]['response']['error'] = {'code': 'bad'}
        with self.assertRaisesRegex(sh.SafeFailure, 'created_status'):
            self.parse(rows)

    def test_created_nonempty_incomplete_details_rejected(self):
        rows = fixture(); rows[0]['response']['incomplete_details'] = {'reason': 'length'}
        with self.assertRaisesRegex(sh.SafeFailure, 'created_status'):
            self.parse(rows)

    def test_created_native_in_progress_and_integer_done_passed(self):
        rows = fixture()
        rows[0]['response'].update(error=None, incomplete_details=None)
        parser = self.parse(rows)
        self.assertEqual(ev.parse_content(parser.derived_response, 'responses')[0], '中文多块')
        self.assertEqual([r['output_index'] for r in rows if r['type'] == 'response.output_item.done'], [0, 1])

    def test_empty_completed_missing_text_done_rejected(self):
        rows = fixture(); rows = [r for r in rows if r['type'] != 'response.output_text.done']
        for i,r in enumerate(rows):r['sequence_number']=i
        with self.assertRaises(sh.SafeFailure): self.parse(rows)

    def test_identity_sequence_and_text_conflicts(self):
        mutations = [
            lambda r:r[0]['response'].update(id='other'),
            lambda r:r[0]['response'].update(model='other'),
            lambda r:r[-1]['response'].update(id='other'),
            lambda r:r[-1]['response'].update(model='other'),
            lambda r:r[2].update(item_id='wrong'),
            lambda r:r[2].update(output_index=9),
            lambda r:r[2].update(content_index=2),
            lambda r:r[3].update(response_id='other'),
            lambda r:r[3].update(delta='wrong'),
            lambda r:r[4].update(text='wrong'),
            lambda r:r[5]['part'].update(text='wrong'),
            lambda r:r[3].update(sequence_number=20),
            lambda r:r[-1]['response'].update(error={'code':'bad'}),
            lambda r:r[-1]['response'].update(incomplete_details={'reason':'length'}),
        ]
        for change in mutations:
            rows=fixture(texts=(("中",),));change(rows)
            with self.subTest(change=mutations.index(change)), self.assertRaises(sh.SafeFailure):self.parse(rows)

    def test_duplicate_done_and_missing_terminal(self):
        rows=fixture();i=next(i for i,r in enumerate(rows) if r['type']=='response.output_text.done')
        rows.insert(i,copy.deepcopy(rows[i]))
        for j,r in enumerate(rows):r['sequence_number']=j
        with self.assertRaises(sh.SafeFailure):self.parse(rows)
        with self.assertRaisesRegex(sh.SafeFailure,'missing_completed'):self.parse(fixture()[:-1])

    def test_nonempty_consistency_no_overwrite(self):
        rows=fixture(nonempty=True);parser=self.parse(rows)
        self.assertIsNone(parser.derived_response);self.assertEqual(parser.response,rows[-1]['response'])
        rows[-1]['response']['output'][0]['content'][0]['text']='conflict'
        with self.assertRaisesRegex(sh.SafeFailure,'conflict'):self.parse(rows)

    def test_error_failed_incomplete_and_late_error(self):
        for kind in ('error','response.failed','response.incomplete'):
            rows=fixture();rows.insert(-1,{'type':kind,'error':{'code':'bad'}})
            for i,r in enumerate(rows):r['sequence_number']=i
            with self.subTest(kind=kind),self.assertRaises(sh.SafeFailure):self.parse(rows)
        rows=fixture();rows.append({'type':'error','sequence_number':len(rows),'message':'late'})
        with self.assertRaises(sh.SafeFailure):self.parse(rows)

    def test_partial_frame_utf8_and_refusal_rejected(self):
        for data in (raw(fixture())[:-1],raw(fixture())+b'\xf0'):
            with self.assertRaises(sh.SafeFailure):
                p=sh.ResponsesSSE();p.feed(data);p.finish()
        rows=fixture();rows[2]['part']['type']='refusal'
        with self.assertRaises(sh.SafeFailure):self.parse(rows)

    def test_transport_separate_original_and_derived_audit(self):
        out=test_responses_sse.SSETests().transport(raw(fixture()))
        self.assertIsNone(out['error']);self.assertEqual(out['body']['output'],[])
        self.assertIsNone(sh.inspect_sse_capture(out))
        out['derived_response']['output'][0]['content'][0]['text']='tampered'
        with self.assertRaisesRegex(sh.SafeFailure,'derived_mismatch'):sh.inspect_sse_capture(out)

    def test_overload_exact_code_is_transport_retry(self):
        bad={'http_status':200,'error':'sse_response_failed','body':{'model':'mock-model'},
             'eventmeta':{'terminal_error':{'type':'response.failed','response':{'error':{'code':'server_is_overloaded'}}}}}
        self.assertTrue(ev.retryable(bad))
        wrong=copy.deepcopy(bad);wrong['eventmeta']['terminal_error']['response']['error']['code']='invalid_request'
        self.assertFalse(ev.retryable(wrong))
        from test_evaluate_scenarios import Fake
        sample=ev.load(ev.ASSETS/'calibration_samples.json')['samples'][0]['input']
        with tempfile.TemporaryDirectory() as temp:
            fake=Fake([bad,bad,bad]);result=ev.Runner(temp,ev.Validator(),ev.PROMPT.read_text(),'mock-model',transport=fake,sleep=lambda _:None,protocol='responses').sample(sample)
            self.assertEqual(len(fake.calls),3);self.assertEqual(result['transport_retries'],2)
            self.assertEqual(result['format_repairs'],0);self.assertEqual(result['status'],'evaluation_failed')

    def test_real_eleven_capture_offline_fidelity(self):
        from recover_responses import validated_derived
        parent=ev.ASSETS/'runs/gpt61-calibration-v1.1.4-sse-040-001'
        if not parent.exists():self.skipTest('historical capture not present')
        count=0
        with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')),patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('HTTP forbidden')):
            for state in ev.load(parent/'session-001/report.json')['sample_status']:
                if state['status']=='success':continue
                sid=state['sample_id'];capture=sorted((parent/sid).glob('attempt-*/http-*.response.safe.json'))[-1]
                response=ev.load(capture)
                if response.get('error'):continue
                values,text=validated_derived(parent,sid,capture)
                self.assertTrue(values['assembly-manifest.json']['original_completed_output_empty']);self.assertTrue(text)
                count+=1
        self.assertEqual(count,11)


if __name__=='__main__':unittest.main()
