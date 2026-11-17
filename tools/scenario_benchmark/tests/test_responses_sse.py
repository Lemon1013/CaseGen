"""Native Responses SSE regressions: exclusively offline transport mocks."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import safe_http as sh
import evaluate_scenarios as ev


def native(model='mock-model'):
    return {'id': 'resp_mock', 'object': 'response', 'model': model, 'status': 'completed',
            'usage': {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30,
                      'output_tokens_details': {'reasoning_tokens': 3}},
            'output': [{'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'ignored'}]},
                       {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '{"中文":"真"}'}]}]}


def event(kind, **data):
    return ('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **data}, ensure_ascii=False) + '\n\n').encode()


def stream(body=None):
    return event('response.created', response={'status': 'in_progress'}) + event('response.output_text.delta', delta='{"中文":"假') + event('response.completed', response=body or native())


class Response:
    status = 200
    headers = {'Content-Type': 'text/event-stream; charset=utf-8', 'x-request-id': 'mock-id'}
    def __init__(self, chunks): self.chunks = list(chunks)
    def read(self, size):
        if not self.chunks: return b''
        first = self.chunks.pop(0)
        if len(first) > size:
            self.chunks.insert(0, first[size:]); first = first[:size]
        return first
    def __enter__(self): return self
    def __exit__(self, *args): pass


class SSETests(unittest.TestCase):
    def transport(self, raw, response=None, secret='mock-key-value'):
        response = response or Response([raw])
        class Opener:
            def open(self, req, timeout):
                self.req = req
                return response
        opener = Opener()
        with patch.object(sh.urllib.request, 'build_opener', return_value=opener):
            out = sh.Transport(secret, 'https://example.invalid/v1/responses')({'model': 'mock-model', 'store': False, 'stream': True, 'input': []})
        self.assertEqual(opener.req.get_header('Accept'), 'text/event-stream')
        return out

    def test_every_byte_utf8_and_crlf_split(self):
        raw = stream().replace(b'\n', b'\r\n')
        out = self.transport(raw, Response([raw[i:i+1] for i in range(len(raw))]))
        self.assertIsNone(out['error'])
        self.assertEqual(out['body'], native())
        self.assertEqual(out['eventmeta']['raw_events_text'], raw.decode())
        self.assertIsNone(sh.inspect_sse_capture(out))
        self.assertEqual(ev.parse_content(out['body'], 'responses')[0], '{"中文":"真"}')

    def test_all_two_chunk_boundaries(self):
        raw = stream()
        for i in range(len(raw) + 1):
            parser = sh.ResponsesSSE()
            parser.feed(raw[:i]); parser.feed(raw[i:])
            self.assertEqual(parser.finish(), native())

    def test_multiline_data_comments_and_cr_only(self):
        text = json.dumps({'type': 'response.completed', 'response': native()}, ensure_ascii=False, indent=2)
        raw = (': comment\revent: response.completed\r' + ''.join('data: ' + line + '\r' for line in text.splitlines()) + '\r').encode()
        out = self.transport(raw)
        self.assertIsNone(out['error']); self.assertEqual(out['body'], native())

    def test_delta_and_done_without_completed_fail(self):
        for raw in (event('response.output_text.delta', delta='{}'), b'data: [DONE]\n\n', b''):
            out = self.transport(raw)
            self.assertEqual(out['error'], 'sse_missing_completed')
            self.assertIsNone(out['body'])
            self.assertEqual(sh.inspect_sse_capture(out), 'sse_missing_completed')

    def test_failed_incomplete_error_true_events_even_http200(self):
        for kind, expected in [('response.failed', 'sse_response_failed'), ('response.incomplete', 'sse_response_incomplete'), ('error', 'sse_error_event')]:
            body = native(); body['status'] = kind.split('.')[-1]
            raw = event('response.output_text.delta', delta='{}') + event(kind, response=body, error={'code': 'upstream_error', 'message': 'actual failure'})
            out = self.transport(raw)
            self.assertEqual(out['http_status'], 200)
            self.assertEqual(out['error'], expected)
            self.assertEqual(out['eventmeta']['terminal_error']['error']['code'], 'upstream_error')
            self.assertEqual(sh.inspect_sse_capture(out), expected)

    def test_partial_completed_unterminated_eof_rejected(self):
        out = self.transport(stream().rstrip(b'\n'))
        self.assertEqual(out['error'], 'sse_unterminated_event')
        self.assertIsNone(out['body'])

    def test_corrupt_json_utf8_and_duplicate_keys(self):
        for raw, expected in [(b'data: {garbage}\n\n', 'sse_invalid_event_json'),
                              (b'data: {"type":"a","type":"b"}\n\n', 'sse_invalid_event_json'),
                              (b'data: \xff\n\n', 'invalid_utf8')]:
            self.assertEqual(self.transport(raw)['error'], expected)

    def test_terminal_response_invalid_and_late_error_rejected(self):
        body = native(); body['status'] = 'incomplete'
        self.assertEqual(self.transport(event('response.completed', response=body))['error'], 'sse_completed_response_invalid')
        self.assertEqual(self.transport(stream() + event('error', message='late failure'))['error'], 'sse_error_event')
        self.assertEqual(self.transport(stream() + event('response.completed', response=native()))['error'], 'sse_duplicate_completed')

    def test_wrong_model_and_native_usage_preserved(self):
        out = self.transport(stream(native('wrong-model')))
        with self.assertRaisesRegex(sh.SafeFailure, 'response_model_mismatch'):
            ev.validate_response_model(out['body'], 'mock-model')
        self.assertEqual(out['body']['usage'], native()['usage'])
        self.assertEqual(ev.usage_for(out['body'], 'responses')['prompt_tokens'], 10)

    def test_credential_echo_in_ignored_delta_still_redacted_before_save(self):
        out = self.transport(event('response.output_text.delta', delta='mock-key-value') + stream())
        self.assertEqual(out['error'], 'credential_echo_redacted')
        self.assertNotIn('mock-key-value', json.dumps(out))

    def test_total_raw_limit_including_ignored_events(self):
        out = self.transport(b':' + b'x' * sh.LIMIT)
        self.assertEqual(out['error'], 'body_limit_exceeded')
        self.assertLessEqual(out['captured_bytes'], sh.LIMIT)
        self.assertLessEqual(len(out['eventmeta']['raw_events_text'].encode()), sh.LIMIT)

    def test_stream_cli_responses_only_and_unmodified_input_schema(self):
        schema = {'type': 'object', 'uniqueItems': True}
        msgs = [{'role': 'user', 'content': 'same business input'}]
        body = ev.body_for('mock-model', msgs, schema, 'json_schema', protocol='responses', schema_compatibility='omit-unique-items', stream=True)
        self.assertIs(body['stream'], True); self.assertIs(body['store'], False)
        self.assertEqual(body['input'], msgs); self.assertTrue(schema['uniqueItems'])
        self.assertNotIn('uniqueItems', body['text']['format']['schema'])
        with self.assertRaisesRegex(sh.SafeFailure, 'stream_requires_responses'):
            ev.body_for('mock-model', msgs, schema, 'json_schema', stream=True)

    def test_streaming_cli_audit_resume_and_fingerprint_binding(self):
        import contextlib
        import io
        import tempfile
        from test_evaluate_scenarios import Fake, to_wire
        import audit_run
        record = ev.load(ev.ASSETS / 'calibration_samples.json')['samples'][0]
        body = native()
        body['output'][-1]['content'][0]['text'] = json.dumps(to_wire(record['expected_judgments']), ensure_ascii=False)
        out = self.transport(stream(body))
        with tempfile.TemporaryDirectory() as temp:
            args = ev.parser().parse_args(['calibrate', '--dataset', str(ev.ASSETS / 'calibration_samples.json'),
                '--sample-ids', 'S001', '--output-dir', str(Path(temp) / 'run'), '--model', 'mock-model',
                '--base-url', 'https://example.invalid', '--api-key-env', 'MOCK_KEY', '--execute',
                '--api-protocol', 'responses', '--responses-schema-compat', 'omit-unique-items', '--stream'])
            fake = Fake([out]); fake.endpoint = 'https://example.invalid/v1/responses'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ev.run(args, fake), 0)
                args.resume = True
                self.assertEqual(ev.run(args, fake), 0)
            self.assertEqual(len(fake.calls), 1)
            checked = audit_run.inspect_run(args.output_dir, {'S001': record['input']})
            self.assertEqual(checked['technical_success_count'], 1)
            self.assertEqual(checked['http_attempts'][0]['eventmeta']['completed_count'], 1)
            args.stream = False
            with self.assertRaisesRegex(sh.SafeFailure, 'resume_fingerprint_mismatch'):
                ev.run(args, fake)

    def test_sse_failure_never_json_format_repair(self):
        import tempfile
        from test_evaluate_scenarios import Fake
        record = ev.load(ev.ASSETS / 'calibration_samples.json')['samples'][0]
        out = self.transport(event('response.output_text.delta', delta='{}') + event('error', code='upstream', message='actual'))
        with tempfile.TemporaryDirectory() as temp:
            fake = Fake([out])
            result = ev.Runner(temp, ev.Validator(), ev.PROMPT.read_text(), 'mock-model', transport=fake,
                               protocol='responses', stream=True).sample(record['input'])
            self.assertEqual(result['errors'], ['sse_error_event'])
            self.assertEqual(result['format_repairs'], 0)
            self.assertEqual(len(fake.calls), 1)

    def test_archive_tamper_rejected(self):
        out = self.transport(stream()); out['body']['model'] = 'tampered'
        with self.assertRaisesRegex(sh.SafeFailure, 'sse_archive_response_mismatch'):
            sh.inspect_sse_capture(out)


if __name__ == '__main__':
    unittest.main()
