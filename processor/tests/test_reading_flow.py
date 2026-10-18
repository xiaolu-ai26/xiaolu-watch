import copy,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from text_output.editorial import validate_reading,check_explicit_relations
from text_output.store import State
from context_analysis.reading import incremental_overview,cards


def group(text,owner='unknown'):
    units=[{'i':0,'text':text,'owner':owner}]
    value={'indices':[0],'clean':text,'distilled':'## 想法\n\n'+text,
        'reading':{'role':'main','retained_indices':[0],'excluded':[],'summary':text}}
    return value,units

class ReadingPlanTests(unittest.TestCase):
    def test_valuable_short_unknown_and_other_voice_remain_visible(self):
        for text in ('先验证需求','不要默认删','也许可以试试'):
            for owner in ('self','other','unknown'):
                value,units=group(text,owner);self.assertEqual(validate_reading(value,units)['role'],'main')
                bad=copy.deepcopy(value);bad.update(distilled='');bad['reading']={'role':'background','retained_indices':[],'excluded':[{'index':0,'reason':'filler'}],'summary':''}
                with self.assertRaises(ValueError):validate_reading(bad,units)
    def test_complete_other_person_explanation_is_not_filtered_as_media(self):
        value,units=group('先保留原文，再按来源整理，可能更容易核对。','other')
        self.assertEqual(validate_reading(value,units)['retained_indices'],[0])
    def test_promotional_exclusion_cannot_reappear_in_summary(self):
        value,units=group('请点赞订阅，支持栏目。');value['distilled']=''
        value['reading']={'role':'background','retained_indices':[],'excluded':[{'index':0,'reason':'promotion'}],'summary':''}
        validate_reading(value,units)
        value['reading']['summary']='呼吁大家点赞订阅。'
        with self.assertRaises(ValueError):validate_reading(value,units)
    def test_bare_watching_thanks_is_greeting_but_mixed_information_is_retained(self):
        value,units=group('Thank you for watching.');value['distilled']=''
        value['reading']={'role':'background','retained_indices':[],'excluded':[{'index':0,'reason':'greeting'}],'summary':''}
        validate_reading(value,units)
        units[0]['text']='Thank you for watching. Do not delete the source.'
        with self.assertRaises(ValueError):validate_reading(value,units)
    def test_like_with_meaning_cannot_be_removed_as_filler(self):
        value,units=group('I like walking.');value['distilled']=''
        value['reading']={'role':'background','retained_indices':[],'excluded':[{'index':0,'reason':'filler'}],'summary':''}
        with self.assertRaises(ValueError):validate_reading(value,units)
        value,units=group('um');value['distilled']='';value['reading']={'role':'background','retained_indices':[],'excluded':[{'index':0,'reason':'filler'}],'summary':''};validate_reading(value,units)
    def test_relation_and_unverified_first_person_veto(self):
        with self.assertRaises(ValueError):check_explicit_relations('中间铺矿砂加膨润土。','中间铺矿砂或膨润土。')
        check_explicit_relations('中间铺矿砂加膨润土。','矿砂加膨润土。')
        value,units=group('我可能以后试试。');value['reading']['summary']='你已经决定试试。'
        with self.assertRaises(ValueError):validate_reading(value,units)
    def test_repetition_needs_retained_source_evidence(self):
        value,units=group('先验证需求');value['reading']['excluded']=[{'index':0,'reason':'repetition'}];value['reading']['retained_indices']=[]
        with self.assertRaises(ValueError):validate_reading(value,units)

class PartialOverviewTests(unittest.TestCase):
    def test_pending_does_not_block_ready_and_revision_invalidates_previous(self):
        class Summaries:
            requests=0;calls=0
            def get(self,items,scope,now,allow_request):
                self.calls+=1
                return {'state':'ready','text':'/'.join(item['id'] for item in items),'generated_at':now}
        with tempfile.TemporaryDirectory() as td:
            state=State(Path(td)/'db');summaries=Summaries()
            a={'id':'a','version':1,'reading_role':'main','summary_state':'ready','summary':'先验证需求','start_at':'2026-01-01T01:00:00Z','end_at':'2026-01-01T01:01:00Z'}
            b=dict(a,id='b',summary_state='pending',summary='')
            result=incremental_overview(state,summaries,[a,b],'2026-01-01',1000,{},0)
            self.assertEqual(result['state'],'ready');self.assertTrue(result['is_partial']);self.assertEqual(result['pending_card_count'],1)
            b.update(summary_state='ready',summary='保留其他人的完整解释')
            incremental_overview(state,summaries,[a,b],'2026-01-01',1001,{},0);self.assertEqual(summaries.calls,1)
            result=incremental_overview(state,summaries,[a,b],'2026-01-01',1601,{},0);self.assertFalse(result['is_partial'])
            b.update(version=2,summary_state='pending',summary='')
            result=incremental_overview(state,summaries,[a,b],'2026-01-01',1602,{},0)
            self.assertEqual(result['text'],'a');self.assertTrue(result['is_partial']);state.close()

