import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from text_output.core import Source, digest
from text_output.conversation import utterances, editor_input, validate_groups, materialize, choose_window
from text_output.speaker_labels import attach
from text_output.store import State, atomic
from text_worker import Worker


def row(n=0, text='我想明天散步。可能会下雨。', captured=1800000000):
    return Source(hashlib.sha256(str(n).encode()).hexdigest(), 'synthetic-session', n, '2027-01-15', captured+n*60,
                  60, text, digest(text), 'synthetic-asr', 'e'*64, ({'text': text, 'start': 2., 'end': 9.},))


def group(title, indices, continued=False, body='我想明天散步，可能会下雨。'):
    return {'title': title, 'indices': indices, 'continue_previous': continued, 'clean': body, 'distilled': '## '+title+'\n\n'+body}


class GroupChecks(unittest.TestCase):
    def test_local_sentence_split_keeps_coarse_parent_time(self):
        r=row();units=utterances([r]);self.assertEqual(len(units),2)
        self.assertEqual([u['text'] for u in units],['我想明天散步。','可能会下雨。'])
        self.assertEqual([u['asr_segment_index'] for u in units],[0,0])
        self.assertEqual([u['start'] for u in units],[2.,2.]);self.assertEqual(units[1]['time_precision'],'asr_parent_span')
        self.assertEqual(units[1]['absolute_start'],r.captured+2)

    def test_strict_full_ordered_coverage_and_continuation(self):
        units=utterances([row()]);valid={'groups':[group('散步安排',[0,1])],'uncertain_terms':[]}
        self.assertEqual(validate_groups(valid,units),valid)
        for indices in [[0],[1,0],[0,0],[0,1,2]]:
            with self.assertRaises(ValueError):validate_groups({'groups':[group('散步安排',indices)],'uncertain_terms':[]},units)
        with self.assertRaises(ValueError):validate_groups({'groups':[group('散步安排',[0,1],True)],'uncertain_terms':[]},units)
        previous={'id':'prior','title':'散步安排'}
        self.assertTrue(validate_groups({'groups':[group('散步安排',[0,1],True)],'uncertain_terms':[]},units,previous))
        with self.assertRaises(ValueError):validate_groups({'groups':[group('另一个标题',[0,1],True)],'uncertain_terms':[]},units,previous)

    def test_provider_egress_has_no_biometrics_scores_ids_or_clocks(self):
        units=utterances([row()]);units[0].update(owner='self',score=.99,embedding=[1,2],profile_version='private-profile')
        sent=json.loads(editor_input(units,{'id':'private-id','title':'散步安排'}))
        self.assertEqual(set(sent['units'][0]),{'i','text','owner','gap_before'})
        serialized=json.dumps(sent);self.assertNotIn('embedding',serialized);self.assertNotIn('private-profile',serialized);self.assertNotIn('private-id',serialized);self.assertNotIn(row().source_id,serialized)

    def test_whole_previous_passage_cannot_be_replayed_as_new(self):
        units=utterances([row(text='现在换一个新的问题。')]);prior_text='这是一段之前已经发布的完整内容，不能因为新窗口就重新打印在用户文档里面。'
        value={'groups':[group('新的问题',[0],False,prior_text+'现在换一个新的问题。')],'uncertain_terms':[]}
        with self.assertRaises(ValueError):validate_groups(value,units,{'title':'旧话题','id':'old','clean_fragment':prior_text})

    def test_local_identity_caption_and_cross_window_append_only(self):
        units=utterances([row()]);units[0]['owner']='self';units[1]['owner']='other'
        first,meta=materialize({'groups':[group('散步安排',[0,1])],'uncertain_terms':[]},units,'batch1')
        self.assertNotIn('未匹配本人',first['clean']);self.assertIn('未匹配本人',meta[0]['owner_caption']);self.assertNotIn(row().source_id,first['clean'])
        newer=utterances([row(1,'如果下雨就带伞。')]);next_value={'groups':[group('散步安排',[0],True,'如果下雨就带伞。')],'uncertain_terms':[]}
        output,next_meta=materialize(next_value,newer,'batch2',meta[-1]);self.assertEqual(meta[-1]['id'],next_meta[-1]['id'])
        self.assertIn('（续）',output['clean']);self.assertNotIn('明天散步',output['clean']);self.assertEqual(next_meta[0]['window_count'],2)
        changed,changed_meta=materialize({'groups':[group('采购计划',[0],False,'准备采购。')],'uncertain_terms':[]},newer,'batch3',next_meta[-1]);self.assertNotEqual(next_meta[-1]['id'],changed_meta[-1]['id'])

    def test_pause_flush_is_not_a_topic_boundary(self):
        r=row();selected=choose_window({0:r},set(),r.captured+61,pause_seconds=12)
        self.assertEqual(selected,[r])  # Actual speech ends at9s; trailing silence flushes early.
        continuous=Source(r.source_id,r.session,0,r.date,r.captured,60,r.text,r.text_hash,r.asr_model,r.receipt_hash,({'text':r.text,'start':0.,'end':59.},))
        self.assertEqual(choose_window({0:continuous},set(),r.captured+61),[])
        self.assertEqual(choose_window({0:continuous},set(),r.captured+241),[continuous])


