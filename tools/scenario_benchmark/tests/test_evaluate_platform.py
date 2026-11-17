"""Platform evaluation regressions: mock SQLite/model only, no real HTTP."""
import copy
import io
import contextlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_platform as ep
import evaluate_scenarios as ev
from safe_http import SafeFailure, Redactor


def fixture(database):
    connection = sqlite3.connect(database)
    tables = [('generation_tasks', ep.TASK_COLUMNS), ('requirements', ep.REQUIREMENT_COLUMNS),
              ('task_test_point_checkpoints', ep.CHECKPOINT_COLUMNS), ('test_points', ep.POINT_COLUMNS)]
    values = {'generation_tasks': [6, 5, 1, 'failed', 'standard', '2026-01-01', '2026-01-02'],
              'requirements': [5, 1, '集合竞价', 'auction task', '[]', None, '2026-01-01', '2026-01-02'],
              'task_test_point_checkpoints': [1, 6, 2, 1, 1, 'confirmed', '2026-01-01', '2026-01-02']}
    for table, columns in tables:
        connection.execute('CREATE TABLE ' + table + '(' + ','.join(columns) + ',api_key TEXT,password TEXT,resume_claim_token TEXT)')
        if table in values:
            connection.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ',api_key,password,resume_claim_token) VALUES(' + ','.join('?' for _ in range(len(columns)+3)) + ')', values[table] + ['forbidden-secret'] * 3)
    for i in range(1, 8):
        row = [i, 6, 1, 'mock-'+str(i), '标题 '+str(i), '原关注点\n保留 ', 'boundary', 'P0', i-1, 0, 1, '2026-01-01', '2026-01-02']
        connection.execute('INSERT INTO test_points VALUES(' + ','.join('?' for _ in range(len(row)+3)) + ')', row + ['forbidden-secret'] * 3)
    connection.execute('CREATE TABLE test_point_citations(id INTEGER,test_point_id INTEGER,citation_id INTEGER)')
    connection.execute('INSERT INTO test_point_citations VALUES(1,1,42)')
    connection.execute('CREATE TABLE task_events(id INTEGER,task_id INTEGER,step TEXT,message TEXT,created_at TEXT,detail_json TEXT)')
    connection.execute('INSERT INTO task_events VALUES(1,6,\'test_points\',\'完成7点\',\'2026-01-01\',?)', (json.dumps({'checkpoint_id':1,'count':7,'api_key':'forbidden-secret','password':'forbidden-secret'}),))
    connection.execute("INSERT INTO task_events VALUES(2,6,'error','forbidden-secret','2026-01-01','{}')")
    connection.commit()
    connection.close()


def result(data):
    return {'schema_version': '1.0', 'input_id': data['input_id'], 'evaluation_status': 'complete',
            'baseline_judgments': [{'baseline_id': r['id'], 'coverage':'missing', 'business_consistency':'not_assessed',
                                    'matched_generated_ids':[], 'evidence':[], 'covered_aspects':[],
                                    'missing_aspects':['mock absent intent'], 'source_refs':[], 'reason':'mock only'} for r in data['baseline']],
            'generated_judgments': [{'generated_id': r['id'], 'relation':'new', 'matched_baseline_ids':[],
                                     'granularity':'appropriate', 'business_consistency':'not_assessed',
                                     'evidence':[{'generated_id':r['id'],'quote':r['title']}], 'source_refs':[], 'reason':'mock only'} for r in data['generated_scenarios']],
            'duplicate_groups':[], 'review_queue':[{'target_type':'generated', 'target_id':r['id'], 'issue_type':'novelty', 'reason':'subset only'} for r in data['generated_scenarios']],
            'limitations':['mock offline judgment, not actual semantic result']}


def node(data, accepted=True):
    return {'input_id':data['input_id'], 'baseline_ids':data['task_scope']['baseline_ids'], 'children':[],
            'accepted':result(data) if accepted else None,
            'audit':{'http_attempts':[{'usage':{'total_tokens':30},'usage_original':{'input_tokens':10,'output_tokens':20},'response_model':'gpt-6.1-sol'}],
                     'samples':[{'errors': [] if accepted else ['mock_technical_failure']}]}}


class PlatformTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.root / 'mock.db'
        fixture(self.database)
        self.snapshot, self.scenarios, self.manifest = ep.snapshot_sqlite(self.database, 6)
        self.gold = ev.load(ev.GOLD)
        self.inputs = ep.partition_inputs(self.gold, self.scenarios, self.manifest)

    def test_readonly_whitelist_database_hash_and_verbatim(self):
        before = ev.sha(self.database)
        snapshot, scenes, manifest = ep.snapshot_sqlite(self.database, 6)
        self.assertEqual(ev.sha(self.database), before)
        text = ev.canonical(snapshot)
        self.assertNotIn('forbidden-secret', text)
        self.assertNotIn('api_key', text)
        self.assertNotIn('password', text)
        self.assertNotIn('resume_claim_token', text)
        self.assertEqual(len(snapshot['event_evidence']), 1)
        self.assertEqual(snapshot['event_evidence'][0]['count'], 7)
        self.assertEqual(manifest['provenance'], 'current_database_snapshot')
        self.assertEqual(manifest['method'], 'sqlite_read_only')
        self.assertEqual(manifest['human_edit_status'], 'unknown')
        self.assertEqual(manifest['snapshot_canonical_sha256'], ev.digest(ev.canonical(snapshot)))
        self.assertEqual(scenes['scenarios'][0]['text'], '标题 1\n原关注点\n保留 ')
        self.assertEqual(len(scenes['scenarios']), 7)
        self.assertFalse(scenes['scenarios'][0]['source_metadata']['is_selected'])
        self.assertTrue(scenes['scenarios'][0]['source_metadata']['is_excluded'])

    def test_117_exact_once_all_seven_every_block_gold_unchanged(self):
        self.assertEqual(len(self.inputs), 24)
        self.assertEqual(sum(len(d['baseline']) for d in self.inputs),117)
        self.assertEqual({r['id'] for d in self.inputs for r in d['baseline']},{r['id'] for r in self.gold['scenarios']})
        for data in self.inputs:
            self.assertLessEqual(len(data['baseline']),5)
            self.assertEqual(data['generated_scenarios'],self.scenarios['scenarios'])
            self.assertEqual(set(data['task_scope']['category_ids']),set(ep.CATEGORIES))
            self.assertEqual(data['task_scope']['reference_scope'],'selected_subset')
            ev.Validator().input(data)

    def test_dropped_candidate_and_duplicate_gold_partition_refused(self):
        bad = copy.deepcopy(self.inputs)
        bad[1]['generated_scenarios'].pop()
        with self.assertRaises(SafeFailure): ep.validate_partition(bad,self.gold,self.scenarios['scenarios'])
        bad = copy.deepcopy(self.inputs)
        bad[1]['baseline'][0] = bad[0]['baseline'][0]
        with self.assertRaises(SafeFailure): ep.validate_partition(bad,self.gold,self.scenarios['scenarios'])

    def test_complete_auc19_other98_and_candidate_denominator_seven(self):
        summary = ep.summarize(self.inputs,[node(d) for d in self.inputs])
        self.assertEqual(summary['status'],'complete')
        self.assertEqual(summary['task_scope_auc19']['planned_denominator'],19)
        self.assertEqual(summary['other98_diagnostic_not_task_missing']['planned_denominator'],98)
        self.assertEqual(summary['candidate_denominator'],7)
        self.assertEqual(summary['candidate_global_unmatched_pending_review_count'],7)
        self.assertEqual(summary['http_count'],24)
        self.assertEqual(summary['usage_all_http_attempts']['total_tokens'],720)

    def test_failure_never_missing_or_global_new(self):
        nodes = [node(d) for d in self.inputs]
        nodes[0] = node(self.inputs[0],False)
        summary = ep.summarize(self.inputs,nodes)
        self.assertEqual(summary['status'],'incomplete')
        self.assertEqual(summary['task_scope_auc19']['technical_unassessed'],5)
        self.assertEqual(summary['task_scope_auc19']['counts']['missing'],14)
        self.assertEqual(summary['candidate_global_unmatched_pending_review_count'],0)
        self.assertTrue(all(r['global_relation']=='incomplete_not_global_new' for r in summary['candidate_summaries']))

    def test_union_raw_links_conflicts_not_majority_and_duplicate_sets(self):
        nodes = [node(d) for d in self.inputs]
        first = nodes[0]['accepted']['generated_judgments'][0]
        second = nodes[1]['accepted']['generated_judgments'][0]
        first.update(relation='matched',matched_baseline_ids=[self.inputs[0]['baseline'][0]['id']],business_consistency='consistent')
        second.update(relation='extension',matched_baseline_ids=[self.inputs[1]['baseline'][0]['id']],business_consistency='conflict',granularity='too_broad')
        group={'generated_ids':['TP-6-1','TP-6-2'],'group_id':'mock-group','reason':'mock','evidence':[]}
        nodes[0]['accepted']['duplicate_groups']=[group]
        nodes[1]['accepted']['duplicate_groups']=[copy.deepcopy(group)]
        summary=ep.summarize(self.inputs,nodes)
        row=summary['candidate_summaries'][0]
        self.assertEqual(len(row['global_matched_baseline_ids']),2)
        self.assertEqual(row['business_consistency_distribution']['conflict'],1)
        self.assertTrue(row['requires_cross_block_review'])
        self.assertEqual(len(row['raw_links']),24)
        self.assertEqual(len(summary['duplicate_groups_union']),1)
        self.assertEqual(len(summary['duplicate_groups_union'][0]['per_block_evidence']),2)

    def test_recovered_parent_not_double_counted_http_preserved(self):
        parent=node(self.inputs[0],False)
        for i,block in enumerate((self.inputs[0]['baseline'][:3],self.inputs[0]['baseline'][3:]),1):
            child=copy.deepcopy(self.inputs[0]); child['input_id'] += '-R'+str(i)
            child['baseline']=block; child['task_scope']['baseline_ids']=[r['id'] for r in block]
            parent['children'].append(node(child))
        summary=ep.summarize(self.inputs,[parent]+[node(d) for d in self.inputs[1:]])
        self.assertEqual(summary['status'],'complete')
        self.assertEqual(summary['full117_diagnostic_not_task_pass_rate']['valid_rows'],117)
        self.assertEqual(summary['http_count'],26)
        self.assertEqual(summary['candidate_denominator'],7)
        self.assertTrue(summary['technical_failures_and_recovery'][0]['recovered_by_children'])

    def test_exclusive_prepare_refuses_before_read_database(self):
        output=self.root/'exists';output.mkdir()
        with patch.object(ep,'snapshot_sqlite',side_effect=AssertionError('must not read')):
            with self.assertRaises(SafeFailure):ep.prepare(self.database,6,output)

    def test_dry_single_flags_no_execute_no_calibration(self):
        args=ep.single_args(Path('input.json'),Path('new-output'))
        self.assertEqual(args.command,'single')
        self.assertFalse(args.execute)
        self.assertTrue(args.stream)
        self.assertEqual(args.api_protocol,'responses')
        self.assertEqual(args.responses_schema_compat,'omit-unique-items')
        self.assertEqual(args.model,'gpt-6.1-sol')

    def test_cross_run_auth_cancel_stops_underlying_http(self):
        class Fake:
            redactor=Redactor();endpoint='https://example.invalid/responses';calls=0
            def __call__(self,body):
                self.calls+=1
                return {'http_status':401,'body':{},'error':None}
        fake=Fake();transport=ep.CancellableTransport(fake)
        transport({})
        with self.assertRaises(SafeFailure):transport({})
        self.assertEqual(fake.calls,1)

    def test_prepare_mock_all_dry_runs_and_protected_hashes(self):
        output=self.root/'prepared'
        with patch.object(ep,'protected_hashes',return_value={str(ev.GOLD):ev.sha(ev.GOLD)}), patch.object(ev,'provider_public',return_value={'provider':'new-api','endpoint':'https://example.invalid/v1/chat/completions','credential_ref_name':'MOCK_REF'}), patch.object(ev,'get_provider',side_effect=AssertionError('no auth')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ep.prepare(self.database,6,output),0)
        plan,inputs,protected=ep.validate_prepared(output)
        self.assertEqual(len(inputs),24)
        self.assertEqual(len(list((output/'dry-run').glob('*/run.json'))),24)
        self.assertEqual(len(list(output.glob('**/*.response.safe.json'))),0)
        self.assertTrue(plan['dry_run_validated_all_blocks'])
        with (output/'snapshot.json').open('a') as handle:handle.write(' ')
        with self.assertRaises(SafeFailure):ep.validate_prepared(output)

    def test_all_mock_judgments_use_frozen_validator(self):
        validator=ev.Validator()
        for data in self.inputs:
            validator.legacy(data,result(data))

    def test_csv_117_baselines_seven_candidates(self):
        summary=ep.summarize(self.inputs,[node(d) for d in self.inputs])
        ep.write_reports(self.root,summary)
        import csv
        with (self.root/'baseline-coverage.csv').open() as handle:self.assertEqual(len(list(csv.DictReader(handle))),117)
        with (self.root/'candidate-summary.csv').open() as handle:self.assertEqual(len(list(csv.DictReader(handle))),7)


if __name__=='__main__':unittest.main()