class ManagedRevisionTests(unittest.TestCase):
    def test_rewrite_preserves_heading_ids_and_rebinds_owned_groups(self):
        from test_dual_text import FakeLark
        from text_output.lark_output import Lark,document_blocks
        class Editable(FakeLark):
            def command(self,args):
                if args[0]=='+update' and args[args.index('--command')+1]=='block_replace':
                    a=self.ids.index(args[args.index('--start-block-id')+1]);b=self.ids.index(args[args.index('--end-block-id')+1])+1
                    content=(self.home/args[args.index('--content')+1][3:]).read_text();values=document_blocks(content)
                    self.items[a:b]=[(v['tag'],v['text']) for v in values];self.ids[a:b]=['id'+str(i) for i in range(self.next_id,self.next_id+len(values))];self.next_id+=len(values);self.writes+=1
                    return {'ok':True,'data':{'result':'success','warnings':[]}}
                return super().command(args)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);state=State(root/'db');fake=Editable(root);publisher=Lark(state,root,fake.command)
            old={'id':'batch','published':True,'output':{'clean':'## 想法\n\n归属未确认\n\n嗯，先验证需求。','distilled':'## 想法\n\n归属未确认\n\n嗯，先验证需求。'}}
            old['conversation_groups']=[{'id':'stable-card','clean_rendered':old['output']['clean'],'distilled_rendered':old['output']['distilled']}]
            publisher.append('2026-01-01',dict(old['output'],id='batch'));headings={identity for identity,(tag,_) in zip(fake.ids,fake.items) if tag in ('title','h1')}
            rendered='## 想法\n\n先验证需求。';new={'id':'batch','published':True,'output':{'clean':rendered,'distilled':rendered},'conversation_groups':[{'id':'stable-card','clean_rendered':rendered,'distilled_rendered':rendered}]}
            publisher.revise_managed_day('2026-01-01',[old],[new],root/'backup')
            self.assertTrue(headings.issubset(fake.ids));self.assertNotIn('归属未确认',''.join(text for _,text in fake.items));self.assertIn('stable-card',state.get('doc:2026-01-01')['group_blocks']);state.close()

class ContextTimeAndIdentityTests(unittest.TestCase):
    def test_latest_measurement_not_projection_clock(self):
        from context_analysis.health import analyze
        sample={'id':'fixture','source':'fixture','revision':1,'kind':'heart_rate','value':70,'unit':'count/min','start_at':'2026-01-01T10:00:00Z','end_at':'2026-01-01T10:01:00Z'}
        import datetime as dt
        now=dt.datetime(2026,1,1,12,tzinfo=dt.timezone.utc).timestamp()
        result=analyze([sample],'2026-01-01','UTC',now)
        self.assertEqual(result['measured_through_at'],'2026-01-01T10:01:00Z');self.assertIsNone(analyze([],'2026-01-01','UTC',now)['measured_through_at'])
    def test_shared_source_and_display_roles_do_not_merge_card_identity(self):
        groups=[]
        for identity,role in [('a','main'),('b','unknown')]:
            groups.append({'id':identity,'title':identity,'clean_fragment':'有价值的短想法','distilled_fragment':'## '+identity+'\n\n有价值的短想法','owner_caption':'归属未确认','reading':{'role':role,'summary':'有价值的短想法'},'source_spans':[{'source_id':'same-source','absolute_start':1,'absolute_end':2,'owner':'other'}]})
        result=cards([{'conversation_groups':groups}]);self.assertEqual([r['id'] for r in result],['a','b']);self.assertEqual(len(cards([{'conversation_groups':groups}],['a'])),1)

