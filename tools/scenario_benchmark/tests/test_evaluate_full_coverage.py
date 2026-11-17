"""Synthetic transport and temporary snapshots only; no credential or real model access."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import evaluate_full_coverage as full
from test_evaluate_project_scenarios import wire_for, capture, reasoning_capture

PUBLIC = {'provider':'magpie','endpoint':'https://mock.example/v1/responses',
          'credential_ref_name':'MOCK','explicit_environment':False}
CONTROL = full.ROOT/'data/evaluation/scenario-benchmark-v0.1/runs/project3-space2-003/generation/scenarios.json'


def scenes(n=6):
    return [{'id':f'TP-{20+i}-{200+i}','category':None,'title':'真实测试意图',
        'description':'甲；乙','test_focus':'甲；乙','text':'真实测试意图\n甲；乙',
        'source_task_id':20+i,'source_point_id':200+i,
        'source_metadata':{'internal_coverage':'NOT_FOR_JUDGE','manual_answer':'DO_NOT_SEND'}} for i in range(n)]


def request_data(body):
    return json.loads(body['input'][1]['content'].split('<INPUT_JSON_DATA>\n',1)[1].split('\n</INPUT_JSON_DATA>')[0])


class FullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.input = self.root/'input.json'; self.output = self.root/'run'
        self.raw = {'input_id':'mock-full','scenarios':scenes(),
            'report_categories':{r['id']:c for r,c in zip(scenes(),full.CATEGORIES)}}
        full.ev.save(self.input,self.raw)
        self.calls = []
        self.patches = [patch.object(full,'public_provider',return_value=PUBLIC),
            patch.object(full,'get_provider',side_effect=AssertionError('credential read forbidden')),
            patch.object(full.time,'sleep')]
        for p in self.patches: p.start()
        full.prepare(self.input,self.output,PUBLIC)

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def transport(self,body):
        self.calls.append(body)
        self.assertEqual(body['max_output_tokens'],8192)
        self.assertFalse(body['store']); self.assertTrue(body['stream']); self.assertNotIn('reasoning',body)
        data = request_data(body)
        self.assertEqual(len(data['context']['baseline']),113)
        self.assertEqual(len(data['context']['generated_scenarios']),6)
        self.assertNotIn('NOT_FOR_JUDGE',full.ev.canonical(body))
        return capture(wire_for(data),model=full.MODEL)

    def run_mock(self,transport=None,**kwargs):
        return full.execute(self.output,execute_http=True,transport=transport or self.transport,**kwargs)

    def test_effective113_exact_rows_and_prompt_inheritance(self):
        rows = full.effective_gold()
        self.assertEqual(rows,[r for r in full.ev.load(full.ev.GOLD)['scenarios'] if r['id'] not in full.EXCLUDED])
        self.assertEqual(full.ev.sha(full.EFFECTIVE_GOLD),'e55397bd155b7b45b2638f7661e0082d44a4a0ba6911224b2aa98204271d4fd0')
        self.assertTrue(full.PROMPT.read_text().endswith(full.ev.PROMPT.read_text()))

    def test_dryrun_no_credential_no_HTTP_full_context_projection(self):
        report = full.execute(self.output)
        self.assertEqual(report['status'],'dry_run'); self.assertEqual(report['http_count_this_sample_all_sessions'],0)
        self.assertEqual(report['baseline_technical_unassessed'],113)
        self.assertEqual(report['overall']['coverage'],{})
        context = full.ev.load(self.output/'context.json')
        self.assertEqual(context['task_scope']['category_ids'],full.CATEGORIES)
        self.assertEqual(context['task_scope']['reference_scope'],'selected_subset')
        self.assertNotIn('NOT_FOR_JUDGE',full.ev.canonical(context))
        self.assertIn('NOT_FOR_JUDGE',(self.output/'source-snapshot.json').read_text())
        for original,projected in zip(self.raw['scenarios'],context['generated_scenarios']):
            self.assertEqual(projected['title'],original['title'])
            self.assertEqual(projected['text'],original['title']+'\n'+original['description'])
            self.assertEqual(projected['source_point_id'],original['source_point_id'])
        for path in self.output.glob('dry-run-requests/*/request.json'):
            self.assertEqual(len(request_data(full.ev.load(path))['context']['generated_scenarios']),6)

    def test_complete_resume_raw_replay_report_every_row(self):
        report = self.run_mock()
        self.assertEqual(report['status'],'complete'); self.assertEqual(len(self.calls),25)
        self.assertEqual((report['accepted_A_rows'],report['accepted_B_rows']),(113,6))
        self.assertEqual(len(report['baseline_rows']),113); self.assertEqual(len(report['candidate_rows']),6)
        self.assertEqual(sum(c['baseline_denominator'] for c in report['categories'].values()),113)
        self.calls.clear(); self.assertEqual(self.run_mock(resume=True)['status'],'complete'); self.assertFalse(self.calls)
        accepted,assembled = full.Runner(self.output).audit()
        self.assertEqual(len(accepted),25); self.assertEqual(len(assembled['baseline_judgments']),113)
        path = self.output/'requests/A-001/attempt-001/content.original.txt'
        path.write_text(path.read_text()+'tamper')
        with self.assertRaises(full.SafeFailure): full.Runner(self.output).audit()

    def test_smoke_one_block_and_resume(self):
        self.assertEqual(self.run_mock(smoke=True)['status'],'smoke_success')
        self.assertEqual(len(self.calls),1)
        self.calls.clear(); self.assertEqual(self.run_mock(resume=True)['status'],'complete')
        self.assertEqual(len(self.calls),24)

    def test_mixed_format_transient_five_HTTP_budget_across_resume(self):
        replies = [503,'bad',503,'bad','bad']
        def mixed(body):
            self.calls.append(body); item = replies.pop(0)
            if item==503: return {'http_status':503,'body':{'error':{'code':'busy'}},'error':None}
            return capture({'bad':'shape'},model=full.MODEL)
        report = self.run_mock(mixed)
        self.assertEqual(report['status'],'evaluation_failed'); self.assertEqual(len(self.calls),5)
        self.assertEqual(report['accepted_A_rows'],0); self.assertEqual(report['overall']['coverage'],{})
        self.calls.clear(); self.run_mock(mixed,resume=True); self.assertFalse(self.calls)
        self.assertFalse(list(self.output.glob('requests/B-*')))

    def test_two_format_repairs_and_two_transient_retries(self):
        for failure in ('format','transient'):
            if failure=='transient':
                self.output = self.root/'transient'; full.prepare(self.input,self.output,PUBLIC)
            def fail(body):
                self.calls.append(body)
                return capture({'bad':'shape'},model=full.MODEL) if failure=='format' else {'http_status':503,'body':None,'error':None}
            self.calls.clear(); self.run_mock(fail)
            self.assertEqual(len(self.calls),3)
            self.calls.clear(); self.run_mock(fail,resume=True); self.assertFalse(self.calls)

    def test_terminal_failures_no_retry_or_B(self):
        for kind in ('incomplete','401','quota','two-channel','wrong-model','oversize'):
            self.output = self.root/kind; full.prepare(self.input,self.output,PUBLIC)
            def fail(body):
                self.calls.append(body); data=request_data(body)
                if kind=='401': return {'http_status':401,'body':None,'error':None}
                if kind=='quota': return {'http_status':429,'body':{'error':{'code':'insufficient_quota'}},'error':None}
                if kind=='two-channel': return reasoning_capture(wire_for(data),model=full.MODEL)
                result = capture(wire_for(data),model='wrong-model' if kind=='wrong-model' else full.MODEL,incomplete=kind=='incomplete')
                if kind=='oversize': result['captured_bytes']=full.LIMIT+1
                return result
            self.calls.clear(); report=self.run_mock(fail)
            self.assertEqual(report['status'],'evaluation_failed',kind); self.assertEqual(len(self.calls),1,kind)
            self.calls.clear(); self.run_mock(fail,resume=True); self.assertFalse(self.calls,kind)
            self.assertEqual(report['overall']['coverage'],{})

    def test_echo_normalization_recorded_and_wrong_echo_refused(self):
        def missing(body):
            self.calls.append(body); wire=wire_for(request_data(body)); wire.pop('input_id'); wire['request_id']=None
            return capture(wire,model=full.MODEL)
        self.assertEqual(self.run_mock(missing,smoke=True)['status'],'smoke_success')
        state=full.ev.load(self.output/'requests/A-001/attempt-001/status.json')
        self.assertEqual(state['container_fields_filled'],['input_id','request_id'])
        self.output=self.root/'wrong'; full.prepare(self.input,self.output,PUBLIC)
        def wrong(body):
            wire=wire_for(request_data(body)); wire['request_id']='WRONG'; return capture(wire,model=full.MODEL)
        self.assertEqual(self.run_mock(wrong)['status'],'evaluation_failed')

    def test_sealed_sources_context_targets_and_schema(self):
        self.raw['scenarios'][0]['title']='changed'; self.input.write_text(json.dumps(self.raw))
        with self.assertRaises(full.SafeFailure): full.validate_prepared(self.output)

    def test_raw_SSE_tamper_rejected(self):
        self.run_mock(smoke=True)
        path=self.output/'requests/A-001/attempt-001/response.sse'; path.write_text('tamper')
        with self.assertRaises(full.SafeFailure): full.Runner(self.output).audit()

    def test_target_size_bounds_and_sealed_three(self):
        for n in (0,6):
            with self.assertRaises(full.SafeFailure): full.prepare(self.input,self.root/f'bad{n}',PUBLIC,target_size=n)
        out=self.root/'three'; spec=full.prepare(self.input,out,PUBLIC,target_size=3)
        self.assertEqual(len(spec['targets']),40); full.validate_prepared(out)

    def test_request_limit_before_output_or_credentials(self):
        oversized={'scenarios':scenes(1)}; oversized['scenarios'][0]['text']='大'*400000
        path=self.root/'huge.json'; full.ev.save(path,oversized)
        with self.assertRaisesRegex(full.SafeFailure,'request_size_limit'):
            full.prepare(path,self.root/'huge-run',PUBLIC)
        self.assertFalse((self.root/'huge-run').exists())

    def test_control120_same113_references_text_and_hash(self):
        output=self.root/'control'; spec=full.prepare(self.input,output,PUBLIC,control_path=CONTROL)
        context=full.ev.load(output/'context.json'); original=full.ev.load(CONTROL)
        self.assertEqual(full.ev.sha(CONTROL),full.CONTROL_SHA); self.assertEqual(len(context['generated_scenarios']),120)
        self.assertEqual(context['baseline'],full.effective_gold())
        self.assertEqual(context['reference_material'],full.ev.load(self.output/'context.json')['reference_material'])
        for a,b in zip(original['scenarios'],context['generated_scenarios']):
            for key in full.PROJECTION:
                if key in a: self.assertEqual(a[key],b[key])
            self.assertEqual(b['text'],a['title']+'\n'+a['description'])
            self.assertNotIn('source_metadata',b)
        self.assertEqual(spec['parameters'],full.ev.load(self.output/'plan.json')['parameters'])


    def test_cross_module_edges_and_cross_task_duplicate_owner_preserve_all_rows(self):
        def cross(body):
            self.calls.append(body); data=request_data(body); wire=wire_for(data)
            context=data['context']; bid=context['baseline'][0]['id']
            members=sorted(r['id'] for r in context['generated_scenarios'])
            gid=members[-1]
            if data['stage']=='A' and bid in data['targets']:
                row=wire['baseline_judgments'][bid]
                row.update(coverage='full',matched_generated_ids=[gid],covered_aspects=['mock cross-module expression'],missing_aspects=[],
                    evidence=[{'generated_id':gid,'quote':'真实测试意图'}])
            if data['stage']=='B' and members[0] in data['targets']:
                wire['duplicate_groups']=[{'group_id':data['request_id']+':D1','generated_ids':[members[0],gid],
                    'evidence':[{'generated_id':g,'quote':'真实测试意图'} for g in (members[0],gid)],'reason':'mock same intention across task ids'}]
            return capture(wire,model=full.MODEL)
        report=self.run_mock(cross)
        self.assertEqual(report['status'],'complete')
        self.assertEqual(report['baseline_rows'][0]['judgment']['matched_generated_ids'],['TP-25-205'])
        self.assertEqual(report['candidate_rows'][-1]['judgment']['matched_baseline_ids'],[report['baseline_rows'][0]['id']])
        self.assertEqual(len(report['candidate_rows']),6); self.assertEqual(len(report['duplicate_groups']),1)
        self.assertEqual(report['categories']['AUC']['coverage']['full'],1)
        self.assertEqual(report['categories']['CAN']['relations']['matched'],1)

    def test_terminal_resume_does_not_resolve_credentials(self):
        def auth(body):
            return {'http_status':401,'body':None,'error':None}
        self.run_mock(auth)
        # No injected transport: lazy construction must not touch credentials for a terminal archive.
        self.assertEqual(full.execute(self.output,execute_http=True,resume=True)['status'],'evaluation_failed')


class CampaignTests(unittest.TestCase):
    def checkpoint(self,task=20,doc=3,text='标题\n真实规则正文'):
        return {'id':task+100,'task_id':task,'attempt':1,'version':1,'status':'confirmed','selected_citation_ids':[77],
            'candidate_citations':[{'id':77,'citation_type':'source','source_chunk_id':300}],
            'evidence':{'status':'complete','manifests':[{'document_id':doc,'space_id':3,'source_key':'doc3',
                'status':'complete','checklist':True,'line_inventory_version':1,'candidate_citation_ids':[77],
                'full_text':text,'full_text_hash':full.ev.digest(text),'line_inventory':[
                    {'id':'h','text':text[:2],'role':'heading','start_char':0,'end_char':3,'context_line_ids':[],
                     'selected':True,'candidate_citation_ids':[77],'selected_citation_ids':[77]},
                    {'id':'p','text':text[3:],'role':'prose','start_char':3,'end_char':len(text),'context_line_ids':['h'],
                     'selected':True,'candidate_citation_ids':[77],'selected_citation_ids':[77]}],'items':[]}]}}

    def build(self,root):
        chapters=[]
        for i,category in enumerate(full.CATEGORIES):
            chapter=root/category; task=20+i
            values={'proposal':{'id':task+200,'task_id':task,'version':1,'status':'pending','retrieval_checkpoint_id':task+100,
                'points':[{'id':200+i,'task_id':task,'checkpoint_id':task+200,'citation_ids':[77],'title':'真实测试意图','verification_goal':'甲；乙',
                'internal_coverage':{'manual_label':'DO_NOT_SEND'}}]},
                'retrieval_checkpoint':self.checkpoint(task), 'retrieval_selection':{'checkpoint_id':task+100,'expected_version':1,'selected_citation_ids':[77]},
                'citations':[{'id':77,'citation_type':'source','source_chunk_id':300}]}
            artifacts={}
            for key,value in values.items():
                path=chapter/(key+'.json'); full.ev.save(path,value)
                artifacts[key]={'path':path.name,'sha256':full.ev.sha(path)}
            frozen={'key':category,'task_id':task,'project_id':4,'space_id':3,
                'status':'awaiting_test_point_confirmation','checkpoint_id':task+200,'version':1,
                'retrieval_checkpoint_id':task+100,'required_expected':full.REQUIRED[category],'required_actual':full.REQUIRED[category],
                'artifacts':artifacts}
            full.ev.save(chapter/'frozen.json',frozen)
            chapters.append({'key':category,'frozen_path':category+'/frozen.json'})
        path=root/'campaign-summary.json'; full.ev.save(path,{'all_frozen':True,'chapters':chapters})
        return path

    def test_manifest_six_new_tasks_text_projection_dedup_all_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=self.build(root)
            data,snapshot,categories,sources=full.campaign_context(path,'campaign')
            self.assertEqual(len(data['generated_scenarios']),6)
            self.assertEqual(len(data['reference_material']),2)
            self.assertTrue(all(len(r['dependencies'])==6 for r in snapshot['reference_provenance']))
            self.assertNotIn('DO_NOT_SEND',full.ev.canonical(data))
            self.assertIn('DO_NOT_SEND',full.ev.canonical(snapshot))
            self.assertEqual(set(categories.values()),set(full.CATEGORIES))
            out=root/'prepared'; full.prepare(path,out,PUBLIC,campaign=True)
            full.validate_prepared(out)
            self.assertEqual(len(sources),31)

    def test_selected_source_exact_slice_hash_and_dependencies(self):
        cp=self.checkpoint(); refs=full.selected_references(cp,20)
        self.assertEqual([r['text'] for r in refs],['标题\n','真实规则正文'])
        for changed in ('hash','slice','dependency'):
            altered=copy.deepcopy(cp); manifest=altered['evidence']['manifests'][0]
            if changed=='hash': manifest['full_text_hash']='wrong'
            elif changed=='slice': manifest['line_inventory'][1]['text']='altered'
            else: manifest['line_inventory'][1]['context_line_ids']=['not-found']
            with self.assertRaises(full.SafeFailure): full.selected_references(altered,20)

    def test_old_task_or_partial_manifest_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=self.build(root)
            summary=full.ev.load(path); summary['all_frozen']=False; path.write_text(json.dumps(summary))
            with self.assertRaises(full.SafeFailure): full.campaign_context(path,'bad')
            summary['all_frozen']=True; path.write_text(json.dumps(summary))
            frozen=root/'AUC/frozen.json'; row=full.ev.load(frozen); row['task_id']=19; frozen.write_text(json.dumps(row))
            with self.assertRaises(full.SafeFailure): full.campaign_context(path,'bad')


    def test_unconfirmed_dependency_and_partial_candidate_set_refused(self):
        for mutation in ('unselected','missing_candidate','partial','wrong_selected','cycle'):
            cp=self.checkpoint(); heading=cp['evidence']['manifests'][0]['line_inventory'][0]
            if mutation=='unselected': heading['selected']=False
            elif mutation=='missing_candidate': heading['candidate_citation_ids']=[]
            elif mutation=='partial': heading['candidate_citation_ids']=[77,78]
            elif mutation=='wrong_selected': heading['selected_citation_ids']=[]
            else: heading['context_line_ids']=['p']
            with self.subTest(mutation=mutation), self.assertRaises(full.SafeFailure):
                full.selected_references(cp,20)

    def test_all_frozen_identity_mismatches_refused_with_valid_artifact_hash(self):
        cases=[('proposal','id'),('proposal','task_id'),('proposal','version'),('proposal','retrieval_checkpoint_id'),
            ('proposal','status'),('retrieval_checkpoint','id'),('retrieval_checkpoint','task_id'),
            ('retrieval_checkpoint','version'),('retrieval_checkpoint','status'),('retrieval_selection','checkpoint_id'),
            ('retrieval_selection','expected_version'),('retrieval_selection','selected_citation_ids'),
            ('point','task_id'),('point','checkpoint_id'),('point','citation_ids')]
        for artifact,field in cases:
            with self.subTest(artifact=artifact,field=field), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); path=self.build(root); frozen_path=root/'AUC/frozen.json'; frozen=full.ev.load(frozen_path)
                key='proposal' if artifact=='point' else artifact
                item_path=root/'AUC'/frozen['artifacts'][key]['path']; value=full.load_artifact(item_path)
                row=value['points'][0] if artifact=='point' else value
                row[field]=[999] if isinstance(row[field],list) else 'wrong' if isinstance(row[field],str) else row[field]+1
                item_path.write_text(json.dumps(value)); frozen['artifacts'][key]['sha256']=full.ev.sha(item_path)
                frozen_path.write_text(json.dumps(frozen))
                with self.assertRaises(full.SafeFailure): full.campaign_context(path,'bad')

    def normal_context(self,cp):
        cp['selected_citation_ids'] += [78,79]
        cp['candidate_citations'] += [{'id':78,'citation_type':'source','source_chunk_id':400},
                                     {'id':79,'citation_type':'wiki','wiki_page_id':42}]
        return {'source_hits':[{'id':400,'source_chunk_id':400,'task_citation_id':78,'document_id':4,'space_id':3,
                'start_char':10,'end_char':16,'content':'普通规则正文','text':'普通规则正文'}],
            'wiki_hits':[{'page_id':42,'source_document_id':4,'space_id':3,'revision':3,'status':'ready',
                'task_citation_id':79,'body':'完整Wiki规则正文'}]}

    def test_normal_selected_source_and_wiki_full_text_only(self):
        cp=self.checkpoint(); context=self.normal_context(cp)
        with self.assertRaisesRegex(full.SafeFailure,'requires_frozen_database'):
            full.selected_references(cp,20)
        refs=full.selected_references(cp,20,context)
        self.assertEqual([r['text'] for r in refs][-2:],['普通规则正文','完整Wiki规则正文'])
        self.assertEqual(refs[-2]['document_id'],4); self.assertEqual(refs[-1]['document_id'],'wiki_hits:42')
        context['source_hits'].append({'task_citation_id':999,'content':'UNSELECTED_BACKGROUND'})
        self.assertNotIn('UNSELECTED_BACKGROUND',full.ev.canonical(full.selected_references(cp,20,context)))
        for changed in ('space','page','chunk','range','excerpt'):
            bad=copy.deepcopy(context)
            if changed=='space': bad['source_hits'][0]['space_id']=999
            elif changed=='page': bad['wiki_hits'][0]['page_id']=999
            elif changed=='chunk': bad['source_hits'][0]['source_chunk_id']=999
            elif changed=='range': bad['source_hits'][0]['end_char']=999
            else:
                for k in ('content','text'): bad['source_hits'][0].pop(k)
                bad['source_hits'][0]['content_excerpt']='truncated only'
            with self.subTest(changed=changed), self.assertRaises(full.SafeFailure):
                full.selected_references(cp,20,bad)

    def test_SQLite_only_frozen_rows_no_auth_tables_and_resume_uses_snapshot(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); database=root/'frozen.db'; cp=self.checkpoint(); context=self.normal_context(cp)
            with sqlite3.connect(database) as conn:
                conn.execute('CREATE TABLE task_retrieval_checkpoints (id INTEGER,task_id INTEGER,attempt INTEGER,version INTEGER,status TEXT,retrieval_json TEXT,selected_citation_ids_json TEXT)')
                conn.execute('CREATE TABLE models (api_key TEXT)')
                conn.execute('INSERT INTO models VALUES (?)',('DO_NOT_READ_SECRET',))
                conn.execute('INSERT INTO task_retrieval_checkpoints VALUES (?,?,?,?,?,?,?)',
                    (cp['id'],cp['task_id'],cp['attempt'],cp['version'],cp['status'],json.dumps({'evidence':cp['evidence'],'context':context}),json.dumps(cp['selected_citation_ids'])))
            before=database.read_bytes(); rows=full.read_frozen_retrievals(database,[cp]); after=database.read_bytes()
            self.assertEqual(before,after); self.assertEqual(set(rows),{cp['id']})
            self.assertNotIn('DO_NOT_READ_SECRET',full.ev.canonical(rows))
            self.assertEqual(len(full.selected_references(cp,20,rows[cp['id']]['retrieval']['context'])),4)
            bad=copy.deepcopy(cp); bad['version']+=1
            with self.assertRaises(full.SafeFailure): full.read_frozen_retrievals(database,[bad])


    def test_table_header_dependencies_from_items_are_confirmed_and_retained(self):
        cp=self.checkpoint(); manifest=cp['evidence']['manifests'][0]
        text='# 章节\n|类型|数量|\n|限价|全部|\n'
        manifest.update(full_text=text,full_text_hash=full.ev.digest(text))
        rows=[]; offset=0
        for index,raw in enumerate(text.splitlines(keepends=True)):
            rows.append({'id':str(index),'text':raw.rstrip('\r\n'),'start_char':offset,'end_char':offset+len(raw),
                'role':['heading','table_header','entry'][index], 'entry_id':'E1' if index==2 else None,
                'context_line_ids':[] if index==0 else ['0'], 'candidate_citation_ids':[77],
                'selected_citation_ids':[77],'selected':True})
            offset+=len(raw)
        manifest['line_inventory']=rows
        # Real backend gives the table header dependency only to the structural item.
        manifest['items']=[{**rows[2],'id':'E1','line_id':'2','context_line_ids':['0','1']}]
        refs=full.selected_references(cp,20)
        self.assertEqual([r['text'] for r in refs],text.splitlines(keepends=True))
        rows[1]['selected']=False
        with self.assertRaisesRegex(full.SafeFailure,'dependency_not_confirmed'):
            full.selected_references(cp,20)
        rows[1]['selected']=True; manifest['items'][0]['candidate_citation_ids']=[77,88]
        with self.assertRaisesRegex(full.SafeFailure,'entry_dependency_not_confirmed'):
            full.selected_references(cp,20)


if __name__=='__main__': unittest.main()
