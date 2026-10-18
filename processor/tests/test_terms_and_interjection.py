import copy,json,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from text_output.editorial import normalize_editor_response,validate_reading
from text_output.core import validate_output,Source,digest,batch_identity
from text_output.conversation import GROUP_SCHEMA,utterances,validate_groups
from text_worker import Worker
from recover_rejected import replay

class TermsInterjectionTests(unittest.TestCase):
 def test_exact_terms_objects_are_lossless_and_original_unchanged(self):
  raw={'groups':[],'uncertain_terms':[{'term':'示例词','note':'含义需确认'}]};before=copy.deepcopy(raw);v,actions=normalize_editor_response(raw,[])
  self.assertEqual(v['uncertain_terms'],['示例词（含义需确认）']);self.assertEqual(raw,before);self.assertEqual(actions[0]['action'],'lossless_term_note_to_string')
 def test_unknown_mixed_nested_and_oversized_terms_are_not_salvaged(self):
  bads=[[{'term':'a','note':'b','extra':'c'}],['a',{'term':'b','note':'c'}],[{'term':{'word':'a'},'note':'b'}],[{'term':'a','note':' '}],[{'term':'','note':'x'}],[{'term':2,'note':'x'}],[{'term':'x','note':False}],[{'term':'x'*79,'note':'ab'}],[{'term':'a','note':'b'}]*31]
  for terms in bads:
   raw={'groups':[],'uncertain_terms':terms};v,actions=normalize_editor_response(raw,[]);self.assertEqual(actions,[]);self.assertEqual(raw,v)
   with self.assertRaises(ValueError):validate_output({'clean':'保留正文','distilled':'保留正文','uncertain_terms':v['uncertain_terms']},'保留正文')
 def test_unicode_and_eighty_character_boundary_are_preserved(self):
  for term,note in [('🦌e\u0301词','不确定？'),('词'*77,'注')]:
   raw={'groups':[],'uncertain_terms':[{'term':term,'note':note}]};v,actions=normalize_editor_response(raw,[]);self.assertEqual(v['uncertain_terms'],[term+'（'+note+'）']);self.assertEqual(len(actions),1)
  raw={'groups':[],'uncertain_terms':[{'term':'词'*78,'note':'注'}]};v,actions=normalize_editor_response(raw,[]);self.assertEqual(v,raw);self.assertEqual(actions,[])
 def test_terms_repair_does_not_bypass_body_or_exclusion_validation(self):
  unit={'i':0,'text':'哎先别删','owner':'unknown','gap_before':'unknown'}
  group={'title':'保留','indices':[0],'continue_previous':False,'clean':'哎先别删','distilled':'','reading':{'role':'background','retained_indices':[],'excluded':[{'index':0,'reason':'filler'}],'summary':''}}
  raw={'groups':[group],'uncertain_terms':[{'term':'示例','note':'待确认'}]};value,actions=normalize_editor_response(raw,[unit]);self.assertTrue(actions)
  with self.assertRaisesRegex(ValueError,'reading_filler_contains_information'):validate_groups(value,[unit],require_reading=True)
  raw['groups'][0]['clean']='';value,_=normalize_editor_response(raw,[unit])
  with self.assertRaises(ValueError):validate_groups(value,[unit],require_reading=True)
 def test_offline_flag_cannot_bypass_attempt_limit_with_regular_provider(self):
  from test_manual_pending import ManualPendingTests
  with tempfile.TemporaryDirectory() as td:
   w,row,c,u=ManualPendingTests().fixture(Path(td));batch=w.state.get('batch:'+c['batch_id']);batch['attempts']=3;w.state.put('batch:'+c['batch_id'],batch);before=list(w.state.db.iterdump())
   with patch('text_worker.discover',return_value={(row.date,row.session):{0:row}}),self.assertRaisesRegex(ValueError,'offline_replay_provider_required'):
    w.tick(reviewed_scope={'batch_id':c['batch_id'],'source_ids':[row.source_id],'expected_generation':0,'offline_verified_response':True})
   self.assertEqual(before,list(w.state.db.iterdump()));w.state.close()
 def test_only_bare_ai_can_be_filler_not_action_negation_number_or_mix(self):
  for text,valid in [(' 哎！ ',True),('哎先别删',False),('哎5元',False),('哎保留原音',False),('哎嗯',False)]:
   group={'indices':[0],'clean':text,'distilled':'','reading':{'role':'background','retained_indices':[],'excluded':[{'index':0,'reason':'filler'}],'summary':''}}
   if valid:validate_reading(group,[{'i':0,'text':text}])
   else:
    with self.assertRaises(ValueError):validate_reading(group,[{'i':0,'text':text}])
 def test_completed_response_replay_preserves_exhausted_attempts_and_ledger(self):
  for kind in ('terms','bare_interjection'):
   with self.subTest(kind=kind),tempfile.TemporaryDirectory() as td:
    root=Path(td);text='先保留原音。哎。';row=Source('a'*64,'s',0,'2026-10-18',1792298100.,60,text,digest(text),'local','b'*64,({'text':text,'start':0.,'end':4.},));units=utterances([row]);bid=digest({'source_batch':batch_identity([row]),'schema':GROUP_SCHEMA})
    g={'title':'原音保留','indices':[0,1],'continue_previous':False,'clean':text,'distilled':'## 原音保留\n\n先保留原音。','reading':{'role':'main','retained_indices':[0],'excluded':[{'index':1,'reason':'filler'}],'summary':'先保留原音。'}};v={'groups':[g],'uncertain_terms':[{'term':'示例词','note':'待确认'}] if kind=='terms' else []}
    w=Worker({'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions'),'feishu':False,'conversation_mode':True},provider=object());b={'id':bid,'date':row.date,'session':row.session,'sequence':0,'sequences':[0],'captured':row.captured,'sources':[row.identity()],'attempts':3,'retry_after':time.time()+9999,'last_error':'editor_validation_failed'};w.state.put('batch:'+bid,b)
    a=w.state.admit('2026-10-18',100,2000000);w.state.usage(a,{'prompt_tokens':5,'completion_tokens':5,'total_tokens':10,'response_metadata':{'model_matches_requested':True,'finish_reason':'stop'}});rejected={'reason':'editor_terms_invalid' if kind=='terms' else 'editor_validation_failed','sources':[row.identity()],'model_output':v};w.state.put('rejected:'+str(a),rejected);binding={'approved_for_apply':True,'generation':0,'batch_id':bid,'rejected_hash':digest(rejected),'previous_hash':digest(None),'context_hash':digest(''),'reviewed_at':time.time()};before=list(w.state.db.execute('select * from admissions'));data={(row.date,row.session):{0:row}}
    with patch('manual_pending.discover',return_value=data),patch('text_worker.discover',return_value=data):result=replay(w,a,binding,[row],None,'')
    saved=w.state.get('batch:'+bid);self.assertEqual(saved['attempts'],3);self.assertEqual(saved['retry_after'],b['retry_after']);self.assertEqual(before,list(w.state.db.execute('select * from admissions')));self.assertEqual(w.state.get('rejected:'+str(a)),rejected);self.assertEqual(result['model_api_calls'],0)
    with patch('manual_pending.discover',return_value=data):replay(w,a,binding,[row],None,'')
    self.assertEqual(w.state.get('batch:'+bid)['attempts'],3)
    from dataclasses import replace
    snapshot=list(w.state.db.iterdump())
    for wrong in [dict(binding,rejected_hash='changed'),dict(binding,context_hash='changed'),dict(binding,generation=1)]:
     with self.assertRaises((ValueError,RuntimeError)):replay(w,a,wrong,[row],None,'')
     self.assertEqual(snapshot,list(w.state.db.iterdump()))
    with self.assertRaises(ValueError):replay(w,a,binding,[replace(row,receipt_hash='c'*64)],None,'')
    self.assertEqual(snapshot,list(w.state.db.iterdump()));w.state.close()

if __name__=='__main__':unittest.main()