class ExclusionRegressionTests(unittest.TestCase):
    def test_privacy_and_negation_idea_is_not_promotional_cta(self):
        value,units=group('关注隐私保护，不要默认删。');value['distilled']=''
        value['reading']={'role':'background','retained_indices':[],'excluded':[{'index':0,'reason':'promotion'}],'summary':''}
        with self.assertRaises(ValueError):validate_reading(value,units)
    def test_mixed_retained_idea_cannot_resurrect_excluded_cta(self):
        units=[{'i':0,'text':'先验证需求。'},{'i':1,'text':'请点赞订阅。'}]
        value={'indices':[0,1],'clean':'先验证需求。请点赞订阅。','distilled':'## 想法\n\n先验证需求。',
            'reading':{'role':'main','retained_indices':[0],'excluded':[{'index':1,'reason':'promotion'}],'summary':'先验证需求。'}}
        validate_reading(value,units)
        bad=copy.deepcopy(value);bad['reading']['summary']='先验证需求，请点赞订阅。'
        with self.assertRaises(ValueError):validate_reading(bad,units)
        bad=copy.deepcopy(value);bad['distilled']+='请点赞订阅。'
        with self.assertRaises(ValueError):validate_reading(bad,units)

class RevisionRecoveryTests(unittest.TestCase):
    def fixture(self, root):
        from test_dual_text import FakeLark
        from text_output.lark_output import Lark,document_blocks
        from text_worker import Worker
        from reprocess_reading import content_version
        class Editable(FakeLark):
            def command(self,args):
                if args[0]=='+update' and args[args.index('--command')+1]=='block_replace':
                    a=self.ids.index(args[args.index('--start-block-id')+1]);b=self.ids.index(args[args.index('--end-block-id')+1])+1
                    values=document_blocks((self.home/args[args.index('--content')+1][3:]).read_text())
                    self.items[a:b]=[(v['tag'],v['text']) for v in values];self.ids[a:b]=['id'+str(i) for i in range(self.next_id,self.next_id+len(values))];self.next_id+=len(values);self.writes+=1
                    if self.fail_after_insert:self.fail_after_insert=False;raise RuntimeError('response lost')
                    return {'ok':True,'data':{'result':'success','warnings':[]}}
                return super().command(args)
        config={'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'feishu':False}
        worker=Worker(config,provider=object());fake=Editable(worker.home);publisher=Lark(worker.state,worker.home,fake.command);worker.publisher=publisher
        before=[];after=[]
        for n,identifier in enumerate(('a','b')):
            old='## '+identifier+'\n\n旧'+identifier;new='## '+identifier+'\n\n新'+identifier
            batch={'id':identifier,'date':'2026-01-01','session':'fixture','sequence':n,'captured':n,'published':True,'sources':[],
                'output':{'clean':old,'distilled':old,'uncertain_terms':[]},'conversation_groups':[{'id':'card-'+identifier,'clean_rendered':old,'distilled_rendered':old}]}
            before.append(batch);worker.state.put('batch:'+identifier,batch);publisher.append('2026-01-01',dict(batch['output'],id=identifier))
            revised=copy.deepcopy(batch);revised['output'].update(clean=new,distilled=new);revised['conversation_groups'][0].update(clean_rendered=new,distilled_rendered=new);after.append(revised)
        directory=root/'candidate';directory.mkdir()
        candidate={'approved_for_apply':True,'generation':0,'date':'2026-01-01','base_versions':{b['id']:content_version(b) for b in before},'revised_batches':after}
        (directory/'candidate.json').write_text(json.dumps(candidate))
        return worker,publisher,fake,before,after,directory

    def test_deletion_between_ranges_uses_new_owned_ids_and_can_rebase(self):
        from contextlib import contextmanager
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);worker,publisher,fake,before,after,directory=self.fixture(root);calls=0
            @contextmanager
            def guard():
                nonlocal calls
                calls+=1
                if calls>=3:raise RuntimeError('generation changed')
                yield
                if calls==2:publisher.remove_retired_blocks('2026-01-01',[before[1]['output']],['card-a'])
            with self.assertRaises(RuntimeError):publisher.revise_managed_day('2026-01-01',before,after,directory,guard)
            text=' '.join(t for _,t in fake.items);self.assertNotIn('旧a',text);self.assertNotIn('新a',text);self.assertIn('旧b',text);self.assertIn('新b',text)
            self.assertNotIn('card-a',worker.state.get('doc:2026-01-01')['group_blocks'])
            publisher.revise_managed_day('2026-01-01',[before[1]],[after[1]],directory)
            text=' '.join(t for _,t in fake.items);self.assertNotIn('旧b',text);self.assertIn('新b',text);worker.state.close()

    def test_cloud_done_before_local_crash_is_recovered_without_rewrite(self):
        from reprocess_reading import apply,content_version
        with tempfile.TemporaryDirectory() as td:
            worker,publisher,fake,before,after,directory=self.fixture(Path(td));original=publisher.revise_managed_day
            def crash(*args,**kwargs):original(*args,**kwargs);raise RuntimeError('crash after cloud')
            publisher.revise_managed_day=crash
            with self.assertRaises(RuntimeError):apply(worker,directory)
            writes=fake.writes;publisher.revise_managed_day=original;apply(worker,directory)
            self.assertEqual(fake.writes,writes)
            for batch in after:self.assertEqual(content_version(worker.state.get('batch:'+batch['id'])),content_version(batch))
            worker.state.close()

    def test_partially_written_local_revision_is_idempotently_completed(self):
        from reprocess_reading import apply,content_version
        with tempfile.TemporaryDirectory() as td:
            worker,publisher,fake,before,after,directory=self.fixture(Path(td))
            publisher.revise_managed_day('2026-01-01',before,after,directory);worker.state.put('batch:a',after[0]);writes=fake.writes
            apply(worker,directory);self.assertEqual(fake.writes,writes)
            for batch in after:self.assertEqual(content_version(worker.state.get('batch:'+batch['id'])),content_version(batch))
            worker.state.close()

    def test_live_published_state_survives_stale_candidate(self):
        from reprocess_reading import apply
        with tempfile.TemporaryDirectory() as td:
            worker,publisher,fake,before,after,directory=self.fixture(Path(td));candidate=json.loads((directory/'candidate.json').read_text())
            for batch in candidate['revised_batches']:batch['published']=False
            (directory/'candidate.json').write_text(json.dumps(candidate));apply(worker,directory)
            self.assertIn('新a',' '.join(t for _,t in fake.items));self.assertIn('新b',' '.join(t for _,t in fake.items))
            self.assertTrue(worker.state.get('batch:a')['published']);worker.state.close()

