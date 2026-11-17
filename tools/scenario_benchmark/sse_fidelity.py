"""Strict carrier-only assembly from fully terminated Responses event evidence."""
import copy
import hashlib
from safe_http import SafeFailure


def assemble(events, completed, raw_text):
    def require(ok, reason):
        if not ok:
            raise SafeFailure('sse_fidelity_' + reason)
    rows = [e['data'] for e in events if isinstance(e.get('data'), dict)]
    require(bool(rows), 'empty_events')
    require(all(type(e.get('sequence_number')) is int for e in rows), 'sequence_type')
    require([e.get('sequence_number') for e in rows] == list(range(len(rows))), 'sequence_gap')
    require(rows[0]['type'] == 'response.created', 'created_order')
    created = rows[0].get('response', {})
    rid, model = created.get('id'), created.get('model')
    require(isinstance(rid, str) and bool(rid) and isinstance(model, str) and bool(model), 'created_identity')
    require(created.get('status') == 'in_progress' and created.get('error') is None and created.get('incomplete_details') is None, 'created_status')
    require(sum(e['type'] == 'response.created' for e in rows) == 1, 'duplicate_created')
    require(sum(e['type'] == 'response.completed' for e in rows) == 1 and rows[-1]['type'] == 'response.completed', 'terminal_order')
    require(completed.get('status') == 'completed' and completed.get('error') is None and completed.get('incomplete_details') is None, 'terminal_status')
    items, segments, ids = {}, {}, set()
    for e in rows:
        kind = e['type']
        require(kind not in ('error', 'response.failed', 'response.incomplete'), 'error_event')
        if 'response' in e:
            r = e['response']
            require(isinstance(r, dict) and r.get('id') == rid and r.get('model') == model, 'response_identity')
        if 'response_id' in e:
            require(e['response_id'] == rid, 'cross_response')
        if kind == 'response.output_item.added':
            oi, item = e.get('output_index'), e.get('item', {})
            require(type(oi) is int and oi == len(items), 'output_index')
            require(isinstance(item.get('id'), str) and item['id'] not in ids, 'item_identity')
            require(item.get('type') != 'message' or (item.get('role') == 'assistant' and item.get('content') == [] and item.get('status') == 'in_progress'), 'assistant_shape')
            ids.add(item['id']); items[oi] = {'added': item, 'done': None, 'parts': []}
        elif kind == 'response.output_item.done':
            oi, item = e.get('output_index'), e.get('item', {})
            require(type(oi) is int and oi >= 0 and oi in items and items[oi]['done'] is None, 'item_done_order')
            added = items[oi]['added']
            require(item.get('id') == added['id'] and item.get('type') == added.get('type'), 'item_done_identity')
            if item.get('type') == 'message':
                require(item.get('role') == 'assistant' and item.get('status') == 'completed', 'item_not_completed')
                require(bool(items[oi]['parts']), 'empty_assistant')
                parts = [segments[(oi, ci)] for ci in items[oi]['parts']]
                require(all(p['part_done'] for p in parts), 'item_before_part_done')
                require(item.get('content') == [p['final_part'] for p in parts], 'item_content_conflict')
            items[oi]['done'] = item
        elif kind.startswith('response.content_part.') or kind.startswith('response.output_text.'):
            oi, ci = e.get('output_index'), e.get('content_index')
            require(type(oi) is int and oi in items and type(ci) is int and ci >= 0, 'segment_indices')
            item = items[oi]
            require(item['added'].get('type') == 'message' and item['done'] is None and e.get('item_id') == item['added']['id'], 'segment_item_identity')
            key = (oi, ci)
            if kind == 'response.content_part.added':
                part = e.get('part', {})
                require(ci == len(item['parts']) and key not in segments and part.get('type') == 'output_text' and part.get('text') == '', 'part_added_shape')
                item['parts'].append(ci)
                segments[key] = {'deltas': [], 'delta_sequences': [], 'done': False, 'part_done': False, 'added_sequence': e['sequence_number']}
            else:
                require(key in segments, 'segment_before_added')
                p = segments[key]
                if kind == 'response.output_text.delta':
                    require(not p['done'] and not p['part_done'] and isinstance(e.get('delta'), str), 'delta_order')
                    p['deltas'].append(e['delta']); p['delta_sequences'].append(e['sequence_number'])
                elif kind == 'response.output_text.done':
                    require(not p['done'] and not p['part_done'] and isinstance(e.get('text'), str), 'duplicate_or_invalid_done')
                    require(''.join(p['deltas']) == e['text'], 'delta_done_conflict')
                    p['done'] = True; p['text'] = e['text']; p['done_sequence'] = e['sequence_number']
                elif kind == 'response.content_part.done':
                    part = e.get('part', {})
                    require(p['done'] and not p['part_done'] and part.get('type') == 'output_text' and part.get('text') == p['text'], 'part_done_conflict')
                    p['part_done'] = True; p['final_part'] = part; p['part_done_sequence'] = e['sequence_number']
                else:
                    require(False, 'unknown_text_event')
        elif kind.startswith('response.refusal.'):
            require(False, 'refusal_not_text')
    require(all(i['done'] is not None for i in items.values()), 'unfinished_item')
    require(bool(segments) and all(p['done'] and p['part_done'] for p in segments.values()), 'unfinished_text')
    output = []
    for oi, item in sorted(items.items()):
        if item['added'].get('type') == 'message':
            out = {k: copy.deepcopy(item['done'][k]) for k in ('id', 'type', 'role', 'status')}
            out['content'] = [copy.deepcopy(segments[(oi, ci)]['final_part']) for ci in item['parts']]
            output.append(out)
    original = completed.get('output')
    require(isinstance(original, list), 'completed_output_shape')
    if original:
        existing = [i for i in original if i.get('type') == 'message' and i.get('role') == 'assistant']
        signature = lambda xs: [(i.get('id'), [(p.get('type'), p.get('text')) for p in i.get('content', [])]) for i in xs]
        require(signature(existing) == signature(output), 'nonempty_completed_conflict')
    derived = copy.deepcopy(completed)
    if not original:
        derived['output'] = output
    manifest = {'version': '1.1.5', 'carrier_only': True, 'original_completed_output_empty': not original,
                'response_id': rid, 'created_model': model, 'completed_model': completed['model'],
                'completed_sequence': rows[-1]['sequence_number'], 'terminal_count': 1,
                'raw_sse_sha256': hashlib.sha256(raw_text.encode('utf-8')).hexdigest(),
                'original_completed_sha256': hashlib.sha256(__import__('json').dumps(completed, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'segments': [{'segment_id': items[oi]['added']['id'] + ':' + str(ci), 'item_id': items[oi]['added']['id'],
                              'output_index': oi, 'content_index': ci, 'added_sequence': p['added_sequence'],
                              'delta_sequences': p['delta_sequences'], 'done_sequence': p['done_sequence'],
                              'part_done_sequence': p['part_done_sequence'], 'delta_count': len(p['deltas']),
                              'delta_equals_done_equals_part_equals_item': True,
                              'text_sha256': hashlib.sha256(p['text'].encode()).hexdigest()}
                             for (oi, ci), p in sorted(segments.items())]}
    return derived, manifest
