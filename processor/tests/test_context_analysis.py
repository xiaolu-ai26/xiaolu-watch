import datetime as dt
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_analysis.health import analyze, normalize, locations
from context_analysis.reading import cards

class HealthTests(unittest.TestCase):
    def sample(self, day, kind, value, index=0):
        start=dt.datetime(2026,1,day,10,index,tzinfo=dt.timezone.utc)
        return dict(id=f'{day}:{kind}:{index}',source='public_fixture',revision=1,kind=kind,value=value,
            unit={'steps':'count','heart_rate':'count/min','hrv_sdnn':'ms','workout':'min'}[kind],
            start_at=start.isoformat(),end_at=(start+dt.timedelta(minutes=1)).isoformat())
    def dataset(self, hr=60, hrv=50):
        rows=[]
        for day in range(1,9):
            for n in range(3):
                rows += [self.sample(day,'steps',0,n),self.sample(day,'heart_rate',60 if day<8 else hr,n), self.sample(day,'hrv_sdnn',50 if day<8 else hrv,n)]
        return rows
    def test_stable_deviation_and_activity(self):
        now=dt.datetime(2026,1,9,tzinfo=dt.timezone.utc).timestamp()
        stable=analyze(self.dataset(),'2026-01-08','UTC',now)
        self.assertEqual(stable['state'],'within_observed_range')
        rows=self.dataset(85,20)
        self.assertEqual(analyze(rows,'2026-01-08','UTC',now)['state'],'deviation')
        rows.append(self.sample(8,'workout',30))
        self.assertEqual(analyze(rows,'2026-01-08','UTC',now)['state'],'unknown')
    def test_no_activity_evidence_missing_and_invalid(self):
        now=dt.datetime(2026,1,9,tzinfo=dt.timezone.utc).timestamp()
        rows=[r for r in self.dataset() if r['kind']!='steps']
        self.assertEqual(analyze(rows,'2026-01-08','UTC',now)['state'],'unknown')
        invalid=self.sample(8,'hrv_sdnn',float('nan')); invalid['unit']='s'
        self.assertEqual(normalize([invalid],now)[1],1)
    def test_revised_deleted_not_duplicated(self):
        row=self.sample(8,'heart_rate',60)
        deleted=dict(row,revision=2,deleted=True)
        self.assertEqual(normalize([row,row,deleted],1e12)[0],[])
    def test_timezone_and_visited_not_current(self):
        row={'start_at':'2026-01-08T00:30:00+08:00','end_at':'2026-01-08T01:00:00+08:00','alias':'公司附近'}
        self.assertFalse(locations([row],0,1e12,1e12)[0]['current'])

class ProjectionTests(unittest.TestCase):
    def test_delete_a_keeps_b_and_other_voice(self):
        groups=[]
        for identity,owner in [('a','self'),('b','other')]:
            groups.append(dict(id=identity,title=identity,clean_fragment=identity,distilled_fragment='## '+identity+'\n正文',owner_caption='归属未确认',
                source_spans=[dict(source_id='shared',absolute_start=0,absolute_end=2,owner=owner)]))
        result=cards([dict(conversation_groups=groups)],['a'])
        self.assertEqual([r['id'] for r in result],['b'])
        self.assertEqual(result[0]['owner_label'],'包含未匹配本人的声音')

if __name__=='__main__':unittest.main()

class AlignmentTests(unittest.TestCase):
    def test_word_boundaries_not_parent_interpolation(self):
        from text_output.core import Source
        from text_output.conversation import utterances
        r=Source('a'*64,'session',0,'2026-01-08',100,60,'甲。乙。','b'*64,segments=({'text':'甲。乙。','start':1,'end':20,'words':[{'word':'甲。','start':2,'end':3},{'word':'乙。','start':15,'end':16}]},))
        units=utterances([r])
        self.assertEqual([(u['start'],u['end']) for u in units],[(2,3),(15,16)])
        self.assertEqual({u['time_precision'] for u in units},{'asr_word_span'})
    def test_unmatched_word_text_stays_coarse(self):
        from text_output.core import Source
        from text_output.conversation import utterances
        r=Source('a'*64,'session',0,'2026-01-08',100,60,'甲。乙。','b'*64,segments=({'text':'甲。乙。','start':1,'end':20,'words':[{'word':'别的文本','start':2,'end':3}]},))
        self.assertEqual({u['time_precision'] for u in utterances([r])},{'asr_parent_span'})