class RevisionStyleTests(unittest.TestCase):
    fixture=RevisionRecoveryTests.fixture
    def test_style_only_change_between_ranges_prevents_second_mutation(self):
        from contextlib import contextmanager
        with tempfile.TemporaryDirectory() as td:
            worker,publisher,fake,before,after,directory=self.fixture(Path(td));original=publisher.command;changed=False;calls=0;writes=fake.writes
            def command(args):
                value=original(args)
                if changed and args[0]=='+fetch':
                    value['data']['document']['content']=value['data']['document']['content'].replace('>旧a</p>','><b>旧a</b></p>')
                return value
            publisher.command=command
            @contextmanager
            def guard():
                nonlocal changed,calls
                calls+=1;yield
                if calls==2:changed=True
            with self.assertRaisesRegex(RuntimeError,'custom_format'):
                publisher.revise_managed_day('2026-01-01',before,after,directory,guard)
            self.assertEqual(fake.writes,writes+1);self.assertIn('旧a',' '.join(text for _,text in fake.items));worker.state.close()
    def test_unknown_completed_range_with_manual_style_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as td:
            worker,publisher,fake,before,after,directory=self.fixture(Path(td));fake.fail_after_insert=True
            with self.assertRaises(RuntimeError):publisher.revise_managed_day('2026-01-01',before,after,directory)
            original=publisher.command;writes=fake.writes
            def command(args):
                value=original(args)
                if args[0]=='+fetch':value['data']['document']['content']=value['data']['document']['content'].replace('>新a</p>','><b>新a</b></p>')
                return value
            publisher.command=command
            with self.assertRaisesRegex(RuntimeError,'custom_format'):
                publisher.remove_retired_blocks('2026-01-01',[before[1]['output']],['card-a'])
            self.assertEqual(fake.writes,writes);worker.state.close()