class SpeakerChecks(unittest.TestCase):
    def setup(self,root):
        r=row();config={'outbox':str(root/'speakers'),'profile_version':'fixture-profile','model_id':'fixture-model','model_revision':'fixture-revision','model_sha256':'f'*64,'profile_sha256':'a'*64,'wait_seconds':60}
        value={'version':1,'status':'done','source_id':r.source_id,'audio_sha256':r.source_id,'asr_receipt_sha256':r.receipt_hash,'time_basis':'source_audio_seconds','profile_version':config['profile_version'],'profile_sha256':config['profile_sha256'],'model_id':config['model_id'],'model_revision':config['model_revision'],'model_sha256':config['model_sha256'],'turns':[{'asr_segment_index':0,'start':2.,'end':9.,'label':'self','score':.9,'confidence_reason':'synthetic-fixture'}]}
        return r,config,value,Path(config['outbox'])/r.source_id/'identity.json'

    def test_exact_binding_and_fail_closed_negative_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);r,c,v,path=self.setup(root);state=State(root/'state.db');atomic(path,v)
            units=utterances([r]);audit=attach(units,[r],c,state,100);self.assertFalse(audit['waiting']);self.assertEqual({u['owner'] for u in units},{'self'});self.assertEqual(audit['evidence'][r.source_id]['sidecar_sha256'],hashlib.sha256(path.read_bytes()).hexdigest());self.assertEqual(audit['evidence'][r.source_id]['profile_version'],c['profile_version']);self.assertEqual([x['label'] for x in audit['evidence'][r.source_id]['unit_labels']],['self','self']);self.assertNotIn('score',json.dumps(audit['evidence']))
            for key,bad in [('source_id','0'*64),('asr_receipt_sha256','0'*64),('profile_version','old'),('profile_sha256','0'*64),('model_id','wrong'),('model_sha256','0'*64),('model_revision','wrong'),('time_basis','absolute_utc')]:
                value=copy.deepcopy(v);value[key]=bad;atomic(path,value);units=utterances([r]);attach(units,[r],c,state,100);self.assertEqual({u['owner'] for u in units},{'unknown'},key)
            atomic(path,v)
            for missing_key in ('model_sha256','profile_sha256'):
                missing=dict(c);missing.pop(missing_key);units=utterances([r]);result=attach(units,[r],missing,state,100);self.assertFalse(result['waiting']);self.assertEqual({u['owner'] for u in units},{'unknown'})
            state.close()

    def test_mixed_unknown_and_overlapping_duplicate_windows_not_majority(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);r,c,v,path=self.setup(root);state=State(root/'db')
            for turns in [
                [dict(v['turns'][0],end=8),dict(v['turns'][0],start=8,label='other')],
                [dict(v['turns'][0],end=8),dict(v['turns'][0],start=8,label='unknown',score=None)],
                [dict(v['turns'][0],end=5.5),dict(v['turns'][0],end=5.5)],
                [dict(v['turns'][0],score=None)],
            ]:
                atomic(path,dict(v,turns=turns));units=utterances([r]);attach(units,[r],c,state,100);self.assertEqual({u['owner'] for u in units},{'unknown'})
            state.close()

    def test_bounded_wait_cold_restart_and_late_arrival_no_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);r,c,v,path=self.setup(root);state=State(root/'db');units=utterances([r]);self.assertTrue(attach(units,[r],c,state,0)['waiting']);state.close();state=State(root/'db')
            units=utterances([r]);self.assertFalse(attach(units,[r],c,state,61)['waiting'])
            rendered,metadata=materialize({'groups':[group('散步安排',[0,1])],'uncertain_terms':[]},units,'prior')
            saved=copy.deepcopy((rendered,metadata));atomic(path,v);later=utterances([r]);attach(later,[r],c,state,62);self.assertEqual({u['owner'] for u in later},{'self'})
            self.assertEqual((rendered,metadata),saved);self.assertNotIn('归属未确认',rendered['clean']);self.assertEqual(metadata[0]['owner_caption'],'归属未确认');state.close()

    def test_malformed_sidecar_roots_fail_unknown_without_blocking_text(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);r,c,v,path=self.setup(root);state=State(root/'db')
            for raw in ['[]','null','"bad"','{not-json']:
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text(raw)
                units=utterances([r]);result=attach(units,[r],c,state,100)
                self.assertFalse(result['waiting']);self.assertEqual({u['owner'] for u in units},{'unknown'})
                output,_=materialize({'groups':[group('散步安排',[0,1])],'uncertain_terms':[]},units,'safe')
                self.assertNotIn('归属未确认',output['clean']);self.assertIn('散步',output['clean'])
            state.close()