class RetirementTests(unittest.TestCase):
    def test_group_filter_keeps_other_body_and_clears_context(self):
        from context_analysis.reading import filter_batch
        batch={'conversation_groups':[{'id':'a'},{'id':'b'}], 'raw_tail':'private deleted A',
               'output':{'clean':'## A\n\nA正文\n\n## B\n\nB正文','distilled':'## A\n\nA整理\n\n## B\n\nB整理','uncertain_terms':[]}}
        result=filter_batch(batch,['a'])
        self.assertEqual(result['output']['clean'],'## B\n\nB正文')
        self.assertEqual(result['raw_tail'],'')
        self.assertIn('A正文',batch['output']['clean'])

class SummaryTests(unittest.TestCase):
    def test_cache_and_text_only_egress(self):
        import tempfile
        from text_output.store import State
        from context_analysis.reading import Summaries
        class Provider:
            calls=0
            def _request(self,rows,policy,user,validator):
                self.calls+=1
                self.user=user
                return validator({'text':'双方讨论了散步安排，尚未决定。','source_ids':['a']}),{'fixture':True}
        with tempfile.TemporaryDirectory() as td:
            state=State(Path(td)/'state');provider=Provider();summaries=Summaries(state,provider)
            item={'id':'a','version':1,'title':'散步','clean':'如果下雨怎么办？','owner_label':'归属未确认', 'latitude':25,'heart_rate':70}
            self.assertEqual(summaries.get([item],'card:a',1)['state'],'ready')
            summaries.get([item],'card:a',2)
            self.assertEqual(provider.calls,1)
            self.assertNotIn('latitude',provider.user);self.assertNotIn('heart_rate',provider.user)
            item['version']=2;summaries.get([item],'card:a',3)
            self.assertEqual(provider.calls,2);state.close()

class RetiredSequenceTests(unittest.TestCase):
    def exercise(self, retired, missing):
        import tempfile, json, hashlib
        from unittest.mock import patch
        from text_worker import Worker
        from context_analysis import retirement
        from text_output.store import atomic
        class Provider:
            calls=0
            seen=[]
            def edit(self, rows, context):
                self.calls+=1;self.seen.extend(r.sequence for r in rows)
                return {'clean':'公开合成语句。','distilled':'## 合成主题\n\n公开合成语句。','uncertain_terms':[]},{}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); provider=Provider()
            config={'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'), 'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions'),'feishu':False}
            ids={n:hashlib.sha256(str(n).encode()).hexdigest() for n in range(3)}
            for n in range(3):
                atomic(root/'sessions/session'/f'{n}.json',{'metadata':{'is_test':False,'sha256':ids[n],'session_id':'session','sequence':n,'capture_date':'2026-01-08','capture_started_at':f'2026-01-08T10:0{n}:00Z','duration_s':60,'original_name':f'{n}.m4a'}})
                if n not in missing:
                    atomic(root/'asr'/ids[n]/'receipt.json',{'source_id':ids[n],'is_test':False,'capture_date':'2026-01-08','status':'done','segments':[{'text':'公开合成语句。','start':1,'end':5}]})
            snapshot={'generation':0,'group_ids':[],'source_ids':[], 'retired_raw_source_ids':[ids[n] for n in retired],'transactions':[]}
            with patch.object(retirement,'snapshot',return_value=snapshot):
                worker=Worker(config,provider);worker.tick(2e9);worker.state.close()
                worker=Worker(config,provider);worker.tick(2e9);worker.state.close()
            return provider.seen
    def test_first_retired_without_asr_does_not_block(self):
        self.assertEqual(self.exercise({0},{0}),[1,2])
    def test_middle_retired_without_asr_keeps_both_sides(self):
        self.assertEqual(self.exercise({1},{1}),[0,2])
    def test_missing_nonretired_still_blocks(self):
        self.assertEqual(self.exercise(set(),{0}),[])
    def test_middle_unready_still_blocks_later(self):
        self.assertEqual(self.exercise(set(),{1}),[0])