class ManualCompletionTests(unittest.TestCase):
    def test_manual_completion_validates_complete_source_and_has_no_provider_calls(self):
        from unittest.mock import patch
        from reprocess_reading import import_manual,content_version
        from text_worker import Worker
        from text_output.editorial import READING_POLICY_VERSION
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);directory=root/'candidate';directory.mkdir()
            worker=Worker({'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'feishu':False},provider=object())
            unit={'i':0,'text':'先验证需求。','owner':'unknown','gap_before':'unknown','source_id':'a'*64,'source_receipt_hash':'b'*64,'asr_segment_index':0,'sentence_index':0,'time_precision':'asr_word_span','start':0,'end':1,'absolute_start':100,'absolute_end':101,'speaker_reason':'fixture'}
            span={k:v for k,v in unit.items() if k not in ('i','text','gap_before')}
            old={'id':'batch','date':'2026-01-01','captured':100,'sequence':0,'session':'fixture','sources':[],
                'output':{'clean':'## 想法\n\n先验证需求。','distilled':'## 想法\n\n先验证需求。','uncertain_terms':[]},
                'conversation_groups':[{'id':'stable','title':'想法','clean_fragment':'先验证需求。','distilled_fragment':'## 想法\n\n先验证需求。','owner_caption':'归属未确认','source_spans':[span]}]}
            candidate={'version':1,'date':'2026-01-01','policy':READING_POLICY_VERSION,'generation':0,'base_versions':{'batch':content_version(old)},'results':{},'attempts':{}}
            (directory/'before-batches.json').write_text(json.dumps([old]));(directory/'candidate.json').write_text(json.dumps(candidate))
            value={'title':'想法','indices':[0],'continue_previous':False,'clean':'先验证需求。','distilled':'## 想法\n\n先验证需求。','reading':{'role':'main','retained_indices':[0],'excluded':[],'summary':'先验证需求。'}}
            review={'policy':READING_POLICY_VERSION,'generation':0,'provenance':'codex_reviewed_manual','groups':{'batch:0':value},'daily_overview':{'text':'先验证需求。','source_ids':['stable']}}
            path=directory/'manual.json';path.write_text(json.dumps(review));entry={'batch_id':'batch','group_index':0,'id':'stable','title':'想法','units':[unit],'basis':'original_asr_units'}
            with patch('reprocess_reading.load_entries',return_value=[entry]):result=import_manual(worker,directory,path)
            self.assertTrue(result['ready_for_review']);self.assertFalse(result['approved_for_apply'])
            group_new=result['revised_batches'][0]['conversation_groups'][0];self.assertEqual(group_new['id'],'stable');self.assertEqual(group_new['source_spans'],[span])
            self.assertEqual(worker.state.db.execute('select count(*) from admissions').fetchone()[0],0)
            self.assertEqual(result['results']['manual:batch:0']['provider']['provider'],'codex_reviewed_manual')
            result['approved_for_apply']=True
            (directory/'candidate.json').write_text(json.dumps(result));before=(directory/'candidate.json').read_bytes()
            review['daily_overview']={'text':'非法来源。','source_ids':['missing']};path.write_text(json.dumps(review))
            with patch('reprocess_reading.load_entries',return_value=[entry]),self.assertRaises(ValueError):import_manual(worker,directory,path)
            self.assertEqual((directory/'candidate.json').read_bytes(),before)
            review['daily_overview']={'text':'先验证需求。','source_ids':['stable']};path.write_text(json.dumps(review))
            with patch('reprocess_reading.load_entries',return_value=[entry]),patch('reprocess_reading.finish_candidate',side_effect=RuntimeError('crash')),self.assertRaises(RuntimeError):import_manual(worker,directory,path)
            self.assertEqual((directory/'candidate.json').read_bytes(),before)
            review['groups']={};path.write_text(json.dumps(review))
            with patch('reprocess_reading.load_entries',return_value=[entry]),self.assertRaises(ValueError):import_manual(worker,directory,path)
            worker.state.close()

    def test_manual_daily_ready_survives_budget_denial_without_fake_admission(self):
        from text_output.editorial import READING_POLICY_VERSION
        class Provider:
            requests=0
            def get(self,*args,**kwargs):raise AssertionError('No provider request needed for same reviewed content')
        with tempfile.TemporaryDirectory() as td:
            state=State(Path(td)/'db');card={'id':'a','version':2,'_content_version':'hash-a','reading_role':'main','summary_state':'ready','summary':'先验证需求','start_at':None,'end_at':None}
            state.put('manual-day-ready:2026-01-01',{'policy':READING_POLICY_VERSION,'output':{'text':'先验证需求。'},'content_versions':{'a':'hash-a'},'input_group_ids':['a'],'reviewed_at':1000,'provider':'codex_reviewed_manual'})
            result=incremental_overview(state,Provider(),[card],'2026-01-01',1001,{},0)
            self.assertEqual(result['state'],'ready');self.assertFalse(result['is_partial']);self.assertEqual(result['text'],'先验证需求。');state.close()
