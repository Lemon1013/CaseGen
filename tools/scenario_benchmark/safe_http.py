"""Reviewed runner safety code adapted to generic CLI; selected scalar only."""
import codecs
import os, re, json, ssl, time, urllib.request, urllib.error, urllib.parse
from pathlib import Path
import yaml
LIMIT = 2 * 1024 * 1024

class SafeFailure(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Redactor:

    def __init__(self, secrets=()):
        self.secrets = list(secrets)
        self.applied = False

    def clean(self, value):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if re.fullmatch('(?i)(authorization|proxy.authorization|api.?key|password|passwd|secret|access.token|refresh.token|auth.token|credential)', str(k)):
                    out[k] = '[REDACTED_AUTH_FIELD]'
                    self.applied = True
                else:
                    out[k] = self.clean(v)
            return out
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            old = value
            for key in self.secrets:
                for spelling in {key, urllib.parse.quote(key, safe=''), json.dumps(key, ensure_ascii=False)[1:-1]}:
                    if spelling:
                        value = value.replace(spelling, '[REDACTED_SELECTED_CREDENTIAL]')
            value = re.sub('(?i)\\bBearer\\s+[A-Za-z0-9._~+/-]+=*', 'Bearer [REDACTED]', value)
            value = re.sub('(?i)((?:api[_-]?key|password|authorization|access[_-]?token|refresh[_-]?token)\\s*[:=]\\s*["\']?)[^\\s"\',;<>]+', '\\1[REDACTED]', value)
            value = re.sub('\\bsk-[A-Za-z0-9_-]{12,}\\b', '[REDACTED_KEY_PATTERN]', value)
            self.applied |= value != old
            return value
        return value

def api_url(base):
    try:
        p = urllib.parse.urlsplit(base)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or ('?' in base) or ('#' in base) or (chr(92) in base) or any((ord(c) <= 32 or ord(c) == 127 for c in base)):
            raise ValueError
        _ = p.port
        if any((x in ('.', '..') for x in urllib.parse.unquote(p.path).split('/'))):
            raise ValueError
    except ValueError:
        raise SafeFailure('unsafe_base_url') from None
    base = base.rstrip('/')
    return base if p.path.rstrip('/').endswith('/v1') else base + '/v1'

# Public configuration preflight never resolves environment or stored secret values.
def provider_public(provider='new-api', base_url=None, api_key_env=None, home=None, profile=None):
    home = Path(home) if home else Path.home()
    if base_url:
        if not api_key_env:
            raise SafeFailure('explicit_base_requires_api_key_env')
        return {'provider': provider, 'endpoint': api_url(base_url) + '/chat/completions',
                'credential_ref_name': api_key_env, 'explicit_environment': True}
    config = Path(profile) if profile else home / '.dsh/profiles/desktop/cordis.patch.yml'
    class Loader(yaml.SafeLoader):
        pass
    Loader.add_constructor('tag:yaml.org,2002:js', lambda loader, node: None)
    rows = yaml.load(config.read_text(encoding='utf-8'), Loader=Loader)
    def flatten(rows):
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    if isinstance(row.get('config'), list):
                        yield from flatten(row['config'])
                    else:
                        yield row
    norm = lambda value: re.sub('[^a-z0-9]', '', str(value).lower())
    hits = []
    for row in flatten(rows):
        config_value = row.get('config')
        if not isinstance(config_value, dict):
            continue
        for ident, value in config_value.get('providers', {}).items():
            if isinstance(value, dict) and (norm(ident) == norm(provider) or norm(value.get('displayName', '')) == norm(provider)):
                hits.append((ident, value))
    if len(hits) != 1:
        raise SafeFailure('provider_not_unique')
    ident, value = hits[0]
    ref = api_key_env or value.get('apiKeyEnv')
    if ref is not None and (not isinstance(ref, str) or not ref or any(ord(c) < 32 or ord(c) == 127 for c in ref)):
        raise SafeFailure('selected_credential_ref_invalid')
    return {'provider': ident, 'endpoint': api_url(value.get('baseURL', '')) + '/chat/completions',
            'credential_ref_name': ref, 'explicit_environment': bool(api_key_env),
            'profile_api': value.get('api') if isinstance(value.get('api'), str) else None}

# Defaults use HOME; explicit environment selection never falls back to the credential store.
def get_provider(provider='new-api', base_url=None, api_key_env=None, home=None, profile=None):
    home = Path(home) if home else Path.home()
    public = provider_public(provider, base_url, api_key_env, home, profile)
    ref = public['credential_ref_name']
    key = os.environ.get(ref) if ref else None
    source = 'explicit_environment' if base_url or api_key_env else 'selected_environment'
    if not key:
        if public['explicit_environment']:
            raise SafeFailure('explicit_environment_absent')
        if not ref:
            raise SafeFailure('selected_credential_ref_absent')
        credentials = home / '.dsh/.credentials.yaml'
        stat = credentials.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 63:
            raise SafeFailure('credential_file_not_owner_only')
        # Construct only the selected scalar, never a map of credential values.
        with credentials.open(encoding='utf-8') as handle:
            root = yaml.compose(handle)
        if not isinstance(root, yaml.MappingNode):
            raise SafeFailure('credential_document_invalid')
        version = next((v.value for k, v in root.value if k.value == 'version'), None)
        refs = next((v for k, v in root.value if k.value == 'refs'), None)
        if version != '1' or not isinstance(refs, yaml.MappingNode):
            raise SafeFailure('credential_layout_incompatible')
        hits = [v for k, v in refs.value if k.value == ref]
        if len(hits) != 1 or not isinstance(hits[0], yaml.ScalarNode):
            raise SafeFailure('selected_credential_absent_or_ambiguous')
        key = hits[0].value
        source = 'managed_selected_scalar'
        del root, refs, hits
    if not isinstance(key, str) or not key.strip() or any(ord(c) < 33 or ord(c) >= 127 for c in key):
        raise SafeFailure('selected_credential_invalid')
    endpoint = public['endpoint']
    if key in endpoint:
        raise SafeFailure('credential_in_endpoint')
    return key, endpoint, {**public, 'credential_source': source}

class ResponsesSSE:
    """Incremental UTF-8/SSE framing; only an original completed response is accepted.

    Raw event text is retained by Transport, so offline audits can reparse it.
    Delta/reasoning/tool events are never silently promoted to an assistant response.
    """
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer = ''
        self.data = []
        self.event_name = ''
        self.events = []
        self.response = None
        self.error = None
        self.terminal_error = None
        self.completed_count = 0
        self.byte_count = 0
        self.first = True
        self.done = False
        self.raw = bytearray()
        self.derived_response = None
        self.assembly_manifest = None

    def feed(self, chunk):
        self.raw.extend(chunk)
        self.byte_count += len(chunk)
        if self.byte_count > LIMIT:
            raise SafeFailure('body_limit_exceeded')
        try:
            self._text(self.decoder.decode(chunk))
        except UnicodeDecodeError:
            raise SafeFailure('invalid_utf8') from None

    def _text(self, text, final=False):
        if self.first and text:
            text = text.removeprefix('\ufeff')
            self.first = False
        self.buffer += text
        while True:
            match = re.search(r'\r\n|\r|\n', self.buffer)
            if not match or (match.group() == '\r' and match.end() == len(self.buffer) and not final):
                break
            line, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
            self._line(line)

    def _line(self, line):
        if not line:
            self._dispatch()
            return
        if line.startswith(':'):
            return
        field, _, value = line.partition(':')
        if value.startswith(' '):
            value = value[1:]
        if field == 'data':
            self.data.append(value)
        elif field == 'event':
            self.event_name = value

    def _dispatch(self):
        name, self.event_name = self.event_name, ''
        if not self.data:
            return
        text, self.data = '\n'.join(self.data), []
        if text == '[DONE]':
            self.done = True
            self.events.append({'event': name, 'data': '[DONE]'})
            return
        if self.done:
            raise SafeFailure('sse_event_after_done')
        def pairs(items):
            out = {}
            for key, value in items:
                if key in out:
                    raise ValueError('duplicate_event_key')
                out[key] = value
            return out
        try:
            event = json.loads(text, object_pairs_hook=pairs,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            raise SafeFailure('sse_invalid_event_json') from None
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise SafeFailure('sse_invalid_event_shape')
        self.events.append({'event': name, 'data': event})
        kind = event['type']
        if name and name != kind:
            raise SafeFailure('sse_event_type_mismatch')
        if kind in ('error', 'response.failed', 'response.incomplete'):
            self.error = {'error': 'sse_error_event', 'response.failed': 'sse_response_failed',
                          'response.incomplete': 'sse_response_incomplete'}[kind]
            self.terminal_error = event
            if isinstance(event.get('response'), dict):
                self.response = event['response']
        elif kind == 'response.completed':
            self.completed_count += 1
            if self.completed_count != 1:
                raise SafeFailure('sse_duplicate_completed')
            response = event.get('response')
            if not isinstance(response, dict) or response.get('status') != 'completed' or not isinstance(response.get('output'), list):
                raise SafeFailure('sse_completed_response_invalid')
            self.response = response

    def finish(self):
        try:
            self._text(self.decoder.decode(b'', final=True), final=True)
        except UnicodeDecodeError:
            raise SafeFailure('invalid_utf8') from None
        # SSE dispatch requires the blank line: EOF must not bless a partial event.
        if self.buffer or self.data or self.event_name:
            raise SafeFailure('sse_unterminated_event')
        if self.error:
            raise SafeFailure(self.error)
        if self.completed_count != 1:
            raise SafeFailure('sse_missing_completed')
        if not self.response['output'] or any(isinstance(e['data'], dict) and e['data']['type'] == 'response.output_item.added' for e in self.events):
            from sse_fidelity import assemble
            derived, manifest = assemble(self.events, self.response, bytes(self.raw).decode('utf-8'))
            if not self.response['output']:
                self.derived_response, self.assembly_manifest = derived, manifest
        return self.response


def effective_response(response):
    return response.get('derived_response', response.get('body'))


def derive_sse_capture(response):
    inspect_sse_capture(response)
    parser = ResponsesSSE()
    parser.feed(response['eventmeta']['raw_events_text'].encode('utf-8'))
    parser.finish()
    if parser.derived_response is None:
        raise SafeFailure('sse_not_empty_completed_fallback')
    return parser.derived_response, parser.assembly_manifest


def inspect_sse_capture(response):
    """Revalidate archival event framing/body; works for both success and failures."""
    meta = response.get('eventmeta')
    if not isinstance(meta, dict) or meta.get('protocol') != 'responses-sse':
        raise SafeFailure('sse_capture_missing')
    parser = ResponsesSSE()
    parse_error = None
    try:
        parser.feed(meta['raw_events_text'].encode('utf-8'))
        parser.finish()
    except SafeFailure as exc:
        parse_error = str(exc)
    if parser.events != meta['events'] or parser.completed_count != meta['completed_count'] or parser.terminal_error != meta['terminal_error']:
        raise SafeFailure('sse_archive_event_mismatch')
    if parser.response != response.get('body'):
        raise SafeFailure('sse_archive_response_mismatch')
    if 'derived_response' in response:
        if parser.derived_response != response['derived_response'] or parser.assembly_manifest != response.get('assembly_manifest'):
            raise SafeFailure('sse_archive_derived_mismatch')
    if response.get('error') is None and parse_error:
        raise SafeFailure(parse_error)
    return parse_error


class Transport:

    def __init__(self, key, endpoint):
        self.key = key
        self.endpoint = endpoint
        self.redactor = Redactor([key])

    def __call__(self, payload):
        if self.redactor.clean(payload) != payload:
            raise SafeFailure('sensitive_content_in_request')
        streaming = payload.get('stream') is True
        if streaming and not self.endpoint.endswith('/responses'):
            raise SafeFailure('stream_requires_responses_endpoint')
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(), headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json', 'Accept': 'text/event-stream' if streaming else 'application/json', 'Accept-Encoding': 'identity', 'User-Agent': 'Scenario-evaluator/1.1.5'}, method='POST')
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        tick = time.monotonic()
        raw = b''
        status = None
        ids = {}
        error = None
        parser = None
        try:
            try:
                response = opener.open(req, timeout=120)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.status
                ids = {k: response.headers.get(k) for k in ['x-request-id', 'request-id', 'x-oneapi-request-id', 'x-newapi-request-id'] if response.headers.get(k)}
                if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                    error = 'unsupported_content_encoding'
                if streaming and status == 200:
                    parser = ResponsesSSE()
                    if response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'text/event-stream':
                        error = error or 'sse_content_type_invalid'
                reader = getattr(response, 'read1', response.read)
                while not error:
                    remaining = 120 - (time.monotonic() - tick)
                    if remaining <= 0:
                        error = 'read_timeout'
                        break
                    # Real urllib HTTPS sockets use this path. Bound each blocking read
                    # by the remaining overall budget, not another full 120 seconds.
                    sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    chunk = reader(min(65536, LIMIT + 1 - len(raw)))
                    if time.monotonic() - tick > 120:
                        error = 'read_timeout'
                    if not chunk:
                        break
                    raw += chunk
                    if len(raw) > LIMIT:
                        error = 'body_limit_exceeded'
                        break
                    if parser and not error:
                        parser.feed(chunk)
                if parser and not error:
                    parser.finish()
        except SafeFailure as exc:
            error = str(exc)
        except Exception as exc:
            error = 'transport_' + type(exc).__name__
        try:
            text = raw[:LIMIT].decode('utf-8')
        except UnicodeDecodeError:
            text = raw[:LIMIT].decode('utf-8', errors='replace')
            error = error or 'invalid_utf8'
        if parser:
            body = parser.response
        else:
            try:
                body = json.loads(text)
            except (ValueError, RecursionError):
                body = text
        result = {'http_status': status, 'request_ids': ids, 'body': body, 'error': error,
                  'duration_seconds': round(time.monotonic() - tick, 6), 'captured_bytes': min(len(raw), LIMIT),
                  'redaction_applied': False,
                  'capture': 'bounded original Responses SSE events plus native final response; no request/authentication headers; redirects refused' if parser else 'bounded decoded business response; no request/authentication headers; redirects refused'}
        if parser:
            result['eventmeta'] = {'protocol': 'responses-sse', 'raw_events_text': text,
                                   'events': parser.events, 'completed_count': parser.completed_count,
                                   'terminal_error': parser.terminal_error,
                                   'assembly': 'original response.completed.response only; no delta/reasoning synthesis'}
        if parser and parser.derived_response is not None:
            result['derived_response'] = parser.derived_response
            result['assembly_manifest'] = parser.assembly_manifest
        cleaned = self.redactor.clean(result)
        if cleaned != result:
            cleaned['error'] = 'credential_echo_redacted'
            cleaned['redaction_applied'] = True
        return cleaned