class SeparateMetricTests(unittest.TestCase):
    sample = HealthTests.sample
    dataset = HealthTests.dataset
    def test_resting_estimate_cannot_reuse_instantaneous_baseline(self):
        rows=self.dataset(85,20)
        for row in rows:
            if row['id'].startswith('8:heart_rate:'):
                row['kind']='resting_heart_rate'
        now=dt.datetime(2026,1,9,tzinfo=dt.timezone.utc).timestamp()
        result=analyze(rows,'2026-01-08','UTC',now)
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['coverage']['baseline_days']['heart_rate'],0)
    def test_source_change_cannot_reuse_other_device_baseline(self):
        rows=self.dataset(85,20)
        for row in rows:
            if row['id'].startswith('8:hrv_sdnn:'):
                row['source']='different-device'
        now=dt.datetime(2026,1,9,tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(analyze(rows,'2026-01-08','UTC',now)['state'],'unknown')

class OwnedCloudDeletionTests(unittest.TestCase):
    def setup_lark(self, root):
        from test_dual_text import FakeLark
        from text_output.store import State
        from text_output.lark_output import Lark
        state=State(root/'db');fake=FakeLark(root);publisher=Lark(state,root,fake.command)
        one={'clean':'## 相同主题\n\n相同正文。','distilled':'## 相同主题\n\n相同正文。'}
        groups=[dict(id=k,clean_rendered=one['clean'],distilled_rendered=one['distilled']) for k in ('a','b')]
        output=dict(id='batch',clean=one['clean']+'\n\n'+one['clean'],distilled=one['distilled']+'\n\n'+one['distilled'],_groups=groups)
        publisher.append('2026-01-08',output)
        return state,fake,publisher,one
    def test_duplicate_prose_deletes_a_exact_ids_keeps_b(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            state,fake,publisher,remaining=self.setup_lark(Path(td))
            day=state.get('doc:2026-01-08');b_ids={b['id'] for values in day['group_blocks']['b'].values() for b in values}
            a_ids={b['id'] for values in day['group_blocks']['a'].values() for b in values}
            publisher.remove_retired_blocks('2026-01-08',[remaining],['a'])
            self.assertTrue(b_ids.issubset(set(fake.ids)));self.assertFalse(a_ids.intersection(fake.ids));state.close()
    def test_manual_same_text_new_id_is_not_deleted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            state,fake,publisher,remaining=self.setup_lark(Path(td));writes=fake.writes
            day=state.get('doc:2026-01-08');a_id=day['group_blocks']['a']['clean'][0]['id']
            fake.ids[fake.ids.index(a_id)]='manual-replaced-identical-text'
            with self.assertRaises(RuntimeError): publisher.remove_retired_blocks('2026-01-08',[remaining],['a'])
            self.assertEqual(fake.writes,writes);state.close()
    def test_unknown_delete_recovers_only_unchanged_remaining_ids(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            state,fake,publisher,remaining=self.setup_lark(Path(td));fake.fail_after_insert=True
            with self.assertRaises(RuntimeError):publisher.remove_retired_blocks('2026-01-08',[remaining],['a'])
            # Server did commit one deletion. Retry recognizes exact remaining identities.
            publisher.remove_retired_blocks('2026-01-08',[remaining],['a'])
            self.assertEqual(len(fake.items),7);state.close()

class CacheRetirementTests(unittest.TestCase):
    def test_model_omitted_citation_does_not_preserve_deleted_input(self):
        import tempfile
        from unittest.mock import patch
        from text_worker import Worker
        from context_analysis import retirement
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config={'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'feishu':False}
            worker=Worker(config,provider=object())
            worker.state.put('overview:fixture',{'input_group_ids':['a','b'],'output':{'text':'提及A和B','source_ids':['b']}})
            snapshot={'generation':0,'group_ids':['a'],'source_ids':[],'transactions':[]}
            with patch.object(retirement,'snapshot',return_value=snapshot):worker.reconcile_deletions()
            self.assertTrue(worker.state.get('overview:fixture')['retired']);self.assertNotIn('output',worker.state.get('overview:fixture'));worker.state.close()

class GenerationIntegrationTests(unittest.TestCase):
    def test_real_store_guard_blocks_delete_and_rejects_stale_writer(self):
        import tempfile, threading
        from context_analysis import retirement
        with tempfile.TemporaryDirectory() as td:
            config={'context_root':str(Path(td)/'context')};ledger=retirement.store(config);errors=[]
            def deleter():
                try:
                    with ledger.generation_guard(timeout=.05):pass
                except Exception as error:errors.append(getattr(error,'code',type(error).__name__))
            with retirement.guard(config,0):
                thread=threading.Thread(target=deleter);thread.start();thread.join()
                with retirement.guard(config,0):pass  # Worker nested writes are reentrant.
            self.assertEqual(errors,['publication_busy'])
            with ledger.generation_guard():
                with ledger.connect() as connection:connection.execute("UPDATE meta SET v=1 WHERE k='generation'")
            with self.assertRaises(Exception):
                with retirement.guard(config,0):self.fail('stale generation accepted')
    def test_inflight_summary_is_not_committed_after_retirement(self):
        import tempfile
        from context_analysis import retirement
        from context_analysis.reading import Summaries
        from text_output.store import State
        with tempfile.TemporaryDirectory() as td:
            config={'context_root':str(Path(td)/'context')};ledger=retirement.store(config);state=State(Path(td)/'state')
            class Provider:
                from contextlib import contextmanager
                @contextmanager
                def with_egress_guard(self, factory):
                    self.factory=factory
                    yield
                def _request(self,rows,policy,user,validator):
                    with self.factory():pass  # Request sent, transport releases before response.
                    with ledger.generation_guard():
                        with ledger.connect() as connection:connection.execute("UPDATE meta SET v=1 WHERE k='generation'")
                    return {'text':'将被删除的对话摘要','source_ids':['a']},{}
            summaries=Summaries(state,Provider(),config)
            item=dict(id='a',version=1,title='主题',clean='公开合成对话。',owner_label='unknown')
            with self.assertRaises(Exception):summaries.get([item],'card:a',1)
            self.assertFalse(any(value.get('output') for _,value in state.items('overview:')));state.close()
    def test_today_projection_and_reopened_deletion(self):
        import tempfile
        from overview_worker import Overview
        from text_worker import Worker
        from context_analysis import retirement
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config={'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'reading_root':str(root/'reading'),'context_root':str(root/'context'),'feishu':False,'timezone':'Asia/Taipei'}
            class Provider:
                def _request(self,rows,policy,user,validator):
                    import json
                    return validator({'text':'对话中讨论了公开合成测试的安排。','source_ids':[json.loads(user)[0]['id']]}),{}
            worker=Worker(config,Provider());ledger=retirement.store(config)
            group=dict(id='a',title='合成安排',clean_fragment='双方讨论合成安排。',distilled_fragment='## 合成安排\n\n双方讨论合成安排。',clean_rendered='## 合成安排\n\n双方讨论合成安排。',distilled_rendered='## 合成安排\n\n双方讨论合成安排。',owner_caption='包含未匹配本人的声音',source_spans=[dict(source_id='a'*64,start=1,end=2,absolute_start=1767830401,absolute_end=1767830402,owner='other',time_precision='asr_word_span')])
            worker.state.put('batch:a',dict(id='a',date='2026-01-08',captured=1767830400,session='fixture',sequence=0,raw_tail='',sources=[],conversation_groups=[group],output={'clean':group['clean_rendered'],'distilled':group['distilled_rendered']}))
            overview=Overview(config,worker);overview.tick(1767840000)
            import json
            public=json.loads((root/'reading/2026-01-08.json').read_text())
            self.assertEqual(public['cards'][0]['owner_label'],'other');self.assertNotIn('source_spans',public['cards'][0]);self.assertEqual(public['overview']['state'],'ready')
            with ledger.generation_guard():
                with ledger.connect() as connection:
                    connection.execute("UPDATE meta SET v=1 WHERE k='generation'")
                    connection.execute('INSERT INTO card_tombstones VALUES(?,?,?,?,?)',('a',1,1,'fixture','2026-01-08'))
            worker.state.close();worker=Worker(config,Provider());overview=Overview(config,worker);overview.tick(1767840001)
            self.assertEqual(json.loads((root/'reading/2026-01-08.json').read_text())['cards'],[]);worker.state.close()

class PublicationRaceRegressionTests(unittest.TestCase):
    def test_error_bookkeeping_cannot_restore_scrubbed_payload(self):
        import tempfile
        from text_worker import Worker
        from context_analysis import retirement
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config={'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'context_root':str(root/'context'),'feishu':False}
            worker=Worker(config,provider=object());ledger=retirement.store(config)
            old={'id':'a','attempts':1,'output':{'clean':'deleted','distilled':'deleted'},'conversation_groups':[{'id':'a'}],'raw_tail':'deleted'}
            worker.state.put('batch:a',old)
            with ledger.generation_guard():
                with ledger.connect() as db:db.execute("UPDATE meta SET v=1 WHERE k='generation'")
                worker.state.put('batch:a',dict(id='a',attempts=1,output={'clean':'','distilled':''},conversation_groups=[],raw_tail=''))
            worker._record_error('batch:a',RuntimeError('fixture failure'))
            current=worker.state.get('batch:a')
            self.assertEqual(current['conversation_groups'],[]);self.assertEqual(current['raw_tail'],'');self.assertEqual(current['output']['clean'],'');self.assertEqual(current['last_error'],'RuntimeError');worker.state.close()

    def test_send_boundary_rejects_retired_request_and_unlocks_before_response(self):
        import tempfile, contextlib
        from unittest.mock import patch
        from context_analysis import retirement
        from text_output.provider import GuardedHTTPSConnection
        with tempfile.TemporaryDirectory() as td:
            config={'context_root':str(Path(td)/'context')};ledger=retirement.store(config);sent=[]
            @contextlib.contextmanager
            def admission():
                with retirement.egress(config,0,group_ids=['a']):yield
            connection=GuardedHTTPSConnection('example.invalid',timeout=180,guard_factory=admission)
            def send(*args,**kwargs):
                sent.append(True)
                # Prove the transport body transmission itself owns the process lock.
                import threading
                blocked=[]
                def contender():
                    try:
                        with ledger.generation_guard(timeout=.02):pass
                    except Exception:blocked.append(True)
                thread=threading.Thread(target=contender);thread.start();thread.join()
                self.assertEqual(blocked,[True])
            with patch('http.client.HTTPSConnection.request',side_effect=send):connection.request('POST','/',body=b'public-fixture')
            # Now inference/response can wait without holding the deletion guard.
            with ledger.generation_guard(timeout=.02):
                with ledger.connect() as db:db.execute("UPDATE meta SET v=1 WHERE k='generation'")
            with patch('http.client.HTTPSConnection.request',side_effect=send):
                with self.assertRaises(Exception):connection.request('POST','/',body=b'stale')
            self.assertEqual(sent,[True]);self.assertEqual(connection.timeout,180)

    def test_old_cloud_conflict_does_not_block_same_day_local_or_next_day(self):
        import tempfile, hashlib
        from text_worker import Worker
        from text_output.store import atomic
        from context_analysis import retirement
        class Provider:
            calls=0
            def edit(self,rows,context):
                self.calls+=1
                return {'clean':'新的公开合成内容。','distilled':'## 新内容\n\n新的公开合成内容。','uncertain_terms':[]},{}
        class Publisher:
            appended=[]
            def remove_retired_blocks(self,date,outputs,deleted):
                if date=='2026-01-07':raise RuntimeError('manual edit preserved')
            def append(self,date,output):
                self.appended.append(date);return {'id':'fixture-document'}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config={'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions'),'context_root':str(root/'context'),'feishu':False}
            provider=Provider();publisher=Publisher();worker=Worker(config,provider,publisher);ledger=retirement.store(config)
            group=lambda identity:dict(id=identity,clean_rendered='## '+identity+'\n\n'+identity,distilled_rendered='## '+identity+'\n\n'+identity)
            old=dict(id='old',date='2026-01-07',session='session-07',sequence=0,sequences=[0],captured=1767779999,sources=[],attempts=1,published=True,raw_tail='old',conversation_groups=[group('a'),group('b')],output={'clean':'## a\n\na\n\n## b\n\nb','distilled':'## a\n\na\n\n## b\n\nb'})
            worker.state.put('batch:old',old)
            with ledger.generation_guard():
                with ledger.connect() as db:
                    db.execute("UPDATE meta SET v=1 WHERE k='generation'")
                    db.execute('INSERT INTO card_tombstones VALUES(?,?,?,?,?)',('a',1,1,'fixture','2026-01-07'))
            for day,seq in [('07',1),('08',0)]:
                sid=hashlib.sha256(day.encode()).hexdigest();session='session-'+day;date='2026-01-'+day
                atomic(root/'sessions'/session/f'{seq}.json',{'metadata':{'is_test':False,'sha256':sid,'session_id':session,'sequence':seq,'capture_date':date,'capture_started_at':date+'T10:00:00Z','duration_s':60,'original_name':'fixture.m4a'}})
                atomic(root/'asr'/sid/'receipt.json',{'source_id':sid,'is_test':False,'capture_date':date,'status':'done','segments':[{'text':'新的公开合成内容。','start':1,'end':5}]})
            result=worker.tick(2e9)
            self.assertEqual(result['new_batches'],2)
            self.assertEqual(publisher.appended,['2026-01-08'])
            self.assertIn('新的公开合成内容',(root/'notes/口语清稿/2026-01-07_口语清稿.md').read_text())
            self.assertTrue(worker.state.get('cloud-delete-conflict:2026-01-07'));worker.state.close()
