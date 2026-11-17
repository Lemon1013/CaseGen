#!/usr/bin/env python3
"""Full-context scoped A/B scenario judge. Offline prepare/audit/report; HTTP only --execute."""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import copy
import os
import re
import json
import csv
import io
import fetch_scenarios as fetch
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from jsonschema import Draft202012Validator
import evaluate_scenarios as ev
from safe_http import (SafeFailure, Transport, Redactor, ResponsesSSE, get_provider,
                       provider_public, effective_response, inspect_sse_capture)
from sse_fidelity import assemble as assemble_sse

# 1.5 = 1.4 plus one Lead-authorized sealed protocol-parameter change: the judge reasoning channel is
# disabled (reasoning_effort='none', ladder rung P4) and the output budget returned to the
# gpt-6.1-sol calibration value 8192 (1.7 lowers it again to 2048, see the 1.7 block below).
# Measured (assessment-deepseek-flash-002/stopped-truncation-report.md,
# ...-003/parameter-ladder.md, ...-004/parameter-ladder.md): deepseek-flash's SSE costs ~235 bytes per
# reasoning output token and the frozen safe_http.py capture LIMIT is 2 MiB, so ~8.9k output tokens is
# the hard ceiling of any single capture; the reasoning channel consumed 5.1k-7.4k of the archived
# 8.4k-token answers and 10 of 29 P3 attempts died at body_limit_exceeded (2,097,152 bytes). Raising the
# budget is strictly worse (an unrecoverable body_limit_exceeded instead of a recoverable
# max_output_tokens truncation), so the only lever left is the reasoning channel itself. With
# effort='none' the provider emits a single assistant channel: the archived probe re-runs through the
# frozen assembler natively, and the observed answer payload is ~0.5k-2.1k tokens per 5-target block.
# 1.6 = 1.5 plus one repair-quality fix, no business/protocol/validation change: session -006 produced
# 112/117 gold rows (23 of 46 attempts needed a bounded format repair) but its A-018 exhausted the
# sealed 3-generation budget because the machine error handed back was the bare token
# `coverage_evidence_required` - no row, no rule - so the model re-emitted a byte-identical wire twice.
# validate_scoped now attributes every row/group-level error to its exact JSON location and restates the
# frozen rule, and the scoped prompt restates the required top-level keys (request_id is the most
# frequently omitted one). Same model, same rung, same wire schema, same gates, same repair budget.
# 1.7 = 1.6 plus two further Lead-authorized (18:00) changes that attack the same observed losses
# without relaxing any business/source/quote/review gate:
#   (a) root container-field normalization. `schema_version`, `input_id`, `request_id` and
#       `evaluation_status` are redundant echo copies of sealed request/plan values, never business
#       content. -006 lost 12 attempts to `scoped_schema_invalid: <root>: 'request_id' is a required
#       property`, so a field that is ABSENT or null is now filled from the sealed values before schema
#       validation; the fill is recorded per attempt (`container_fields_filled` in status.json and
#       validation.json) and disclosed in report.md/report.json. A field that is present but WRONG is
#       still rejected by the same `const` check - no silent overwrite of a wrong value.
#   (b) format-repair budget 3 -> 8 generations (1 initial + up to 7 bounded repairs); transport retries
#       stay <= 2. -006's A-018 re-emitted a byte-identical wire on both repair turns and exhausted the
#       sealed 3-generation budget, which permanently stopped the whole B stage.
#   (c) Lead decision 18:05: max_output_tokens 8192 -> 2048. -006 shows ~895 SSE bytes per output token
#       with the reasoning channel off, so the frozen 2 MiB capture LIMIT is hit at ~2,340 output tokens
#       and that failure is unrecoverable; 2048 keeps over-long answers on the recoverable
#       `incomplete/max_output_tokens` path that the sealed one-time 3+2 split can absorb.
# The wire structure (scoped1.1), the sealed model/rung and every local gate are unchanged; no model
# switch and no business-label preselection.
VERSION = 'project-scoped-evaluator-1.7'
SCHEMA_VERSION = 'scoped1.1'
# The formal judge model is a sealed plan parameter; these are only probe/default candidates.
DEFAULT_MODEL = 'deepseek-flash'
PROBE_MODELS = ('deepseek-flash', 'deepseek-official/deepseek-flash')
CATEGORIES = ['AUC', 'AMT', 'ACC', 'OPN', 'CLS', 'CAN']
INVENTORY = dict(zip(CATEGORIES, [19, 14, 13, 21, 24, 26]))
PROMPT = Path(__file__).with_name('prompts') / 'evaluator_scoped_v1.md'
MAX_REQUEST_BYTES = 1024 * 1024
MAX_TARGETS = 5
# Lead-authorized (18:00) container normalization. These four root fields are redundant echoes of the
# sealed request/plan values, not business content: -006 lost 12 attempts to
# `scoped_schema_invalid: <root>: 'request_id' is a required property`. A field that is absent or null
# is filled from the sealed values before schema validation (and the fill is recorded + disclosed); a
# field that is present but wrong still fails the schema's `const` check exactly as before.
CONTAINER_FIELDS = ('schema_version', 'input_id', 'request_id', 'evaluation_status')
CONTAINER_EVALUATION_STATUS = 'complete'
# Lead-authorized (18:00) repair budget: one target may use up to GENERATIONS_MAX generations
# (1 initial + up to 7 bounded format repairs). -006's A-018 exhausted the sealed 3-generation budget
# by re-emitting a byte-identical wire twice. Transport retries stay <= 2, unchanged.
GENERATIONS_MAX = 8
FORMAT_REPAIRS_MAX = GENERATIONS_MAX - 1
TRANSPORT_RETRIES_MAX = 2
# Sealed judge output budget (Lead decision 18:05). It must stay a sealed plan parameter: every request
# is built from this value and validate_prepared recomputes it exactly. With reasoning off, the -006
# captures measure ~895 bytes of SSE per output token, so the frozen safe_http.py 2 MiB capture LIMIT is
# reached at ~2,340 output tokens - and hitting it is UNRECOVERABLE (`body_limit_exceeded`, no
# `incomplete/max_output_tokens`, so the sealed one-time split cannot rescue the block). 2048 keeps every
# over-long answer inside the recoverable path: the model truncates with reason=max_output_tokens and the
# sealed one-time 3+2 split regenerates the halves. Raising the budget is strictly worse here.
MAX_OUTPUT_TOKENS = 2048
# Sealed judge thinking control (Lead-authorized ladder rung P4): 'none' is the only value that keeps
# deepseek-flash inside the frozen 2 MiB capture LIMIT. Verified on this exact gateway: effort='low'
# still emits a reasoning channel (136 reasoning tokens on a trivial probe), effort='none' emits none.
# Any other value would be a different rung and is refused by sealed_reasoning_effort.
REASONING_EFFORT = 'none'
REASONING_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high')
MODEL_ID_PATTERN = r'[A-Za-z0-9][A-Za-z0-9._/:-]{0,100}'
# Only read-only prior evidence of this campaign may predate prepare; every judging artifact is refused.
PREEXISTING_RUN_ENTRIES = {'model-probe', 'model-probe.console.log', 'model-probe-round1-invalid-schema',
                           'tests-original.log', 'tests-scoped.log', 'aborted-previous-session.md',
                           'model-probe-failure.md', 'tests-original-final.log', 'tests-scoped-final.log'}
# Sealed SSE validation policy: frozen carrier assembly, plus the strictly scoped two-channel extension.
SSE_VALIDATION_POLICY = 'frozen_assemble_v1_or_reasoning_channel_extension_1.0'
SSE_TWO_CHANNEL_VERSION = 'sse-two-channel-1.0'
REASONING_PART_EVENTS = ('response.content_part.added', 'response.content_part.done')
TWO_CHANNEL_EVENT_TYPES = frozenset((
    'response.created', 'response.in_progress', 'response.output_item.added', 'response.output_item.done',
    'response.content_part.added', 'response.content_part.done', 'response.output_text.delta',
    'response.output_text.done', 'response.reasoning_text.delta', 'response.reasoning_text.done',
    'response.completed'))
PROBE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['ok'],
                'properties': {'ok': {'type': 'boolean'}}}
PROBE_MAX_REQUESTS = 2
_CHECKED_SCHEMAS = set()
SCOPES = ('集合竞价撮合成交', '单笔成交金额', '累计成交金额', '开盘价', '收盘价', '撤单订单类型验证')


def require(condition, error):
    if not condition:
        raise SafeFailure(error)


def require_model_id(value, error='judge_model_invalid'):
    require(isinstance(value, str) and bool(re.fullmatch(MODEL_ID_PATTERN, value)), error)
    return value


# The frozen generation binding is a positive int generation number, not the literal '-1': the authoritative
# run bound 'project-scenarios-1' while the current sampler already ships 'project-scenarios-2', so an
# exact-string check would refuse every new generation with generation_frozen_script_mismatch. Only the
# generator IDENTIFIER stays frozen; the counter is allowed to advance. The frozen script sha, the
# map_scenarios hash, the doc2/8-page/34-chunk binding, the fingerprint digest, the payload identity and
# the scope identity checks around it are untouched, so this relaxes the version string only.
GENERATION_VERSION_PATTERN = r'project-scenarios-[1-9][0-9]*'


def generation_version_bound(value):
    """True only for a present 'project-scenarios-N' string with a positive integer N."""
    return isinstance(value, str) and bool(re.fullmatch(GENERATION_VERSION_PATTERN, value))