class WorkerChecks(unittest.TestCase):
    def test_cross_chunk_topic_persistence_no_duplicate_after_restart(self):
        class Provider:
            calls=0
            def edit_conversations(self,rows,units,previous,context):
                self.calls+=1;title='采购计划' if '采购' in units[0]['text'] else '散步安排';continued=bool(previous and previous['title']==title)
                value={'groups':[group(title,list(range(len(units))),continued,' '.join(u['text'] for u in units))],'uncertain_terms':[]}
                validate_groups(value,units,previous,context);return value,{'model':'synthetic'}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config={'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions'),'feishu':False,'conversation_mode':True};provider=Provider()
            def add(n,text):
                r=row(n,text);atomic(root/'sessions/synthetic-session'/f'{n}.json',{'metadata':{'is_test':False,'sha256':r.source_id,'session_id':r.session,'sequence':n,'capture_date':r.date,'capture_started_at':__import__('datetime').datetime.fromtimestamp(r.captured,__import__('datetime').timezone.utc).isoformat(),'duration_s':60,'original_name':f'{n}.m4a'}});atomic(root/'asr'/r.source_id/'receipt.json',{'source_id':r.source_id,'is_test':False,'capture_date':r.date,'status':'done','segments':[{'text':text,'start':2.,'end':9.}]})
            add(0,'我打算散步。');w=Worker(config,provider);w.tick(2e9);first=w._saved_batches('2027-01-15')[0]['conversation_groups'][0];w.state.close()
            add(1,'下雨就带伞。');w=Worker(config,provider);w.tick(2e9);second=w._saved_batches('2027-01-15')[1]['conversation_groups'][0];self.assertEqual(first['id'],second['id']);w.tick(2e9);self.assertEqual(provider.calls,2)
            add(2,'接下来讨论采购。');w.tick(2e9);third=w._saved_batches('2027-01-15')[2]['conversation_groups'][0];self.assertNotEqual(second['id'],third['id'])
            text=(root/'notes/口语清稿/2027-01-15_口语清稿.md').read_text();self.assertEqual(text.count('我打算散步'),1);self.assertEqual(text.count('下雨就带伞'),1);w.state.close()


if __name__=='__main__':unittest.main()