def empty_object_paths(node, path=''):
    """JSON-pointer-ish paths of object nodes that declare no properties at all.

    Several provider-side strict-schema validators reject such objects outright
    (`An object with no properties is not allowed.`), so the scoped wire schema must never
    contain one; the local validator keeps the same prohibition as a hard requirement.
    """
    found = []
    if isinstance(node, dict):
        if isinstance(node.get('properties'), dict) and not node['properties']:
            found.append(path or '/')
        for key, value in node.items():
            found += empty_object_paths(value, path + '/' + str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += empty_object_paths(value, path + f'[{index}]')
    return found


def save_once(path, value, text=False):
    """Archive a derived attempt artifact; an existing identical file is reused, never rewritten."""
    path = Path(path)
    content = value if text else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if path.exists():
        require(path.is_file() and not path.is_symlink() and path.read_text(encoding='utf-8') == content,
                'archived_attempt_artifact_changed')
        return
    ev.save(path, value, text=text)


def strict_assemble_capture(response):
    """Frozen sse_fidelity strict carrier assembly over the archived evidence.

    Must raise nothing for a successful capture and derive exactly effective_response(response),
    including when the original completed output is non-empty.
    """
    meta = response.get('eventmeta')
    require(isinstance(meta, dict) and meta.get('protocol') == 'responses-sse', 'sse_capture_missing')
    raw = meta['raw_events_text']
    parser = ResponsesSSE()
    parser.feed(raw.encode('utf-8'))
    parser.finish()
    body = response.get('body')
    require(isinstance(body, dict), 'sse_archive_response_missing')
    derived, manifest = assemble_sse(parser.events, body, raw)
    native = effective_response(response)
    require(derived == native, 'sse_assembled_response_mismatch')
    if 'derived_response' in response:
        require(response['derived_response'] == derived and response.get('assembly_manifest') == manifest,
                'sse_archive_derived_mismatch')
    return derived, manifest


def two_channel_require(condition, reason):
    if not condition:
        raise SafeFailure('sse_two_channel_' + reason)


def capture_error_acceptable(response):
    """A capture error is acceptable only when the sealed validation proves it is the frozen
    fidelity failure of the reasoning channel; anything else stays a technical failure."""
    error = response.get('error')
    return error is None or (isinstance(error, str) and error.startswith('sse_fidelity_'))


def parse_archived_sse(response):
    """Reparse archived raw SSE bytes with the frozen parser, exactly like inspect_sse_capture.

    Archived bytes are never rewritten; this is a read-only derived view for validation.
    """
    meta = response.get('eventmeta')
    require(isinstance(meta, dict) and meta.get('protocol') == 'responses-sse', 'sse_capture_missing')
    raw = meta.get('raw_events_text')
    require(isinstance(raw, str) and bool(raw), 'sse_raw_events_missing')
    parser = ResponsesSSE()
    parse_error = None
    try:
        parser.feed(raw.encode('utf-8'))
        parser.finish()
    except SafeFailure as exc:
        parse_error = str(exc)
    return parser, raw, parse_error


def filter_reasoning_content_parts(events):
    """Remove ONLY content_part.added|done owned by a non-message (reasoning) item.

    Every removed event must be a reasoning_text content part of a reasoning item that owns it;
    anything else is a technical failure rather than a silent filter.
    """
    owners = {}
    for event in events:
        data = event.get('data')
        if isinstance(data, dict) and data.get('type') == 'response.output_item.added':
            owners[data.get('output_index')] = data.get('item') if isinstance(data.get('item'), dict) else {}
    kept, removed = [], []
    for event in events:
        data = event.get('data')
        if isinstance(data, dict) and data.get('type') in REASONING_PART_EVENTS:
            owner = owners.get(data.get('output_index'))
            if isinstance(owner, dict) and owner.get('type') != 'message':
                two_channel_require(owner.get('type') == 'reasoning', 'filter_owner_not_reasoning')
                two_channel_require((data.get('part') or {}).get('type') == 'reasoning_text',
                                    'filter_part_not_reasoning_text')
                two_channel_require(data.get('item_id') == owner.get('id'), 'filter_owner_identity_mismatch')
                removed.append(event)
                continue
        kept.append(event)
    return kept, removed


def reproduce_capture_error(response, events, raw):
    """The recorded non-success error must be the frozen fidelity failure of these exact bytes."""
    error = response.get('error')
    require(isinstance(error, str) and error.startswith('sse_fidelity_'), 'sse_unexpected_capture_error')
    body = response.get('body')
    require(isinstance(body, dict), 'sse_archive_response_missing')
    reproduced = None
    try:
        assemble_sse(events, body, raw)
    except SafeFailure as exc:
        reproduced = str(exc)
    require(reproduced == error, 'sse_recorded_fidelity_error_not_reproducible')
    return error


def assistant_signature(payload):
    """Ordered assistant message signature: identity plus every output part verbatim."""
    return [(item.get('id'), item.get('role'), item.get('status'),
             [(part.get('type'), part.get('text')) for part in (item.get('content') or [])])
            for item in (payload.get('output') or [])
            if isinstance(item, dict) and item.get('type') == 'message' and item.get('role') == 'assistant']


def validate_reasoning_channel(events, assistant_texts):
    """Equal-rigour lifecycle validation of the withheld reasoning channel over ORIGINAL events."""
    rows = [e['data'] for e in events if isinstance(e.get('data'), dict)]
    two_channel_require(bool(rows), 'empty_events')
    two_channel_require(all(type(e.get('sequence_number')) is int for e in rows), 'sequence_type')
    two_channel_require([e['sequence_number'] for e in rows] == list(range(len(rows))), 'sequence_gap')
    two_channel_require(rows[0].get('type') == 'response.created', 'created_order')
    two_channel_require(sum(e['type'] == 'response.created' for e in rows) == 1, 'duplicate_created')
    two_channel_require(sum(e['type'] == 'response.completed' for e in rows) == 1
                        and rows[-1].get('type') == 'response.completed', 'terminal_order')
    for row in rows:
        two_channel_require(row['type'] not in ('error', 'response.failed', 'response.incomplete'), 'error_event')
        two_channel_require(row['type'] in TWO_CHANNEL_EVENT_TYPES, 'unknown_event_type:' + str(row['type']))
    items, reasoning_index, segments = {}, set(), {}
    for row in rows:
        if row['type'] != 'response.output_item.added':
            continue
        item = row.get('item') if isinstance(row.get('item'), dict) else {}
        output_index = row.get('output_index')
        two_channel_require(type(output_index) is int and output_index == len(items), 'output_index')
        if item.get('type') == 'reasoning':
            two_channel_require(item.get('content') == [] and item.get('status') == 'in_progress',
                                'reasoning_item_added_shape')
            reasoning_index.add(output_index)
        items[output_index] = {'added': item, 'done': None, 'parts': []}
    for row in rows:
        kind = row['type']
        if kind == 'response.output_item.done':
            output_index = row.get('output_index')
            two_channel_require(type(output_index) is int and output_index in items
                                and items[output_index]['done'] is None, 'item_done_order')
            item = row.get('item') if isinstance(row.get('item'), dict) else {}
            added = items[output_index]['added']
            two_channel_require(item.get('id') == added.get('id') and item.get('type') == added.get('type'),
                                'item_done_identity')
            items[output_index]['done'] = item
            continue
        if kind not in ('response.content_part.added', 'response.content_part.done', 'response.output_text.delta',
                        'response.output_text.done', 'response.reasoning_text.delta', 'response.reasoning_text.done'):
            continue
        output_index, content_index = row.get('output_index'), row.get('content_index')
        if output_index not in reasoning_index:
            continue  # assistant channel: proven by the frozen assembler over the projection
        record = items[output_index]
        two_channel_require(record['done'] is None, 'reasoning_event_after_item_done')
        two_channel_require(row.get('item_id') == record['added'].get('id'), 'reasoning_item_identity')
        two_channel_require(type(content_index) is int and content_index >= 0, 'reasoning_content_index')
        key = (output_index, content_index)
        segment = segments.get(key)
        if kind == 'response.content_part.added':
            part = row.get('part') if isinstance(row.get('part'), dict) else {}
            two_channel_require(content_index == len(record['parts']) and segment is None, 'reasoning_part_added_order')
            two_channel_require(part.get('type') == 'reasoning_text' and part.get('text') == '',
                                'reasoning_part_added_shape')
            record['parts'].append(content_index)
            segments[key] = {'deltas': [], 'done': False, 'part_done': False, 'text': None}
        elif kind == 'response.reasoning_text.delta':
            two_channel_require(segment is not None and not segment['done'] and not segment['part_done'],
                                'reasoning_delta_order')
            two_channel_require(isinstance(row.get('delta'), str), 'reasoning_delta_shape')
            segment['deltas'].append(row['delta'])
        elif kind == 'response.reasoning_text.done':
            two_channel_require(segment is not None and not segment['done'] and not segment['part_done'],
                                'reasoning_done_order')
            two_channel_require(isinstance(row.get('text'), str), 'reasoning_done_shape')
            two_channel_require(''.join(segment['deltas']) == row['text'], 'reasoning_delta_done_conflict')
            segment['done'], segment['text'] = True, row['text']
        elif kind == 'response.content_part.done':
            part = row.get('part') if isinstance(row.get('part'), dict) else {}
            two_channel_require(segment is not None and segment['done'] and not segment['part_done'],
                                'reasoning_part_done_order')
            two_channel_require(part.get('type') == 'reasoning_text' and part.get('text') == segment['text'],
                                'reasoning_part_done_conflict')
            segment['part_done'] = True
        else:
            two_channel_require(False, 'reasoning_item_assistant_event')
    texts = []
    for output_index in sorted(reasoning_index):
        record = items[output_index]
        two_channel_require(record['done'] is not None, 'reasoning_item_unfinished')
        two_channel_require(record['done'].get('status') == 'completed', 'reasoning_item_not_completed')
        two_channel_require(bool(record['parts']), 'reasoning_item_without_part')
        parts = [segments[(output_index, index)] for index in record['parts']]
        two_channel_require(all(p['done'] and p['part_done'] for p in parts), 'reasoning_segment_unfinished')
        final = [(p.get('type'), p.get('text')) for p in (record['done'].get('content') or [])]
        two_channel_require(final == [('reasoning_text', p['text']) for p in parts], 'reasoning_item_content_conflict')
        texts.extend(p['text'] for p in parts)
    two_channel_require(all(oi in reasoning_index for oi, _ in segments), 'orphan_reasoning_segment')
    for text in texts:
        if text:
            two_channel_require(all(text not in value for value in assistant_texts),
                                'reasoning_text_inside_assistant_output')
    return texts


def strict_two_channel_assemble(response):
    """Sealed `sse-two-channel-1.0` validation over archived raw SSE evidence.

    Pristine captures (no error) use the frozen assembler only, with no filtering at all. A capture
    whose sole frozen fidelity failure is the reasoning channel is validated as: (1) the frozen
    assembler over the same event list with ONLY the reasoning-owned content_part.added|done removed
    (sequence numbers re-projected to a contiguous carrier index, because the frozen assembler
    requires exactly that) must succeed and must reproduce the completed assistant message exactly;
    (2) the withheld reasoning channel must satisfy the full lifecycle and global sequence checks.
    """
    parser, raw, parse_error = parse_archived_sse(response)
    body = response.get('body')
    require(isinstance(body, dict), 'sse_archive_response_missing')
    events = parser.events
    manifest = {'validation_version': SSE_TWO_CHANNEL_VERSION, 'frozen_files_unchanged': True}
    if response.get('error') is None:
        require(parse_error is None, 'sse_pristine_capture_parse_error')
        derived, frozen = strict_assemble_capture(response)
        original_output = effective_response(response).get('output')
        manifest.update({'channel_mode': 'pristine_frozen_assemble', 'frozen_assembler_version': frozen.get('version'),
                         'recorded_capture_error': None, 'removed_event_count': 0, 'removed_events_sha256': None,
                         'removed_event_sequence_numbers': [], 'reasoning_item_count': 0, 'reasoning_text_sha256': [],
                         'assistant_channel_sha256': ev.digest(ev.canonical(assistant_signature(derived))),
                         'original_completed_output_empty': not original_output, 'frozen_assemble_manifest': frozen})
        return derived, manifest
    error = reproduce_capture_error(response, events, raw)
    two_channel_require(parse_error == error, 'reparse_error_not_equal_recorded_error')
    kept, removed = filter_reasoning_content_parts(events)
    two_channel_require(bool(removed), 'no_reasoning_content_part_to_filter')
    two_channel_require(len(kept) + len(removed) == len(events), 'filter_accounting')
    projected, original_sequences = copy.deepcopy(kept), []
    for index, event in enumerate(projected):
        original_sequences.append(event['data']['sequence_number'])
        event['data']['sequence_number'] = index
    two_channel_require(original_sequences == sorted(set(original_sequences)) and bool(original_sequences),
                        'projection_not_order_preserving')
    derived, frozen = assemble_sse(projected, body, raw)
    native = effective_response(response)
    require(isinstance(native, dict), 'sse_archive_response_missing')
    original_output = native.get('output')
    two_channel_require(isinstance(original_output, list) and bool(original_output), 'completed_body_without_output')
    two_channel_require(bool(assistant_signature(derived)), 'assistant_channel_empty')
    two_channel_require(assistant_signature(derived) == assistant_signature(native),
                        'assistant_channel_conflict_with_completed_body')
    reasoning_texts = validate_reasoning_channel(
        events, [text for _, _, _, parts in assistant_signature(derived) for _, text in parts])
    manifest.update({'channel_mode': 'reasoning_channel_extension', 'frozen_assembler_version': frozen.get('version'),
                     'recorded_capture_error': error, 'removed_event_count': len(removed),
                     'removed_events_sha256': ev.digest(ev.canonical(
                         [[e['data'].get('type'), e['data'].get('sequence_number')] for e in removed])),
                     'removed_event_sequence_numbers': [e['data'].get('sequence_number') for e in removed],
                     'reasoning_item_count': sum(1 for event in events if isinstance(event.get('data'), dict)
                                                 and event['data'].get('type') == 'response.output_item.added'
                                                 and (event['data'].get('item') or {}).get('type') == 'reasoning'),
                     'reasoning_text_sha256': [ev.digest(text) for text in reasoning_texts],
                     'assistant_channel_sha256': ev.digest(ev.canonical(assistant_signature(derived))),
                     'original_completed_output_empty': False,
                     'projection': {'reason': 'frozen_assemble_requires_contiguous_sequence_numbers',
                                    'projected_event_count': len(projected),
                                    'original_sequence_numbers_sha256': ev.digest(ev.canonical(original_sequences)),
                                    'original_last_sequence_number': original_sequences[-1]},
                     'frozen_assemble_manifest': frozen})
    return derived, manifest


def sse_validation_record(manifest):
    """Small disclosure record archived in validation.json and surfaced in the report."""
    if not manifest:
        return None
    return {key: manifest.get(key) for key in
            ('validation_version', 'channel_mode', 'recorded_capture_error', 'removed_event_count',
             'removed_events_sha256', 'reasoning_item_count', 'reasoning_text_sha256',
             'assistant_channel_sha256', 'frozen_assembler_version')}


def verify_sse_capture(response):
    """Archival framing revalidation plus the sealed strict two-channel assembly."""
    inspect_sse_capture(response)
    return strict_two_channel_assemble(response)


def prompt_text():
    # Original policy is frozen; scoped prefix explicitly overrides only its output protocol.
    return PROMPT.read_text() + '\n\n--- 原完整业务政策（输出协议由上文替代） ---\n' + ev.PROMPT.read_text()


def full_context(scenarios, provenance, input_id='project3-space2-001', references=None):
    rows = ev.load(ev.GOLD)['scenarios']
    require(Counter(r['category'] for r in rows) == Counter(INVENTORY), 'gold_inventory_mismatch')
    require(len(scenarios) > 0, 'candidate_count_must_be_positive')
    context = {'input_id': input_id, 'baseline': rows, 'generated_scenarios': copy.deepcopy(scenarios),
               'reference_material': references or [], 'task_scope': {'category_ids': CATEGORIES,
               'baseline_ids': [r['id'] for r in rows], 'reference_scope': 'full_baseline',
               'novelty_scope': '完整117参考未命中仍需业务新增核查；不是法规有效性证明'},
               'generation_provenance': copy.deepcopy(provenance)}
    ev.Validator().input(context)
    return context


def target_plan(context, size=5):
    require(1 <= size <= MAX_TARGETS, 'target_size_out_of_bounds')
    plan = []
    for stage, field in [('A', 'baseline'), ('B', 'generated_scenarios')]:
        ids = [r['id'] for r in context[field]]
        for start in range(0, len(ids), size):
            plan.append({'stage': stage, 'request_id': f'{stage}-{start // size + 1:03d}',
                         'targets': ids[start:start + size]})
    return plan


def request_data(context, target, a_mapping=None):
    data = {'context': context, **target}
    if target['stage'] == 'B':
        require(a_mapping is not None, 'B_requires_complete_A_mapping')
        data['a_mapping'] = a_mapping
    return data


def inverse_mapping(context, accepted_A):
    expected = [r['id'] for r in context['baseline']]
    rows, origins = {}, {}
    for entry in accepted_A:
        # Only real A rows feed the graph; a B or foreign entry is a programming error, not data.
        require(entry['origin']['stage'] == 'A', 'inverse_mapping_requires_real_A_rows')
        for bid, row in entry['wire']['baseline_judgments'].items():
            require(bid not in rows, 'duplicate_A_baseline')
            rows[bid] = row
            origins[bid] = entry['origin']
    require(set(rows) == set(expected), 'A_not_complete_no_B_or_global_new')
    reverse = {r['id']: [] for r in context['generated_scenarios']}
    for bid in expected:
        for gid in rows[bid]['matched_generated_ids']:
            require(gid in reverse, 'A_unknown_candidate_edge')
            reverse[gid].append(bid)
    return {'origin': 'deterministic_reverse_of_real_A_edges_not_independent_blind_judgment',
            'candidate_to_baseline': reverse,
            'baseline_judgments': [{'judgment': rows[bid], 'source': origins[bid]} for bid in expected]}


def scoped_schema(data, legacy):
    context = data['context']
    schema = copy.deepcopy(legacy)
    schema.pop('$id', None)
    schema.pop('$schema', None)
    schema['title'] = SCHEMA_VERSION
    props, defs = schema['properties'], schema['$defs']
    props['schema_version'] = {'type': 'string', 'const': SCHEMA_VERSION}
    props['input_id'] = {'type': 'string', 'const': context['input_id']}
    props['request_id'] = {'type': 'string', 'const': data['request_id']}
    schema['required'].append('request_id')
    baseline_fields = ev.Validator().mod.BASELINE_FIELDS
    external = [f"baseline:{r['id']}:{f}" for r in context['baseline']
                for f in baseline_fields]
    external += [r['source_id'] for r in context.get('reference_material', [])
                 if not r['source_id'].startswith('generated:')]
    internal = [f"generated:{r['id']}:text" for r in context['generated_scenarios']]
    # Shared enums: full 117 references appear once, never expanded per row/branch.
    defs['external_source_id'] = {'type': 'string', 'pattern': '^(?:' + '|'.join(re.escape(i) for i in sorted(set(external))) + ')$'}
    defs['internal_source_id'] = {'type': 'string', 'pattern': '^(?:' + '|'.join(re.escape(i) for i in sorted(set(internal))) + ')$'}
    defs['conflict_source_id'] = {'anyOf': [{'$ref': '#/$defs/external_source_id'}, {'$ref': '#/$defs/internal_source_id'}]}
    for name, source in [('external_ref', 'external_source_id'), ('conflict_ref', 'conflict_source_id')]:
        defs[name] = copy.deepcopy(defs['source_ref'])
        defs[name]['properties']['source_id'] = {'$ref': '#/$defs/' + source}
    stage_field = nonstage_field = None
    for field, definition, idfield, stage in [
            ('baseline_judgments', 'baseline_judgment', 'baseline_id', 'A'),
            ('generated_judgments', 'generated_judgment', 'generated_id', 'B')]:
        stage_field = field if data['stage'] == stage else stage_field
        nonstage_field = field if data['stage'] != stage else nonstage_field
        keys = {}
        if data['stage'] == stage:
            for target in data['targets']:
                row = copy.deepcopy(defs[definition])
                row['properties'][idfield] = {'type': 'string', 'const': target}
                if stage == 'B':
                    links = data['a_mapping']['candidate_to_baseline'][target]
                    row['properties']['matched_baseline_ids'] = {'type': 'array', 'const': links,
                                                                'items': {'type': 'string'}, 'uniqueItems': True}
                    row['properties']['relation'] = {'type': 'string', 'enum':
                        ['matched', 'extension', 'unresolved'] if links else ['new', 'unresolved', 'out_of_scope']}
                branches = []
                for conflict in (False, True):
                    branch = copy.deepcopy(row)
                    branch['properties']['business_consistency'] = ({'type': 'string', 'const': 'conflict'}
                        if conflict else {'type': 'string', 'enum': ['consistent', 'uncertain', 'not_assessed']})
                    branch['properties']['source_refs'] = {'type': 'array', 'items':
                        {'$ref': '#/$defs/' + ('conflict_ref' if conflict else 'external_ref')}}
                    branches.append(branch)
                keys[target] = {'anyOf': branches}
        props[field] = {'type': 'object', 'properties': keys, 'required': list(keys), 'additionalProperties': False}
    # scoped1.1 (authorized L1 fix): only the current stage's judgment map is declared. The non-stage
    # field is deleted from properties and required, so the top-level additionalProperties=false rejects
    # its presence - strictly stronger than the previous "must be present but an empty object" shape.
    require(stage_field is not None and nonstage_field is not None, 'unknown_stage')
    props.pop(nonstage_field, None)
    schema['required'] = [name for name in schema['required'] if name != nonstage_field]
    if data['stage'] == 'A':
        props['duplicate_groups']['maxItems'] = 0
    else:
        # Namespace group IDs by this block's request_id so cross-block collisions cannot pass.
        defs['duplicate_group']['properties']['group_id'] = {
            'type': 'string', 'pattern': '^' + re.escape(data['request_id']) + r':[A-Za-z0-9_.:-]{1,64}$'}
    review = copy.deepcopy(defs['review_item'])
    review['properties']['target_type'] = {'type': 'string', 'const': 'baseline' if data['stage'] == 'A' else 'generated'}
    review['properties']['target_id'] = {'type': 'string', 'enum': data['targets']}
    props['review_queue'] = {'type': 'array', 'items': review}
    def typed(node):
        if isinstance(node, dict):
            if ('const' in node or 'enum' in node) and 'type' not in node:
                node['type'] = 'string'
            for value in node.values():
                typed(value)
        elif isinstance(node, list):
            for value in node:
                typed(value)
    typed(schema)
    # Strict wire schema must never declare an empty-properties object: some provider-side strict
    # validators (the deepseek-flash channel) reject the whole block otherwise. Enforced locally.
    require(not empty_object_paths(schema), 'wire_schema_empty_object_forbidden')
    schema_hash = ev.digest(ev.canonical(schema))
    if schema_hash not in _CHECKED_SCHEMAS:
        Draft202012Validator.check_schema(schema)
        _CHECKED_SCHEMAS.add(schema_hash)
    return schema


def machine_error(error):
    """Render one jsonschema error so the repair prompt names the exact location and failure.

    A missing top-level key has an empty `absolute_path`; rendering it as `<root>` (instead of an empty
    string) keeps the machine error unambiguous for the bounded format-repair generation, which is told
    to fix exactly these errors and nothing else in the business labels.
    """
    where = '/'.join(map(str, error.absolute_path)) or '<root>'
    return where + ': ' + error.message


def validate_scoped(data, wire, schema, validator=None):
    validator = validator or ev.Validator()
    context = data['context']
    validator.input(context)
    require(data['stage'] in ('A', 'B'), 'unknown_stage')
    allowed = {r['id'] for r in context['baseline' if data['stage'] == 'A' else 'generated_scenarios']}
    require(1 <= len(data['targets']) <= MAX_TARGETS and len(set(data['targets'])) == len(data['targets'])
            and set(data['targets']) <= allowed, 'invalid_stage_targets')
    errors = list(Draft202012Validator(schema).iter_errors(wire))
    require(not errors, 'scoped_schema_invalid: ' + '; '.join(machine_error(e) for e in errors[:8]))
    # scoped1.1: the wire carries exactly this stage's judgment map. The non-stage field must be
    # absent (the schema already forbids it via additionalProperties=false; asserted again locally).
    judgment_field, kind, idfield = (('baseline_judgments', 'baseline', 'baseline_id') if data['stage'] == 'A'
                                     else ('generated_judgments', 'generated', 'generated_id'))
    nonstage = 'generated_judgments' if data['stage'] == 'A' else 'baseline_judgments'
    require(nonstage not in wire, 'non_stage_judgment_field_present')
    judgments = wire.get(judgment_field)
    require(isinstance(judgments, dict), 'stage_judgment_map_incomplete: ' + judgment_field
            + ' must be an object keyed by exactly these target IDs: ' + ', '.join(map(str, data['targets'])))
    missing = [i for i in data['targets'] if i not in judgments]
    unexpected = [i for i in judgments if i not in set(data['targets'])]
    require(not missing and not unexpected, 'stage_judgment_map_incomplete: ' + judgment_field
            + ' missing target IDs: ' + ', '.join(map(str, missing)) + '; unexpected IDs: '
            + ', '.join(map(str, unexpected)) + '; required IDs: ' + ', '.join(map(str, data['targets'])))
    require(wire['evaluation_status'] == 'complete', 'technical_evaluation_failed_not_missing')
    generated = {r['id']: r for r in context['generated_scenarios']}
    bids = {r['id'] for r in context['baseline']}
    sources = {r['source_id']: [r['text']] for r in context.get('reference_material', [])}
    for r in context['baseline']:
        for field in validator.mod.BASELINE_FIELDS:
            sources[f"baseline:{r['id']}:{field}"] = validator.mod.baseline_chunks(r, field)
    for gid, row in generated.items():
        sources[f'generated:{gid}:text'] = [row['text']]
    def stuck(error, label, detail=''):
        """Machine error text that names the exact row and the violated rule.

        The bounded format-repair generation only ever sees the machine errors verbatim, so an error
        that names neither the offending row nor the rule cannot be repaired. Observed in the
        superseded session -006: A-018 got the bare token `coverage_evidence_required` and re-emitted a
        byte-identical wire on both repair turns, exhausting the sealed 3-generation budget. The detail
        texts below only restate the frozen machine rules; they never preselect a business label.
        """
        return error + ': ' + label + (': ' + detail if detail else '')

    def entry(target_type, target_id, issue_type):
        return {'target_type': target_type, 'target_id': target_id, 'issue_type': issue_type}

    def evidence(items, label):
        for item in items:
            require(item['generated_id'] in generated,
                    stuck('unknown_evidence_ID', label, 'evidence.generated_id='
                          + json.dumps(str(item['generated_id']), ensure_ascii=False)))
            candidate = generated[item['generated_id']]
            require(bool(item['quote']) and any(item['quote'] in candidate[f] for f in
                    ('title', 'description', 'test_focus', 'text')),
                    stuck('false_evidence_quote', label, 'evidence.generated_id='
                          + json.dumps(str(item['generated_id']), ensure_ascii=False) + ' quote='
                          + json.dumps(str(item['quote'])[:80], ensure_ascii=False)
                          + ' is not a verbatim substring of that candidate\'s title/description/test_focus/text'))

    def refs(items, business, owners, label):
        internal = {}
        for index, ref in enumerate(items):
            sid, quote = ref['source_id'], ref['quote']
            require(sid in sources and bool(quote) and any(quote in chunk for chunk in sources[sid]),
                    stuck('false_source_quote_or_ID', label, 'source_refs[' + str(index) + '].source_id='
                          + json.dumps(str(sid), ensure_ascii=False) + ' quote='
                          + json.dumps(str(quote)[:80], ensure_ascii=False)
                          + (' is not a declared source ID' if sid not in sources
                             else ' is not a verbatim substring of that source')))
            if sid.startswith('generated:'):
                gid = sid[len('generated:'):-len(':text')]
                require(business == 'conflict' and gid in owners,
                        stuck('internal_conflict_ownership', label, 'source_refs[' + str(index) + ']='
                              + json.dumps(str(sid), ensure_ascii=False) + ' business_consistency='
                              + json.dumps(str(business), ensure_ascii=False)
                              + ' owners=' + json.dumps(sorted(map(str, owners)), ensure_ascii=False)
                              + ' (internal generated: sources are only allowed for business_consistency=conflict)'))
                internal.setdefault(gid, set()).add(quote)
        require(all(len(quotes) >= 2 for quotes in internal.values()),
                stuck('internal_conflict_two_distinct_fragments_required', label,
                      'internal sources=' + json.dumps(
                          {sid: len(quotes) for sid, quotes in sorted(internal.items())}, ensure_ascii=False)
                      + ' each internal generated:<ID>:text source needs at least 2 distinct real fragments'))
    queued = {(q['target_type'], q['target_id'], q['issue_type']) for q in wire['review_queue']}
    if data['stage'] == 'B' and any(q['issue_type'] == 'coverage' for q in wire['review_queue']):
        require(bool(wire['limitations']), 'graph_mapping_dispute_requires_limitations')
    for row in wire[judgment_field].values():
        rid = row[idfield]
        label = judgment_field + '[' + str(rid) + ']'

        def row_require(condition, error, detail=''):
            require(condition, stuck(error, label, detail))

        evidence(row['evidence'], label)
        if kind == 'baseline':
            links = set(row['matched_generated_ids'])
            row_require(links <= set(generated), 'unknown_candidate_link',
                        'matched_generated_ids=' + json.dumps(sorted(map(str, links)), ensure_ascii=False)
                        + ' contains IDs that are not real candidates')
            row_require(links == {e['generated_id'] for e in row['evidence']},
                        'baseline_evidence_link_mismatch',
                        'matched_generated_ids=' + json.dumps(sorted(map(str, links)), ensure_ascii=False)
                        + ' must equal the exact set of evidence.generated_id values='
                        + json.dumps(sorted({str(e['generated_id']) for e in row['evidence']}), ensure_ascii=False))
            cov = row['coverage']
            if cov in ('full', 'partial'):
                row_require(bool(links) and bool(row['covered_aspects']), 'coverage_evidence_required',
                            'coverage=' + str(cov) + ' requires non-empty matched_generated_ids and non-empty '
                            'covered_aspects; got ' + str(len(links)) + ' matched_generated_ids and '
                            + str(len(row['covered_aspects'])) + ' covered_aspects (frozen rule: coverage=missing is '
                            'the only coverage value validated without both)')
            if cov == 'full':
                row_require(not row['missing_aspects'], 'full_has_missing',
                            'coverage=full requires empty missing_aspects; got '
                            + json.dumps(row['missing_aspects'], ensure_ascii=False)[:200])
            if cov == 'partial':
                row_require(bool(row['missing_aspects']), 'partial_requires_missing',
                            'coverage=partial requires non-empty missing_aspects')
            if cov == 'missing':
                row_require(not links and not row['covered_aspects'] and bool(row['missing_aspects']),
                            'missing_has_matches',
                            'coverage=missing requires empty matched_generated_ids and empty covered_aspects and '
                            'non-empty missing_aspects; got ' + str(len(links)) + ' links, '
                            + str(len(row['covered_aspects'])) + ' covered_aspects, '
                            + str(len(row['missing_aspects'])) + ' missing_aspects')
            if cov == 'unresolved':
                row_require((kind, rid, 'coverage') in queued, 'unresolved_requires_review',
                            'coverage=unresolved requires review_queue entry '
                            + json.dumps(entry(kind, rid, 'coverage'), ensure_ascii=False))
            owners = links
        else:
            row_require(set(row['matched_baseline_ids']) <= bids, 'unknown_baseline_link',
                        'matched_baseline_ids=' + json.dumps(sorted(set(row['matched_baseline_ids'])),
                                                             ensure_ascii=False)
                        + ' contains IDs that are not real gold/reference IDs')
            row_require({e['generated_id'] for e in row['evidence']} == {rid}, 'candidate_own_evidence_required',
                        'evidence.generated_id must be exactly this candidate; got '
                        + json.dumps(sorted({str(e['generated_id']) for e in row['evidence']}), ensure_ascii=False))
            owners = {rid}
            if row['relation'] == 'unresolved':
                row_require((kind, rid, 'coverage') in queued and bool(wire['limitations']),
                            'B_unresolved_requires_coverage_review_and_limitations',
                            'relation=unresolved requires review_queue entry '
                            + json.dumps(entry(kind, rid, 'coverage'), ensure_ascii=False) + ' and non-empty limitations')
            if row['relation'] == 'new':
                row_require((kind, rid, 'novelty') in queued, 'full117_new_requires_business_review',
                            'relation=new requires review_queue entry '
                            + json.dumps(entry(kind, rid, 'novelty'), ensure_ascii=False))
        refs(row['source_refs'], row['business_consistency'], owners, label)
        if row['business_consistency'] in ('conflict', 'consistent'):
            row_require(bool(row['source_refs']), 'business_label_requires_real_source_refs',
                        'business_consistency=' + str(row['business_consistency'])
                        + ' requires at least one non-empty real source_refs quote')
        if row['business_consistency'] in ('conflict', 'uncertain'):
            row_require((kind, rid, 'business_consistency') in queued, 'business_dispute_requires_review',
                        'business_consistency=' + str(row['business_consistency'])
                        + ' requires review_queue entry '
                        + json.dumps(entry(kind, rid, 'business_consistency'), ensure_ascii=False))
        if kind == 'generated' and row['granularity'] == 'uncertain':
            row_require((kind, rid, 'granularity') in queued, 'uncertain_granularity_requires_review',
                        'granularity=uncertain requires review_queue entry '
                        + json.dumps(entry(kind, rid, 'granularity'), ensure_ascii=False))
    group_ids = set()
    for index, group in enumerate(wire['duplicate_groups']):
        members = group['generated_ids']
        label = 'duplicate_groups[' + str(index) + ':' + str(group.get('group_id')) + ']'
        require(group['group_id'] not in group_ids,
                stuck('duplicate_group_id', label, 'group_id is already used by an earlier group in this wire'))
        group_ids.add(group['group_id'])
        require(set(members) <= set(generated),
                stuck('unknown_duplicate_member', label, 'generated_ids='
                      + json.dumps(sorted(map(str, members)), ensure_ascii=False) + ' contains non-candidate IDs'))
        require(min(members) in data['targets'],
                stuck('duplicate_owner_not_current_B_targets', label, 'min(generated_ids)=' + str(min(members))
                      + ' must belong to this block\'s targets=' + json.dumps(list(data['targets']), ensure_ascii=False)))
        evidence(group['evidence'], label)
        require({e['generated_id'] for e in group['evidence']} == set(members),
                stuck('duplicate_evidence_mismatch', label, 'evidence.generated_id values='
                      + json.dumps(sorted({str(e['generated_id']) for e in group['evidence']}), ensure_ascii=False)
                      + ' must equal generated_ids=' + json.dumps(sorted(map(str, members)), ensure_ascii=False)))
    return wire


def assembled_result(context, accepted, validator=None):
    validator = validator or ev.Validator()
    maps = {'baseline_judgments': {}, 'generated_judgments': {}}
    result = {'schema_version': '1.0', 'input_id': context['input_id'], 'evaluation_status': 'complete',
              'duplicate_groups': [], 'review_queue': [], 'limitations': []}
    for entry in accepted:
        wire = entry['wire']
        for field in maps:
            # scoped1.1 wires carry only their own stage's judgment map.
            for rid, row in wire.get(field, {}).items():
                require(rid not in maps[field], 'assembled_duplicate_ID')
                maps[field][rid] = copy.deepcopy(row)
        for field in ('duplicate_groups', 'review_queue', 'limitations'):
            # Legacy strings has uniqueItems. Container-only stable uniqueness, not business repair.
            for row in wire[field]:
                if field == 'limitations' and row in result[field]:
                    continue
                result[field].append(copy.deepcopy(row))
    group_ids = set()
    for group in result['duplicate_groups']:
        require(group['group_id'] not in group_ids, 'assembled_duplicate_group_id')
        group_ids.add(group['group_id'])
    for field, source in [('baseline_judgments', 'baseline'), ('generated_judgments', 'generated_scenarios')]:
        ids = [r['id'] for r in context[source]]
        require(set(maps[field]) == set(ids), 'assembled_incomplete_technical_unassessed')
        result[field] = [maps[field][rid] for rid in ids]
    validator.legacy(context, result)
    return result


def sealed_reasoning_effort():
    """The sealed thinking control (ladder rung P4: 'none'), or None for the provider default.

    None and 'none' are different states and must not be conflated: None seals no `reasoning` field at
    all (provider default thinking), while 'none' is itself a sealed parameter that disables the
    reasoning channel and is recorded verbatim in plan.json.
    """
    if REASONING_EFFORT is None:
        return None
    require(REASONING_EFFORT in REASONING_EFFORTS, 'sealed_reasoning_effort_invalid')
    return REASONING_EFFORT


def sealed_parameters():
    """The exact sealed protocol parameters; validate_prepared recomputes and compares them bit for bit."""
    parameters = {'protocol': 'responses', 'stream': True, 'store': False,
        'schema_compatibility': 'omit-unique-items_API_only', 'max_output_tokens': MAX_OUTPUT_TOKENS,
        'workers_max': 2, 'transport_retries_max': TRANSPORT_RETRIES_MAX,
        'format_repairs_max': FORMAT_REPAIRS_MAX, 'generations_max': GENERATIONS_MAX,
        'length_split_once': 'two_target_halves_no_recursive_split',
        'local_request_limit_bytes': MAX_REQUEST_BYTES, 'sse_validation': SSE_VALIDATION_POLICY}
    effort = sealed_reasoning_effort()
    if effort is not None:
        parameters['reasoning_effort'] = effort
    return parameters


def container_values(data):
    """The sealed values of the four root container/echo fields for this request.

    They are copies of sealed plan/request values (schema version, context input_id, request_id and the
    only accepted completion status), never business content. Nothing here is inferred from the model's
    answer.
    """
    return {'schema_version': SCHEMA_VERSION, 'input_id': data['context']['input_id'],
            'request_id': data['request_id'], 'evaluation_status': CONTAINER_EVALUATION_STATUS}


def normalize_container_fields(wire, data):
    """Fill ABSENT-or-null root container fields from the sealed values; never overwrite a value.

    Returns (wire, filled) where `filled` names the fields the model itself omitted or nulled. Any other
    value - including a wrong request_id/input_id/schema_version/evaluation_status - is left untouched
    and keeps failing the schema's `const` check. Business labels, evidence, quotes, source refs, links
    and the review queue are not touched or re-derived here.
    """
    require(isinstance(wire, dict), 'scoped_wire_not_object')
    expected = container_values(data)
    filled = [name for name in CONTAINER_FIELDS if wire.get(name) is None]
    for name in filled:
        wire[name] = expected[name]
    return wire, filled


def repair_errors(data, errors):
    """Machine errors echoed back verbatim, then a deterministic container/JSON-syntax restatement.

    The frozen `ev.messages_for` embeds this list verbatim as `机器错误：{"errors": [...]}` in the bounded
    repair turn, so the exact machine error text stays first and byte-identical; the appended lines only
    restate the sealed container echo fields and JSON syntax. They never preselect a business label,
    coverage value, relation or review item - those stay exactly as strictly validated as before.
    """
    listed = [str(error) for error in (errors or [])]
    if not listed:
        return listed
    expected = container_values(data)
    joined = ' '.join(listed)
    listed.append('wire顶层必须逐字包含四个容器/回显字段（它们是本次request封印值的副本，不是业务内容）：'
                  + '、'.join(str(name) + '=' + json.dumps(expected[name], ensure_ascii=False)
                              for name in CONTAINER_FIELDS)
                  + '；不要臆造其它request_id，也不要改动其中的业务行内容。')
    if any(token in joined for token in ('Expecting', 'Extra data', 'duplicate_json_key', 'JSONDecodeError')) \
            or any(name in joined for name in CONTAINER_FIELDS):
        listed.append('输出必须是单个JSON对象，不要代码围栏、注释或解释文字：键与字符串一律用双引号，'
                      '键与值之间用英文冒号，元素之间用英文逗号，最后一个元素后不得有逗号，'
                      '同一对象内不得出现重复键。')
    return listed


def payload_for(data, schema, prompt, prior=None, errors=None, model=DEFAULT_MODEL):
    messages = ev.messages_for(data, prompt, schema, prior,
                               repair_errors(data, errors) if prior is not None else None)
    body = ev.body_for(require_model_id(model), messages, schema, 'json_schema', MAX_OUTPUT_TOKENS,
                       'responses', 'omit-unique-items', True)
    effort = sealed_reasoning_effort()
    if effort is not None:
        body['reasoning'] = {'effort': effort}
    return body


def source_hashes(extra=()):
    paths = [Path(__file__), PROMPT, ev.PROMPT, Path(ev.__file__),
             Path(ev.__file__).with_name('safe_http.py'), Path(ev.__file__).with_name('sse_fidelity.py'),
             Path(ev.__file__).with_name('audit_run.py'), Path(ev.__file__).with_name('evaluate_platform.py'),
             ev.GOLD, ev.ASSETS / 'judgment.schema.json', ev.ASSETS / '.qa/validate_assets.py'] + list(extra)
    return {str(p.resolve()): ev.sha(p) for p in paths}


def check_hashes(hashes):
    require(all(Path(p).is_file() and ev.sha(p) == h for p, h in hashes.items()), 'source_or_protected_hash_changed')


def run_root(output):
    """The run root that owns every artifact of this judging run (assessment dirs, logs, generation)."""
    return Path(output).resolve().parent


def protected_hashes(output):
    # Include all preexisting frozen/gold/control/history bytes; exclude the whole current run root,
    # whose real inputs stay bound by generation_sources/source_hashes instead of by this list.
    root = run_root(output)
    paths = list(ev.ASSETS.rglob('*')) + list(ev.GOLD.parent.rglob('*'))
    paths += list(Path(__file__).parent.glob('*.py')) + list(Path(__file__).parent.glob('tests/*.py'))
    return {str(p.resolve()): ev.sha(p) for p in paths if p.is_file()
            and not p.resolve().is_relative_to(root)
            and p.name != 'generate_project_scenarios.py' and p.name != 'test_generate_project_scenarios.py'}


def validate_generation(scenes, manifest):
    if 'source_metadata' in manifest:
        metadata = manifest['source_metadata']
        require(len(metadata.get('project', [])) == 1 and metadata['project'][0]['id'] == 3,
                'generation_requires_project3_space2')
        require(len(metadata.get('space', [])) == 1 and metadata['space'][0]['id'] == 2
                and metadata['space'][0]['project_id'] == 3, 'generation_requires_project3_space2')
        outcomes = manifest.get('outcomes', [])
        require(len(outcomes) == 6 and [o.get('scope') for o in outcomes] == list(SCOPES)
                and all(o.get('status') == 'snapshot_saved' and o.get('scenario_count', 0) > 0 for o in outcomes),
                'generation_requires_six_completed_scope_snapshots')
        require(manifest.get('gold_or_controls_loaded') is False and manifest.get('not_raw_llm') is True,
                'generation_source_boundary_invalid')
        ids = [o['task_id'] for o in outcomes]
        rows = scenes['scenarios']
        require(manifest.get('scenario_count') == len(rows), 'generation_count_mismatch')
        for outcome in outcomes:
            selected = [r for r in rows if r.get('source_task_id') == outcome['task_id']]
            require(len(selected) == outcome['scenario_count'] and all(r.get('task_scope') == outcome['scope'] for r in selected),
                    'candidate_scope_task_mapping_invalid')
    else:
        # Explicit simple interchange contract, useful for local mock fixtures too.
        require(manifest.get('project_id') == 3 and manifest.get('space_id') == 2, 'generation_requires_project3_space2')
        tasks = manifest.get('tasks', manifest.get('task_mapping', []))
        if isinstance(tasks, dict):
            tasks = [{'category': k, **v} for k, v in tasks.items()]
        require(len(tasks) == 6 and {t.get('category', t.get('category_id')) for t in tasks} == set(CATEGORIES),
                'generation_requires_six_category_task_mapping')
        ids = [t.get('task_id', t.get('source_task_id')) for t in tasks]
        require(manifest.get('status') in ('complete', 'completed', 'success') or manifest.get('complete') is True,
                'generation_manifest_not_complete')
        rows = scenes['scenarios']
    require(all(isinstance(i, int) and i > 0 and i != 6 for i in ids) and len(set(ids)) == 6,
            'generation_requires_six_new_real_task_IDs')
    require(bool(rows) and all(r.get('source_task_id') in ids for r in rows), 'candidate_source_task_mapping_invalid')
    return rows


def generation_sources(scenarios, manifest_path, manifest):
    paths = [Path(scenarios), Path(manifest_path)]
    if 'source_metadata' not in manifest:
        return paths
    directory = Path(manifest_path).parent
    fingerprint = directory / 'fingerprint.json'
    fp = ev.load(fingerprint)
    binding = fp['binding']
    encoded = (json.dumps(binding, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    require(ev.digest(encoded) == fp['sha256'] == manifest['fingerprint'], 'generation_fingerprint_mismatch')
    require(binding['metadata'] == manifest['source_metadata'], 'generation_metadata_binding_mismatch')
    frozen = directory / 'generator-executed.py'
    require(ev.sha(frozen) == binding['script_sha256'] and generation_version_bound(binding.get('version')), 'generation_frozen_script_mismatch')
    require(ev.sha(Path(fetch.__file__)) == binding['map_scenarios_source_sha256'], 'generation_mapper_hash_mismatch')
    metadata = binding['metadata']
    require(metadata['project'][0]['status'] == metadata['space'][0]['status'] == 'active', 'generation_inactive_source')
    require(len(metadata['documents']) == 1 and metadata['documents'][0]['id'] == 2
            and metadata['documents'][0]['status'] == 'ready', 'generation_doc2_not_ready')
    require(len(metadata['pages']) == 8 and all(p['source_document_id'] == 2 and p['status'] == 'published' for p in metadata['pages']), 'generation_pages_wrong_document')
    require(len(metadata['chunks']) == 34 and all(c['document_id'] == 2 for c in metadata['chunks']), 'generation_chunks_wrong_document')
    require(not any(b['enabled'] for b in metadata['external_bindings']), 'generation_external_source_mixed')
    preview_path = directory / 'document-2-preview.raw.json'
    preview = ev.load(preview_path)
    parsed_path = directory / 'document-2-parsed-hash.json'
    parsed = ev.load(parsed_path)
    require(preview['document_id'] == parsed['document_id'] == 2 and preview['truncated'] is False
            and preview['quality_ok'] is True and len(preview['text']) == preview['char_count'] == parsed['text_length'] == metadata['documents'][0]['char_count']
            and ev.digest(preview['text']) == parsed['text_sha256'], 'generation_preview_not_complete_or_hash_mismatch')
    paths += [fingerprint, frozen, Path(fetch.__file__), preview_path, parsed_path]
    concatenated = []
    require(len(binding['payloads']) == 6, 'generation_payloads_incomplete')
    for index, outcome in enumerate(manifest['outcomes'], 1):
        scope = directory / f'scope-{index:02d}'
        mandatory = ('manifest.json', 'scenarios.json', 'proposal.raw.json', 'created.raw.json', 'observed_task.json', 'retrieval.raw.json', 'retrieval-confirm.raw.json', 'citations.raw.json', 'events.raw.json')
        require(all((scope / name).is_file() for name in mandatory), 'generation_scope_missing_raw_block')
        meta = ev.load(scope / 'manifest.json')
        require(meta.get('fresh_task_created_by_this_run') is True and meta['task_id'] == outcome['task_id']
                and meta['task_scope'] == outcome['scope'] and meta['checkpoint_status'] == 'pending'
                and meta['checkpoint_version'] == 1, 'generation_snapshot_not_fresh_pending_scope')
        require(ev.sha(scope / 'proposal.raw.json') == meta['proposal_sha256'], 'generation_proposal_hash_mismatch')
        original = ev.load(scope / 'created.raw.json')
        payload = binding['payloads'][index - 1]
        require(payload['project_id'] == 3 and payload['wiki_space_id'] == 2
                and payload['title'] == '纯场景采样：' + outcome['scope']
                and not payload['reference_case_ids'] and not payload['reference_text'], 'generation_payload_scope_mismatch')
        require(original['id'] == outcome['task_id'] and original['project_id'] == 3
                and original['wiki_space_id'] == 2 and original['title'] == payload['title']
                and original['description'] == payload['description'], 'generation_created_task_identity_mismatch')
        observed = ev.load(scope / 'observed_task.json')
        require(observed['id'] == outcome['task_id'] and observed['project_id'] == 3 and observed['wiki_space_id'] == 2
                and observed['status'] == 'awaiting_test_point_confirmation' and observed['imported_case_count'] == 0
                and observed['test_point_count'] == outcome['scenario_count'], 'generation_observed_task_not_unconfirmed')
        retrieval = ev.load(scope / 'retrieval.raw.json')
        confirmed = ev.load(scope / 'retrieval-confirm.raw.json')
        require(retrieval['task_id'] == outcome['task_id'] and retrieval['version'] == retrieval['attempt'] == 1
                and confirmed['id'] == outcome['task_id'] and confirmed['project_id'] == 3 and confirmed['wiki_space_id'] == 2, 'generation_retrieval_wrong_task')
        citations = ev.strict_json('{"items":' + (scope / 'citations.raw.json').read_text() + '}')['items']
        require(citations and {c['id'] for c in citations} == {c['id'] for c in retrieval['candidate_citations']}, 'generation_citations_not_actual_retrieval')
        pages = {p['id'] for p in metadata['pages']}
        chunks = {c['id'] for c in metadata['chunks']}
        require(all((c.get('wiki_page_id') in pages if c['citation_type'] == 'wiki' else c.get('source_chunk_id') in chunks) for c in citations), 'generation_citations_wrong_document')
        proposal = ev.load(scope / 'proposal.raw.json')
        require(proposal['retrieval_checkpoint_id'] == retrieval['id'] and all(set(p['citation_ids']) <= {c['id'] for c in citations} for p in proposal['points']), 'generation_proposal_retrieval_binding_invalid')
        require(proposal['status'] == 'pending' and proposal['version'] == proposal['attempt'] == 1
                and proposal['id'] == meta['checkpoint_id'], 'generation_checkpoint_identity_mismatch')
        rebuilt = fetch.map_scenarios(proposal, outcome['task_id'], None)
        for row in rebuilt['scenarios']:
            row['task_scope'] = outcome['scope']
        archived = ev.load(scope / 'scenarios.json')
        require(rebuilt == archived and len(rebuilt['scenarios']) == meta['scenario_count'] == outcome['scenario_count'], 'generation_snapshot_not_exact_proposal_mapping')
        concatenated.extend(rebuilt['scenarios'])
        paths += [p for p in scope.glob('*.json') if p.is_file()]
    require(concatenated == ev.load(scenarios)['scenarios'] and len({r['id'] for r in concatenated}) == len(concatenated), 'generation_aggregate_not_exact_snapshots')
    return paths


def archived_probe_verdict(output, model):
    """Re-derive the probe verdict from archived raw captures; never sends a new network call.

    A probe id is sufficient for sealing when an archived attempt proves HTTP 200, exact model echo,
    the sealed strict SSE validation and schema-valid structured output. The archived summary may
    still say `failed` when the only reason was the reasoning channel, which is now covered by the
    sealed extension; this function revalidates the raw bytes instead of trusting that summary.
    """
    directory = Path(output) / 'model-probe'
    summary_path = directory / 'probe.json'
    require(summary_path.is_file(), 'model_probe_missing')
    summary = ev.load(summary_path)
    tried = summary.get('models_tried') if isinstance(summary, dict) else None
    require(isinstance(tried, list) and bool(tried) and all(isinstance(m, str) for m in tried),
            'model_probe_evidence_invalid')
    require(summary.get('requests_sent', PROBE_MAX_REQUESTS + 1) <= PROBE_MAX_REQUESTS, 'model_probe_budget_invalid')
    require(model in tried, 'model_probe_not_bound_to_selected_model')
    for attempt in sorted(p for p in directory.glob('attempt-*') if p.is_dir()):
        capture_path, request_path = attempt / 'response.safe.json', attempt / 'request.safe.json'
        if not capture_path.is_file() or not request_path.is_file():
            continue
        capture = ev.load(capture_path)
        native = effective_response(capture)
        try:
            require(ev.load(request_path).get('model') == model, 'probe_request_model_mismatch')
            require(capture.get('http_status') == 200, 'probe_http_not_200')
            require(capture_error_acceptable(capture), 'probe_capture_error')
            _, manifest = strict_two_channel_assemble(capture)
            require(isinstance(native, dict), 'probe_response_not_object')
            ev.validate_response_model(native, model)
            content, finish = ev.parse_content(native, 'responses')
            require(finish == 'stop', 'probe_response_not_complete')
            wire = ev.strict_json(content.strip())
            errors = list(Draft202012Validator(PROBE_SCHEMA).iter_errors(wire))
            require(not errors and wire.get('ok') is True,
                    'probe_structured_output_invalid: ' + '; '.join(e.message for e in errors[:3]))
        except (SafeFailure, ValueError):
            continue
        return {'dir': 'model-probe', 'summary_sha256': ev.sha(summary_path),
                'capture': str(capture_path.relative_to(Path(output))), 'capture_sha256': ev.sha(capture_path),
                'selected_model': model, 'echoed_model': native['model'], 'http_status': 200,
                'requests_sent': summary['requests_sent'], 'usage': ev.usage_for(native, 'responses'),
                'sse_channel_mode': manifest['channel_mode'],
                'archived_probe_status_before_revalidation': summary.get('status'),
                'verdict': 'archived_probe_revalidated_offline_no_new_network_call'}
    raise SafeFailure('archived_model_probe_insufficient')


def model_probe_seal(output, model):
    """Bind the sealed judge model to its archived probe evidence, when a probe was run."""
    if not (Path(output) / 'model-probe' / 'probe.json').is_file():
        return None
    return archived_probe_verdict(output, model)


def check_model_probe(output, spec):
    probe = spec.get('model_probe')
    present = (Path(output) / 'model-probe' / 'probe.json').is_file()
    if probe is None:
        require(not present, 'model_probe_evidence_not_sealed')
        return None
    require(isinstance(probe, dict) and probe.get('dir') == 'model-probe', 'model_probe_binding_invalid')
    require(probe == archived_probe_verdict(output, spec['model']), 'model_probe_seal_not_exact_revalidation')
    return probe


def prepare(scenarios, manifest, output, size=5, base_url=None, api_key_env=None, model=None):
    output = Path(output)
    require(not output.is_symlink(), 'output_symlink_refused')
    existing = {p.name for p in output.iterdir()} if output.is_dir() else set()
    require(not output.exists() or existing <= PREEXISTING_RUN_ENTRIES, 'output_exists_refuse_overwrite')
    model = require_model_id(model or DEFAULT_MODEL)
    scenes, provenance = ev.load(scenarios), ev.load(manifest)
    rows = validate_generation(scenes, provenance)
    generation_paths = generation_sources(scenarios, manifest, provenance)
    references = []
    if 'source_metadata' in provenance:
        preview = ev.load(Path(manifest).parent / 'document-2-preview.raw.json')
        references = [{'source_id': 'project3-document2-rules', 'text': preview['text'],
            'source': '用户本项目导入交易规则的API解析原文，非独立法律审查/非gold重审批'}]
    context = full_context(rows, {'provenance': 'new_project_service_generation_snapshot',
        'project_id': 3, 'space_id': 2, 'human_edit_status': 'unknown',
        'first_generation_unmodified': 'unknown', 'source_manifest_sha256': ev.sha(manifest)}, references=references)
    plan = target_plan(context, size)
    validator, prompt = ev.Validator(), prompt_text()
    preflight = []
    # Worst-case B mapping includes every potential edge, exact full A row-sized data is
    # unknown before A. Initial B estimate is disclosed; actual B bytes gated again.
    estimated = {'origin': 'preflight_only_not_sent_as_real_A',
        'candidate_to_baseline': {r['id']: [b['id'] for b in context['baseline']] for r in rows},
        'baseline_judgments': []}
    for target in plan:
        data = request_data(context, target, estimated if target['stage'] == 'B' else None)
        schema = scoped_schema(data, validator.legacy_schema)
        body = payload_for(data, schema, prompt, model=model)
        count = len(ev.canonical(body).encode())
        require(count <= MAX_REQUEST_BYTES, 'request_exceeds_local_one_MiB_no_filtering')
        preflight.append({'request_id': target['request_id'], 'stage': target['stage'],
            'target_count': len(target['targets']), 'context_bytes': len(ev.canonical(context).encode()),
            'schema_bytes': len(ev.canonical(schema).encode()), 'request_bytes': count,
            'B_actual_mapping_bytes_pending': target['stage'] == 'B'})
    public = provider_public('new-api', base_url, api_key_env)
    public = {**public, 'endpoint': public['endpoint'].removesuffix('/chat/completions') + '/responses'}
    sources = source_hashes(generation_paths)
    protected = protected_hashes(output)
    spec = {'version': VERSION, 'schema_version': SCHEMA_VERSION, 'model': model,
        'model_probe': model_probe_seal(output, model),
        'generation_inputs': {'scenarios': str(Path(scenarios).resolve()), 'manifest': str(Path(manifest).resolve())},
        'provider_public': public, 'endpoint': public['endpoint'], 'base_url': base_url,
        'api_key_env': api_key_env, 'sources': sources, 'protected': protected,
        'context_sha256': ev.digest(ev.canonical(context)), 'prompt_sha256': ev.digest(prompt),
        'target_plan': plan, 'preflight': preflight, 'gold_count': 117, 'candidate_count': len(rows),
        'parameters': sealed_parameters(),
        'context_budget': 'unknown; one small A smoke must complete with returned usage before workers2',
        'protocol_difference_from_calibration': 'full117+allN each request; only targets output; B graph reversed from A; not independent blind agreement',
        'planned_calls': len(plan)}
    spec['fingerprint_sha256'] = ev.digest(ev.canonical(spec))
    output.mkdir(parents=True, exist_ok=True)
    ev.save(output / 'context.json', context)
    ev.save(output / 'prompt.md', prompt, text=True)
    ev.save(output / 'legacy.schema.json', validator.legacy_schema)
    ev.save(output / 'plan.json', spec)
    check_hashes(sources)
    check_hashes(protected)
    return spec


def validate_prepared(output, model=None):
    output = Path(output)
    spec = ev.load(output / 'plan.json')
    bound = {k: v for k, v in spec.items() if k != 'fingerprint_sha256'}
    require(ev.digest(ev.canonical(bound)) == spec.get('fingerprint_sha256'), 'prepared_fingerprint_mismatch')
    require(spec['version'] == VERSION and spec['parameters'] == sealed_parameters(), 'protocol_parameter_drift')
    require(spec['endpoint'] == spec['provider_public']['endpoint'] and spec['endpoint'].endswith('/responses'), 'endpoint_plan_drift')
    require(spec['schema_version'] == SCHEMA_VERSION, 'prepared_protocol_mismatch')
    sealed = require_model_id(spec.get('model'), 'sealed_model_invalid')
    if model is not None:
        require(require_model_id(model, 'cli_model_invalid') == sealed, 'cli_model_differs_from_sealed_plan')
    # Either list may have been emptied or trimmed in plan.json; recompute both from real bytes.
    generation = spec.get('generation_inputs')
    require(isinstance(generation, dict) and set(generation) == {'scenarios', 'manifest'}, 'generation_inputs_missing')
    scenarios, manifest_path = Path(generation['scenarios']), Path(generation['manifest'])
    require(scenarios.is_file() and manifest_path.is_file(), 'generation_inputs_missing')
    expected_sources = source_hashes(generation_sources(scenarios, manifest_path, ev.load(manifest_path)))
    require(isinstance(spec.get('sources'), dict) and bool(spec['sources']), 'prepared_sources_empty')
    require(spec['sources'] == expected_sources, 'prepared_sources_not_exact_recomputation')
    expected_protected = protected_hashes(output)
    require(isinstance(spec.get('protected'), dict) and bool(spec['protected']), 'prepared_protected_empty')
    require(spec['protected'] == expected_protected, 'prepared_protected_not_exact_recomputation')
    check_model_probe(output, spec)
    check_hashes(spec['sources'])
    check_hashes(spec['protected'])
    context = ev.load(output / 'context.json')
    require(ev.digest(ev.canonical(context)) == spec['context_sha256'], 'context_tamper')
    require(ev.digest((output / 'prompt.md').read_text()) == spec['prompt_sha256'], 'prompt_tamper')
    validator = ev.Validator()
    validator.input(context)
    require(ev.load(output / 'legacy.schema.json') == validator.legacy_schema, 'legacy_schema_tamper')
    require(spec['target_plan'] == target_plan(context, len(spec['target_plan'][0]['targets'])), 'target_plan_tamper')
    return spec, context


@contextmanager
def pid_lock(output):
    output = Path(output).resolve()
    lock = output / '.active.lock'
    require(not lock.is_symlink(), 'lock_symlink_refused')
    try:
        with lock.open('x') as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        raise SafeFailure('active_or_stale_outer_PID_lock_manual_inspection_required') from None
    try:
        yield
    finally:
        require(lock.resolve().parent == output and lock.name == '.active.lock', 'unsafe_lock_target')
        lock.unlink()


class ScopedScheduler:
    def __init__(self, output, transport=None, model=None):
        self.output = Path(output)
        self.spec, self.context = validate_prepared(output, model)
        self.model = self.spec['model']
        self.prompt = (self.output / 'prompt.md').read_text()
        self.validator = ev.Validator()
        self.transport = transport
        self.redactor = getattr(transport, 'redactor', Redactor())
        self.cancel = Event()
        for path in self.output.glob('requests/*/attempt-*/response.safe.json'):
            try:
                response = ev.load(path)
            except (SafeFailure, ValueError):
                continue  # unreadable/partial capture; evaluate classifies it as interrupted
            if response.get('http_status') == 401 or ev.quota_exhausted(response):
                self.cancel.set()
        if transport is not None:
            require(getattr(transport, 'endpoint', self.spec['endpoint']) == self.spec['endpoint'], 'transport_endpoint_mismatch')

    def audit_entry(self, directory, data):
        schema = scoped_schema(data, self.validator.legacy_schema)
        require(ev.load(directory / 'input.json') == data, 'archived_input_not_exact')
        require(ev.load(directory / 'schema.json') == schema, 'archived_schema_not_exact')
        repair = ev.load(directory / 'repair.json') if (directory / 'repair.json').exists() else {}
        if repair:
            previous = sorted(p for p in directory.parent.glob('attempt-*') if p.name < directory.name and (p / 'status.json').exists() and ev.load(p / 'status.json').get('failure_kind') == 'format')
            require(bool(previous) and repair == {'prior': (previous[-1] / 'content.original.txt').read_text(), 'errors': ev.load(previous[-1] / 'status.json')['errors']}, 'repair_not_bound_to_previous_machine_failure')
        body = payload_for(data, schema, self.prompt, repair.get('prior'), repair.get('errors'), self.model)
        require(ev.load(directory / 'request.json') == body, 'archived_request_not_exact')
        response_path, status_path = directory / 'response.safe.json', directory / 'status.json'
        require((directory / 'started.json').is_file(), 'attempt_missing_started_seal')
        started = ev.load(directory / 'started.json')
        require(started == {'pid': started.get('pid'), 'request_id': data['request_id'], 'model': self.model},
                'interrupted_started_tamper')
        if not response_path.exists():
            # Interrupted HTTP attempt: unknown usage/billing, kept and reported, never completed.
            require(not status_path.exists(), 'interrupted_attempt_has_status_without_response')
            return None
        try:
            response = ev.load(response_path)
        except (SafeFailure, ValueError):
            require(not status_path.exists(), 'unreadable_response_archive_with_status')
            return None
        require(self.redactor.clean(body) == body and self.redactor.clean(response) == response,
                'archived_credential_echo')
        if not status_path.exists():
            # Captured but never derived: unknown outcome, no success claim; the sealed strict
            # validation is advisory here because no archived status asserts success.
            if response.get('eventmeta'):
                inspect_sse_capture(response)
                try:
                    strict_two_channel_assemble(response)
                except SafeFailure:
                    pass
            return None
        state = ev.load(status_path)
        recorded_filled = list(state.get('container_fields_filled') or [])
        sse_manifest = None
        if response.get('eventmeta'):
            try:
                _, sse_manifest = verify_sse_capture(response)
            except SafeFailure:
                require(state['status'] != 'success', 'successful_SSE_invalid')
                sse_manifest = None
        require(ev.load(directory / 'validation.json') == self.validation_record(state, sse_manifest, recorded_filled),
                'validation_archive_tamper')
        require(state['response_sha256'] == ev.sha(response_path), 'response_hash_tamper')
        if state['status'] != 'success':
            return None
        require(response.get('http_status') == 200 and capture_error_acceptable(response)
                and not response.get('redaction_applied'), 'successful_response_invalid')
        require(isinstance(response.get('eventmeta'), dict), 'successful_capture_without_responses_sse')
        native = effective_response(response)
        ev.validate_response_model(native, self.model)
        content, finish = ev.parse_content(native, 'responses')
        require(finish == 'stop', 'SSE_noncomplete_technical_failure')
        require((directory / 'content.original.txt').read_text() == content, 'content_original_tamper')
        wire, filled = normalize_container_fields(ev.strict_json(content.strip()), data)
        # The fill is recomputed from the archived original content, so a hand-edited wire.json or a
        # silently edited raw answer both fail here.
        require(filled == recorded_filled, 'container_field_fill_not_reproducible')
        require(wire == ev.load(directory / 'wire.json'), 'original_wire_tamper')
        require(state['wire_sha256'] == ev.digest(ev.canonical(wire)), 'wire_hash_tamper')
        validate_scoped(data, wire, schema, self.validator)
        origin = {'stage': data['stage'], 'request_id': data['request_id'], 'attempt': directory.name,
                  'path': str(directory.relative_to(self.output)), 'content_sha256': ev.digest(content),
                  'wire_sha256': state['wire_sha256'], 'validator_sha256': self.spec['sources'][str((ev.ASSETS / '.qa/validate_assets.py').resolve())]}
        require(ev.load(directory / 'mapping.json') == origin, 'mapping_provenance_tamper')
        return {'wire': wire, 'origin': origin}

    def validation_record(self, state, sse_manifest, filled):
        """The exact archived validation.json body; both live derivation and offline audit build it here.

        `container_fields_filled` records which root echo fields the model itself omitted or nulled and
        that were therefore filled from the sealed request/plan values. It is evidence, not a relaxation:
        a present-but-wrong value is still rejected by the schema before this record is written.
        """
        return {'status': 'passed' if state['status'] == 'success' else 'failed',
                'errors': state['errors'], 'semantic_validation': 'not_performed',
                'container_fields_filled': list(filled),
                'validator_sources': self.spec['sources'],
                'sse_validation': sse_validation_record(sse_manifest) if state['status'] == 'success' else None}

    def finalize_attempt(self, directory, data, response):
        """Derive and archive one captured response; also used to recover an interrupted attempt."""
        schema = scoped_schema(data, self.validator.legacy_schema)
        state = {'status': 'evaluation_failed', 'failure_kind': 'technical', 'errors': [],
                 'container_fields_filled': [], 'response_sha256': ev.sha(directory / 'response.safe.json')}
        content, sse_manifest = None, None
        try:
            require(response.get('http_status') == 200 and capture_error_acceptable(response),
                    response.get('error') or 'http_' + str(response.get('http_status')))
            _, sse_manifest = verify_sse_capture(response)
            native = effective_response(response)
            ev.validate_response_model(native, self.model)
            content, finish = ev.parse_content(native, 'responses')
            save_once(directory / 'content.original.txt', content, text=True)
            require(finish == 'stop', 'SSE_noncomplete_technical_failure')
            state['failure_kind'] = 'format'
            wire, filled = normalize_container_fields(ev.strict_json(content.strip()), data)
            state['container_fields_filled'] = filled
            save_once(directory / 'wire.json', wire)
            validate_scoped(data, wire, schema, self.validator)
            state.update(status='success', failure_kind=None, wire_sha256=ev.digest(ev.canonical(wire)))
            save_once(directory / 'mapping.json', {'stage': data['stage'], 'request_id': data['request_id'],
                'attempt': directory.name, 'path': str(directory.relative_to(self.output)),
                'content_sha256': ev.digest(content), 'wire_sha256': state['wire_sha256'],
                'validator_sha256': self.spec['sources'][str((ev.ASSETS / '.qa/validate_assets.py').resolve())]})
        except Exception as exc:
            state['errors'] = [str(exc) if isinstance(exc, (SafeFailure, ValueError)) else 'local_' + type(exc).__name__]
        native = effective_response(response)
        state['response_model'] = native.get('model') if isinstance(native, dict) else None
        save_once(directory / 'validation.json',
                  self.validation_record(state, sse_manifest, state['container_fields_filled']))
        save_once(directory / 'status.json', state)
        return state, content

    def evaluate(self, target, a_mapping=None):
        data = request_data(self.context, target, a_mapping)
        root = self.output / 'requests' / target['request_id']
        prior, errors = None, None
        transports, generations = 0, 0
        # Every archived attempt is immutable; derive the remaining budget from captures.
        for directory in sorted(root.glob('attempt-*')):
            if not directory.is_dir():
                continue
            response_path, status_path = directory / 'response.safe.json', directory / 'status.json'
            if not response_path.exists():
                # Interrupted HTTP: unknown cost, counted, and still recoverable inside budget.
                self.audit_entry(directory, data)
                transports += 1
                continue
            try:
                response = ev.load(response_path)
            except (SafeFailure, ValueError):
                require(not status_path.exists(), 'unreadable_response_archive_with_status')
                transports += 1
                continue
            if not status_path.exists():
                # Re-verify the archived capture in place; a verified success is never resent.
                state, content = self.finalize_attempt(directory, data, response)
                if state['status'] == 'success':
                    entry = self.audit_entry(directory, data)
                    if entry:
                        return entry
                if response.get('http_status') == 401 or ev.quota_exhausted(response):
                    self.cancel.set()
                    return None
                if ev.retryable(response):
                    transports += 1
                elif state.get('failure_kind') == 'format':
                    generations += 1
                    prior, errors = content, state['errors']
                else:
                    transports += 1
                continue
            entry = self.audit_entry(directory, data)
            if entry:
                return entry
            state = ev.load(status_path)
            if response.get('http_status') == 401 or ev.quota_exhausted(response):
                self.cancel.set()
                return None
            if ev.retryable(response):
                transports += 1
            elif state.get('failure_kind') == 'format':
                generations += 1
                prior = (directory / 'content.original.txt').read_text()
                errors = state['errors']
            else:
                return None
        while not self.cancel.is_set() and transports <= TRANSPORT_RETRIES_MAX and generations < GENERATIONS_MAX:
            directory = root / f'attempt-{len(list(root.glob("attempt-*"))) + 1:03d}'
            schema = scoped_schema(data, self.validator.legacy_schema)
            body = payload_for(data, schema, self.prompt, prior, errors, self.model)
            require(len(ev.canonical(body).encode()) <= MAX_REQUEST_BYTES, 'actual_request_exceeds_one_MiB_no_filtering')
            require(self.redactor.clean(body) == body, 'request_would_expose_selected_credential')
            directory.mkdir(parents=True)
            ev.save(directory / 'input.json', data)
            ev.save(directory / 'schema.json', schema)
            if prior is not None:
                ev.save(directory / 'repair.json', {'prior': prior, 'errors': errors})
            ev.save(directory / 'request.json', body)
            save_once(directory / 'started.json', {'pid': os.getpid(), 'request_id': target['request_id'], 'model': self.model})
            try:
                response = self.transport(body)
            except Exception as exc:
                response = {'http_status': None, 'body': None, 'error': 'transport_' + type(exc).__name__}
            if self.redactor.clean(response) != response:
                response = self.redactor.clean(response)
                response['error'] = 'credential_echo_redacted'
            save_once(directory / 'response.safe.json', response)
            if response.get('http_status') == 401 or ev.quota_exhausted(response):
                self.cancel.set()
            state, content = self.finalize_attempt(directory, data, response)
            entry = self.audit_entry(directory, data)
            if entry:
                return entry
            if ev.retryable(response):
                transports += 1
            elif state['failure_kind'] == 'format':
                generations += 1
                prior, errors = content, state['errors']
            else:
                break
        return None

    def evaluate_group(self, target, a_mapping=None):
        split_path = self.output / 'requests' / target['request_id'] / 'split.json'
        if split_path.exists():
            children = self.split_targets(target)
            require(ev.load(split_path) == {'children': children}, 'technical_split_target_tamper')
        else:
            entry = self.evaluate(target, a_mapping)
            if entry:
                return [entry]
            captures = sorted((self.output / 'requests' / target['request_id']).glob('attempt-*/response.safe.json'))
            if not captures or len(target['targets']) < 2 or self.cancel.is_set():
                return []
            try:
                native = effective_response(ev.load(captures[-1])) or {}
            except (SafeFailure, ValueError):
                return []
            if not isinstance(native, dict) or native.get('status') != 'incomplete' \
                    or (native.get('incomplete_details') or {}).get('reason') != 'max_output_tokens':
                return []
            children = self.split_targets(target)
            ev.save(split_path, {'children': children})
        # Exactly one predeclared split; child evaluate cannot recursively split.
        return [entry for child in children if (entry := self.evaluate(child, a_mapping))]

    @staticmethod
    def split_targets(target):
        mid = (len(target['targets']) + 1) // 2
        return [{**target, 'request_id': target['request_id'] + '-split-' + str(i), 'targets': ids}
                for i, ids in enumerate((target['targets'][:mid], target['targets'][mid:]), 1)]


def summarize(context, accepted, assembled=None):
    report = {'status': 'complete' if assembled else 'incomplete',
        'technical_status': 'passed' if assembled else 'technical_unassessed',
        'baseline_denominator': 117, 'candidate_denominator': len(context['generated_scenarios']),
        'coverage_scores': None, 'candidate_counts': None, 'technical_failure_never_missing': True,
        'graph_mapping_policy': 'B links are deterministic reverse of real A edges, not independent blind judgment or inter-model agreement; valid quotes/links do not prove semantics',
        'graph_mapping_disputes': [dict(q, origin=e['origin']) for e in accepted if e['origin']['stage'] == 'B'
                                 for q in e['wire']['review_queue'] if q['issue_type'] == 'coverage'],
        'row_provenance': {rid: e['origin'] for e in accepted for field in
                           ('baseline_judgments', 'generated_judgments') for rid in e['wire'].get(field, {})},
        'baseline_technical_unassessed': 117 - sum(len(e['wire'].get('baseline_judgments', {})) for e in accepted),
        'candidate_technical_unassessed': len(context['generated_scenarios']) - sum(len(e['wire'].get('generated_judgments', {})) for e in accepted),
        'not_calibration_or_regulatory_proof': True}
    if assembled:
        def coverage(rows):
            counts = Counter(r['coverage'] for r in rows)
            return {'denominator': len(rows), 'counts': dict(counts),
                    'full': counts['full'] / len(rows),
                    'full_plus_partial': (counts['full'] + counts['partial']) / len(rows),
                    'business_consistency': dict(Counter(r['business_consistency'] for r in rows)),
                    'full_without_conflict_count': sum(r['coverage'] == 'full' and r['business_consistency'] != 'conflict' for r in rows),
                    'full_and_consistent_count': sum(r['coverage'] == 'full' and r['business_consistency'] == 'consistent' for r in rows)}
        byid = {r['id']: r for r in context['baseline']}
        report['coverage_scores'] = {'all117': coverage(assembled['baseline_judgments']),
            'categories': {cat: coverage([r for r in assembled['baseline_judgments'] if byid[r['baseline_id']]['category'] == cat]) for cat in CATEGORIES}}
        report['candidate_counts'] = {field: dict(Counter(r[field] for r in assembled['generated_judgments']))
                                      for field in ('relation', 'granularity', 'business_consistency')}
        report['duplicate_groups'] = assembled['duplicate_groups']
        report['review_queue'] = assembled['review_queue']
    return report


def audit(output, model=None):
    """Scoped offline audit, NOT audit_run.inspect_run (which is wire1.1/calibration specific)."""
    scheduler = ScopedScheduler(output, None, model)
    started_path = Path(output) / 'execution.started.json'
    if started_path.exists():
        seal = ev.load(started_path)
        require(isinstance(seal.get('plan_sha256'), str)
                and seal['plan_sha256'] == ev.sha(Path(output) / 'plan.json'), 'execution_started_plan_seal_invalid')
        require(isinstance(seal.get('workers'), int) and 1 <= seal['workers'] <= 2, 'execution_started_workers_invalid')
    accepted = []
    a_mapping = None
    for target in scheduler.spec['target_plan']:
        if target['stage'] == 'B' and a_mapping is None:
            try:
                a_mapping = inverse_mapping(scheduler.context, accepted)
            except SafeFailure:
                break
        split = Path(output) / 'requests' / target['request_id'] / 'split.json'
        for directory in sorted((split.parent).glob('attempt-*')):
            entry = scheduler.audit_entry(directory, request_data(scheduler.context, target, a_mapping))
            if entry:
                require(not split.exists(), 'successful_target_must_not_split')
                accepted.append(entry)
                break
        if split.exists():
            children = scheduler.split_targets(target)
            require(ev.load(split) == {'children': children}, 'technical_split_target_tamper')
            captures = sorted(split.parent.glob('attempt-*/response.safe.json'))
            native = effective_response(ev.load(captures[-1])) or {}
            require(native.get('status') == 'incomplete' and (native.get('incomplete_details') or {}).get('reason') == 'max_output_tokens', 'split_requires_exact_length_failure')
            for child in children:
                for directory in sorted((Path(output) / 'requests' / child['request_id']).glob('attempt-*')):
                    entry = scheduler.audit_entry(directory, request_data(scheduler.context, child, a_mapping))
                    if entry:
                        accepted.append(entry)
                        break
    assembled = None
    assembly_error = None
    try:
        assembled = assembled_result(scheduler.context, accepted, scheduler.validator)
    except Exception as exc:
        assembly_error = str(exc)
    report = summarize(scheduler.context, accepted, assembled)
    report['assembly_error'] = assembly_error
    captures, tokens, unreadable = [], Counter(), []
    for path in sorted(Path(output).glob('requests/*/attempt-*/response.safe.json')):
        try:
            response = ev.load(path)
        except (SafeFailure, ValueError):
            unreadable.append(str(path.relative_to(output)))
            continue
        status = path.parent / 'status.json'
        channel_mode = None
        if response.get('eventmeta'):
            try:
                channel_mode = verify_sse_capture(response)[1]['channel_mode']
            except SafeFailure:
                require(status.exists() and ev.load(status)['status'] != 'success', 'successful_SSE_invalid')
        usage = ev.usage_for(effective_response(response), 'responses')
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                tokens[key] += value
        captures.append({'path': str(path.relative_to(output)), 'sha256': ev.sha(path),
                         'usage': usage, 'http_status': response.get('http_status'), 'error': response.get('error'),
                         'sse_channel_mode': channel_mode})
    interrupted = [str(p.parent.relative_to(output)) for p in Path(output).glob('requests/*/attempt-*/started.json')
                   if not (p.parent / 'response.safe.json').exists()]
    observed_models = Counter()
    for p in sorted(Path(output).glob('requests/*/attempt-*/status.json')):
        recorded = ev.load(p).get('response_model')
        observed_models[recorded if recorded is not None else 'unknown'] += 1
    container_fills = [{'path': str(p.parent.relative_to(output)),
                        'fields': list(ev.load(p).get('container_fields_filled') or [])}
                       for p in sorted(Path(output).glob('requests/*/attempt-*/status.json'))
                       if ev.load(p).get('container_fields_filled')]
    report.update(technical_failures=[{'path': str(p.relative_to(output)), **ev.load(p)}
                  for p in sorted(Path(output).glob('requests/*/attempt-*/status.json'))
                  if ev.load(p)['status'] != 'success'],
                   interrupted_unknown_cost=interrupted,
                   incomplete_capture_unknown_status=[str(p.parent.relative_to(output)) for p in
                       sorted(Path(output).glob('requests/*/attempt-*/response.safe.json'))
                       if not (p.parent / 'status.json').exists()],
                   unreadable_capture_archives=unreadable,
                   http_attempts_total=len(captures) + len(interrupted) + len(unreadable),
                   judge_model=scheduler.model, observed_response_models=dict(observed_models),
                   sealed_parameters=scheduler.spec['parameters'],
                   container_normalization={
                       'fields': list(CONTAINER_FIELDS),
                       'policy': 'a root field that is absent or null in the model JSON is filled from the '
                                 'sealed request/plan value before schema validation; a present-but-wrong '
                                 'value is still rejected; no business field is touched',
                       'total_attempts_filled': len(container_fills),
                       'attempts': container_fills},
                   sse_validation={'version': SSE_TWO_CHANNEL_VERSION, 'policy': SSE_VALIDATION_POLICY,
                       'channel_modes': dict(Counter(r['sse_channel_mode'] or 'not_responses_sse'
                                                     for r in captures)),
                       'reasoning_extension_used': any(r['sse_channel_mode'] == 'reasoning_channel_extension'
                                                       for r in captures),
                       'frozen_sse_files_sha256': {name: ev.sha(Path(__file__).with_name(name))
                                                   for name in ('safe_http.py', 'sse_fidelity.py')}},
                   http_count=len(captures), http_captures=captures, usage_all_attempts=dict(tokens),
                  http_without_usage=sum(not r['usage'] for r in captures),
                  billing_notice='returned usage only; absent usage/failure, interrupted started-only requests and already inflight cancellation may still be billed',
                  sources_and_protected_hashes_unchanged=True,
                  semantic_validation='not_performed_by_machine_audit')
    if assembled:
        existing = Path(output) / 'assembled.legacy.json'
        if existing.exists():
            require(ev.load(existing) == assembled, 'assembled_business_row_tamper')
    return report, assembled


def save_derived(path, value, text=False):
    content = value if text else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if path.exists():
        require(path.is_file() and not path.is_symlink(), 'derived_report_path_invalid')
        previous = path.read_text()
        if previous != content:
            path.write_text(content, encoding='utf-8')
    else:
        ev.save(path, value, text=text)


def write_reports(directory, report, assembled, context):
    directory = Path(directory)
    save_derived(directory / 'report.json', report)
    lines = ['# Project3 / Space2 正式场景评测', '',
        f"技术状态：{report['status']}；参考主场景117；真实候选N={report['candidate_denominator']}（仅计一次）。",
        f"HTTP已返回记录：{report['http_count']}；HTTP尝试总数（含中断未返回）：{report.get('http_attempts_total')}；无usage：{report['http_without_usage']}；中断未知费用：{len(report.get('interrupted_unknown_cost', []))}；捕获未派生status：{len(report.get('incomplete_capture_unknown_status', []))}。",
        f"裁判模型为sealed plan参数：{report.get('judge_model')}；每块实际响应model必须精确等于该值。生成配置型号 mimo-v2.6-pro；平台未公开实际生成响应model、usage和cost，均unknown。",
        f"封印协议参数：{ev.canonical(report.get('sealed_parameters'))}。本轮按Lead授权关闭裁判推理通道（reasoning_effort=none），"
        f"并把 max_output_tokens 定为 {MAX_OUTPUT_TOKENS}（Lead决定18:05）："
        '冻结safe_http.py的原始SSE捕获上限为2 MiB；关闭思考后按-006实测约895字节/output token，即约2,340输出token处即撞上该上限，'
        '而该失败形态（body_limit_exceeded）不可恢复、一次性拆分也救不回，故把所有过长答案压回模型侧可恢复的 incomplete/max_output_tokens 路径；'
        '超长块由封印的一次性3+2拆分重生成。'
        '开启thinking时（8192+默认、32768+默认、32768+low 三级均已实测）推理通道更早撞上该上限、同样不可恢复（见 aborted-previous-session.md 与 disclosure.md）。'
        '关闭推理后只有assistant单通道，实测每块答案约1.0k–1.3k输出token。本会话的 max_output_tokens 与 gpt-6.1-sol 校准（8192+thinking）不同，'
        '因此裁判深度、token消耗与成本不可与该校准直接比较；未更换裁判模型、未修改任何冻结文件；'
        'max_output_tokens 与 reasoning_effort 均为封印参数，由validate_prepared逐字重算校验，任何漂移都会拒绝。',
        '本轮为全新会话且全程同一裁判模型；此前gpt-6.1-sol部分会话的9个A块未并入本结果（见 aborted-previous-session.md）。',
        'B图由A真实matched_generated_ids确定性反转，不是独立盲评。机器JSON/引文/链接通过不等于业务语义正确。', '',
        '## 相对-006的三处授权变更（Lead决定，全程披露；业务门禁一处未放宽）', '',
        f"（a）容器字段规范化：wire顶层 {ev.canonical(list(report.get('container_normalization', {}).get('fields', list(CONTAINER_FIELDS))))} "
        '四项只是封印 request/plan 值的回显副本（schema版本、context input_id、request_id、唯一可接受的完成状态），不含任何业务判断。'
        '若模型JSON中该字段**缺失或为null**，则在schema校验前按封印值补齐；'
        f"本会话触发补齐的attempt数：{report.get('container_normalization', {}).get('total_attempts_filled', 0)}"
        f"（{ev.canonical(report.get('container_normalization', {}).get('attempts', []))}）。"
        '若字段存在但**值错误**，仍按原schema const拒绝，不做静默覆盖。业务/来源/引文/链接/复核队列校验强度一律未变。',
        f"（b）格式修复预算：generations_max={GENERATIONS_MAX}（1次初代 + 最多{FORMAT_REPAIRS_MAX}次有界格式修复），"
        f"transport重试≤{TRANSPORT_RETRIES_MAX}；两者均为封印参数，由validate_prepared逐字重算校验。"
        '此变更只放大修复次数，不放宽任何机器校验。',
        f"（c）输出额度：max_output_tokens={MAX_OUTPUT_TOKENS}（封印参数，见上文为何必须低于2 MiB捕获上限对应的约2,340 token）。"
        '超长块只会在模型侧以 incomplete/max_output_tokens 结束，并由封印的一次性3+2拆分重生成；本报告逐条披露拆分记录。', '',
        f"SSE校验封印值：{report['sse_validation']['policy']}（{report['sse_validation']['version']}）。"
        f"各capture通道模式：{ev.canonical(report['sse_validation']['channel_modes'])}。冻结safe_http.py与sse_fidelity.py字节未改，其sha256见report.json。",
        '无error的capture只走冻结sse_fidelity.assemble原路径、完全不过滤；仅当capture的error本身是冻结sse_fidelity_*保真错误且唯一成因是推理通道content part时启用扩展：'
        '逐条断言被移除事件属reasoning项且part.type=reasoning_text、事件按原顺序把序列号投影为连续载波索引（冻结assemble要求连续，故必须投影），'
        '对投影调用同一冻结assemble并要求成功且assistant消息通道与completed body完全一致；'
        '推理通道则按同等严格度校验原事件的added→delta→done→part.done→item.done链条、原序列连续、唯一created/completed、无错误事件、推理文本不得进入assistant输出。',
        '本轮封印 reasoning_effort=none，预期capture为pristine单通道、该扩展不触发；若提供商仍发出推理通道，同一严格校验照旧适用。'
        '扩展未换模型、未修改任何冻结文件；reasoning内容不进入任何业务判断。', '',
        '## 覆盖（full与full+partial分开）', '',
        '|类别|分母|full|partial|missing|unresolved|full率|full+partial率|', '|---|---:|---:|---:|---:|---:|---:|---:|']
    if assembled:
        scores = report['coverage_scores']
        for category, score in [('全部', scores['all117']), *scores['categories'].items()]:
            c = score['counts']
            lines.append(f"|{category}|{score['denominator']}|{c.get('full',0)}|{c.get('partial',0)}|{c.get('missing',0)}|{c.get('unresolved',0)}|{score['full']:.2%}|{score['full_plus_partial']:.2%}|")
        lines += ['', '## 候选业务、粒度及重复（不并入覆盖率）', '']
        for field, counts in report['candidate_counts'].items():
            lines += [f"- {field}：" + '；'.join(f'{k}={v}' for k,v in counts.items())]
        lines += [f"- 重复组：{len(assembled['duplicate_groups'])}；人工复核项：{len(assembled['review_queue'])}。", '', '## 限制与复核', '']
        lines += ['- ' + text for text in assembled['limitations']]
    else:
        lines += ['', f"尚有参考{report['baseline_technical_unassessed']}和候选{report['candidate_technical_unassessed']}技术未评；不输出全量得分，不将技术失败归入missing。"]
    lines += ['', '资料限于用户本项目导入交易规则API解析原文、原117参考和本次47真实候选。原gold不重审批，本报告不是法规有效性证明，也不是40校准准确率或模型间一致率。',
        '所有模型行文字/标签原样保留，仅container装配；全量结果由原frozen Validator.legacy复核。',
        'usage按每条原capture唯一累计；无usage、失败及取消时已inflight请求仍可能计费。离线audit不读凭据或访问网络。',
        '来源链、实际model、请求参数、失败及每行origin见report.json与requests原SSE/derived归档。', '', '## 返回usage（所有attempt）', '', ev.canonical(report['usage_all_attempts'])]
    save_derived(directory / 'report.md', '\n'.join(lines) + '\n', text=True)
    if assembled:
        for filename, field, key in [('baseline-coverage.csv', 'baseline_judgments', 'baseline_id'),
                                     ('candidate-judgments.csv', 'generated_judgments', 'generated_id'),
                                     ('review-queue.csv', 'review_queue', 'target_id')]:
            rows = assembled[field]
            columns = list(rows[0]) if rows else ['target_type', 'target_id', 'issue_type', 'reason']
            columns += ['origin']
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                copied = {k: ev.canonical(v) if isinstance(v, (list, dict)) else v for k,v in row.items()}
                copied['origin'] = ev.canonical(report['row_provenance'].get(row[key]))
                writer.writerow(copied)
            save_derived(directory / filename, stream.getvalue(), text=True)


def execute(output, workers=2, resume=False, transport=None, model=None):
    require(1 <= workers <= 2, 'workers_max_two')
    output = Path(output)
    with pid_lock(output):
        spec, context = validate_prepared(output, model)
        started = output / 'execution.started.json'
        require(resume or not started.exists(), 'execution_exists_use_resume')
        if started.exists():
            require(ev.load(started)['plan_sha256'] == ev.sha(output / 'plan.json'), 'resume_plan_fingerprint_mismatch')
        # Verify every already archived row BEFORE selected credentials or HTTP on resume.
        audit(output, model)
        if transport is None:
            public = provider_public('new-api', spec['base_url'], spec['api_key_env'])
            endpoint = public['endpoint'].removesuffix('/chat/completions') + '/responses'
            require(endpoint == spec['endpoint'], 'public_endpoint_changed')
            key, resolved, _ = get_provider('new-api', spec['base_url'], spec['api_key_env'])
            require(resolved.removesuffix('/chat/completions') + '/responses' == endpoint, 'selected_endpoint_changed')
            transport = Transport(key, endpoint)
        if not started.exists():
            ev.save(started, {'plan_sha256': ev.sha(output / 'plan.json'), 'workers': workers})
        scheduler = ScopedScheduler(output, transport, model)
        targets_A = [t for t in spec['target_plan'] if t['stage'] == 'A']
        accepted = []
        smoke = scheduler.evaluate_group(targets_A[0])
        if smoke:
            accepted.extend(smoke)
            response = ev.load(output / smoke[0]['origin']['path'] / 'response.safe.json')
            # Unknown public model context capacity: returned usage and complete smoke gate concurrency.
            if sum(len(e['wire'].get('baseline_judgments', {})) for e in smoke) == len(targets_A[0]['targets']) and all(ev.usage_for(effective_response(ev.load(output / e['origin']['path'] / 'response.safe.json')), 'responses') for e in smoke):
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    accepted += [e for group in pool.map(scheduler.evaluate_group, targets_A[1:]) for e in group]
                try:
                    mapping = inverse_mapping(context, accepted)
                except SafeFailure:
                    mapping = None
                if mapping and not scheduler.cancel.is_set():
                    targets_B = [t for t in spec['target_plan'] if t['stage'] == 'B']
                    b_smoke = scheduler.evaluate_group(targets_B[0], mapping)
                    if b_smoke:
                        accepted.extend(b_smoke)
                        response = ev.load(output / b_smoke[0]['origin']['path'] / 'response.safe.json')
                        if sum(len(e['wire'].get('generated_judgments', {})) for e in b_smoke) == len(targets_B[0]['targets']) and all(ev.usage_for(effective_response(ev.load(output / e['origin']['path'] / 'response.safe.json')), 'responses') for e in b_smoke):
                            with ThreadPoolExecutor(max_workers=workers) as pool:
                                accepted += [e for group in pool.map(lambda t: scheduler.evaluate_group(t, mapping), targets_B[1:]) for e in group]
        report, assembled = audit(output, model)
        if assembled and not (output / 'assembled.legacy.json').exists():
            ev.save(output / 'assembled.legacy.json', assembled)
        for path in output.rglob('*'):
            if path.is_file():
                text = path.read_text(encoding='utf-8')
                require(scheduler.redactor.clean(text) == text, 'selected_credential_echo_in_artifacts')
        report['selected_credential_echo_scan_passed'] = True
        report['smoke_gate'] = 'complete original SSE plus exact model and returned usage required before remaining A/B queue'
        session = output / f'session-{len(list(output.glob("session-*"))) + 1:03d}'
        session.mkdir()
        write_reports(session, report, assembled, context)
        write_reports(output, report, assembled, context)
        return report


def probe_models(output, models=PROBE_MODELS, base_url=None, api_key_env=None, transport=None):
    """Bounded judge-model probe: at most PROBE_MAX_REQUESTS minimal streaming Responses calls.

    Each attempt is recorded verbatim (redacted) under <output>/model-probe/. An id passes only
    with HTTP 200, exact model echo, a valid strict SSE lifecycle and schema-valid structured output.
    """
    output = Path(output)
    require(not output.is_symlink(), 'probe_output_symlink_refused')
    existing = {p.name for p in output.iterdir()} if output.is_dir() else set()
    require(not output.exists() or existing <= PREEXISTING_RUN_ENTRIES, 'probe_output_not_empty')
    require(not (output / 'model-probe').exists(), 'probe_output_has_model_probe')
    models = [m for m in models if m]
    require(1 <= len(models) <= PROBE_MAX_REQUESTS, 'probe_model_budget_invalid')
    output.mkdir(parents=True, exist_ok=True)
    directory = output / 'model-probe'
    directory.mkdir()
    if transport is None:
        public = provider_public('new-api', base_url, api_key_env)
        endpoint = public['endpoint'].removesuffix('/chat/completions') + '/responses'
        require(endpoint.endswith('/responses'), 'probe_endpoint_invalid')
        key, resolved, _ = get_provider('new-api', base_url, api_key_env)
        require(resolved.removesuffix('/chat/completions') + '/responses' == endpoint, 'selected_endpoint_changed')
        transport = Transport(key, endpoint)
    redactor = getattr(transport, 'redactor', Redactor())
    log, attempts, selected, requests_sent = [], [], None, 0
    for index, model in enumerate(models, 1):
        if selected is not None or requests_sent >= PROBE_MAX_REQUESTS:
            break
        model = require_model_id(model, 'probe_model_id_invalid')
        body = ev.body_for(model, [{'role': 'user', 'content': '结构化探针：只返回 JSON 对象 {"ok": true}，不要其他文字。'}],
                           PROBE_SCHEMA, 'json_schema', 4096, 'responses', 'omit-unique-items', True)
        require(redactor.clean(body) == body, 'probe_request_credential_echo')
        attempt = directory / f'attempt-{index:02d}'
        attempt.mkdir()
        ev.save(attempt / 'request.safe.json', body)
        requests_sent += 1
        try:
            response = transport(body)
        except Exception as exc:
            response = {'http_status': None, 'body': None, 'error': 'transport_' + type(exc).__name__}
        if redactor.clean(response) != response:
            response = redactor.clean(response)
            response['error'] = 'credential_echo_redacted'
        ev.save(attempt / 'response.safe.json', response)
        record = {'model_requested': model, 'http_status': response.get('http_status'),
                  'error': response.get('error'), 'echoed_model': None, 'usage': None,
                  'sse_strict_assemble': None, 'sse_channel_mode': None,
                  'structured_output_valid': False, 'status': 'failed', 'errors': []}
        native = effective_response(response)
        content = None
        try:
            require(response.get('http_status') == 200 and capture_error_acceptable(response),
                    response.get('error') or 'http_' + str(response.get('http_status')))
            _, manifest = strict_two_channel_assemble(response)
            record['sse_strict_assemble'] = 'passed'
            record['sse_channel_mode'] = manifest['channel_mode']
            require(isinstance(native, dict), 'probe_response_not_object')
            record['echoed_model'] = native.get('model')
            record['usage'] = ev.usage_for(native, 'responses')
            ev.validate_response_model(native, model)
            content, finish = ev.parse_content(native, 'responses')
            ev.save(attempt / 'content.original.txt', content, text=True)
            require(finish == 'stop', 'probe_response_not_complete')
            wire = ev.strict_json(content.strip())
            errors = list(Draft202012Validator(PROBE_SCHEMA).iter_errors(wire))
            require(not errors and wire.get('ok') is True,
                    'probe_structured_output_invalid: ' + '; '.join(e.message for e in errors[:3]))
            record['structured_output_valid'] = True
            record['status'] = 'passed'
        except Exception as exc:
            record['errors'] = [str(exc) if isinstance(exc, (SafeFailure, ValueError)) else 'local_' + type(exc).__name__]
        ev.save(attempt / 'status.json', record)
        events = [e['data'].get('type') for e in (response.get('eventmeta') or {}).get('events', [])
                  if isinstance(e.get('data'), dict)]
        attempts.append({k: v for k, v in record.items() if k != 'errors'} | {'errors': record['errors']})
        log += [f"## {attempt.name}", 'model_requested: ' + model,
                'request_body: ' + ev.canonical(body), 'http_status: ' + repr(record['http_status']),
                'echoed_model: ' + repr(record['echoed_model']), 'usage: ' + ev.canonical(record['usage']),
                'sse_strict_assemble: ' + repr(record['sse_strict_assemble']),
                'sse_channel_mode: ' + repr(record['sse_channel_mode']),
                'sse_event_types: ' + ev.canonical(events), 'structured_output: ' + (content or ''),
                'errors: ' + ev.canonical(record['errors']), '']
        if record['status'] == 'passed':
            selected = model
    passing = attempts[-1] if selected else {}
    summary = {'status': 'passed' if selected else 'failed', 'selected_model': selected,
               'echoed_model': passing.get('echoed_model') if selected else None,
               'http_status': passing.get('http_status') if selected else None,
               'usage': passing.get('usage') if selected else None,
               'endpoint': getattr(transport, 'endpoint', None), 'requests_sent': requests_sent,
               'budget': PROBE_MAX_REQUESTS, 'models_tried': [a['model_requested'] for a in attempts],
               'attempts': attempts,
               'policy': 'HTTP 200 + exact model echo + sealed strict SSE validation (' + SSE_VALIDATION_POLICY + ') + schema-valid structured output; no fallback to other models or chat_completions'}
    ev.save(directory / 'probe.json', summary)
    header = ['# judge model probe (verbatim, redacted)', '', 'policy: ' + summary['policy'], '']
    save_text_once(directory / 'probe.log', '\n'.join(header + log + ['verdict: ' + ev.canonical(
        {k: summary[k] for k in ('status', 'selected_model', 'echoed_model', 'http_status', 'requests_sent')})]) + '\n')
    for path in sorted(directory.rglob('*')):
        if path.is_file():
            require(redactor.clean(path.read_text(encoding='utf-8')) == path.read_text(encoding='utf-8'),
                    'probe_selected_credential_echo_in_artifacts')
    return summary


def save_text_once(path, content):
    path = Path(path)
    if path.exists():
        require(path.is_file() and not path.is_symlink() and path.read_text(encoding='utf-8') == content,
                'archived_attempt_artifact_changed')
        return
    ev.save(path, content, text=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--scenarios', required=True, type=Path)
    p.add_argument('--source-manifest', required=True, type=Path)
    p.add_argument('--output-dir', required=True, type=Path)
    p.add_argument('--target-size', type=int, default=5)
    p.add_argument('--base-url')
    p.add_argument('--api-key-env')
    p.add_argument('--model', default=DEFAULT_MODEL)
    p = sub.add_parser('probe')
    p.add_argument('--output-dir', required=True, type=Path)
    p.add_argument('--models', default=','.join(PROBE_MODELS))
    p.add_argument('--base-url')
    p.add_argument('--api-key-env')
    for name in ('execute', 'audit', 'report'):
        p = sub.add_parser(name)
        p.add_argument('--run-dir', required=True, type=Path)
        p.add_argument('--model', required=(name == 'execute'))
        if name == 'execute':
            p.add_argument('--execute', required=True, action='store_true')
            p.add_argument('--resume', action='store_true')
            p.add_argument('--workers', type=int, default=2)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            spec = prepare(args.scenarios, args.source_manifest, args.output_dir, args.target_size, args.base_url, args.api_key_env, args.model)
            print(ev.canonical({'status': 'prepared_offline', 'model': spec['model'], 'gold': 117, 'N': spec['candidate_count'],
                'planned_calls': spec['planned_calls'], 'fingerprint_sha256': spec['fingerprint_sha256'],
                'model_probe': spec['model_probe']}))
            return 0
        if args.command == 'probe':
            summary = probe_models(args.output_dir, args.models.split(','), args.base_url, args.api_key_env)
            print(ev.canonical({k: summary[k] for k in ('status', 'selected_model', 'echoed_model', 'http_status',
                                                        'usage', 'requests_sent', 'models_tried', 'endpoint')}))
            return 0 if summary['status'] == 'passed' else 2
        report = execute(args.run_dir, args.workers, args.resume, model=args.model) if args.command == 'execute' \
            else audit(args.run_dir, args.model)[0]
        print(ev.canonical(report))
        return 0 if report['status'] == 'complete' else 1
    except Exception as exc:
        print('error: ' + (str(exc) if isinstance(exc, SafeFailure) else 'local_' + type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
