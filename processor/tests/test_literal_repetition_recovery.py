import copy,json,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from text_output.editorial import retain_literal_repetitions,validate_reading
from text_output.conversation import utterances,GROUP_SCHEMA
from text_output.core import Source,digest,batch_identity
from text_worker import Worker
from recover_rejected import replay


def plan(unit='先验证需求。',clean=None,distilled=None,reason='repetition',role='main'):
 clean=unit if clean is None else clean;distilled=unit if distilled is None else distilled
 units=[{'i':0,'text':'请'+unit},{'i':1,'text':unit}]
 group={'title':'验证需求','indices':[0,1],'continue_previous':False,'clean':clean,'distilled':'## 验证需求\n\n'+distilled,
 'reading':{'role':role,'retained_indices':[0],'excluded':[{'index':1,'reason':reason}],'summary':'先验证需求。'}}
 return {'groups':[group],'uncertain_terms':[]},units

class LiteralRetentionTests(unittest.TestCase):
 def test_both_full_drafts_prove_literal_retention_without_mutating_response(self):
  v,u=plan();original=copy.deepcopy(v);new,actions=retain_literal_repetitions(v,u)
  self.assertEqual(v,original);self.assertEqual(new['groups'][0]['reading']['retained_indices'],[0,1]);self.assertEqual(actions[0]['original_reason'],'repetition');validate_reading(new['groups'][0],u)
  for field in ('title','indices','clean','distilled','continue_previous'):self.assertEqual(v['groups'][0][field],new['groups'][0][field])
 def test_missing_one_draft_title_only_negation_numbers_and_nonrepeat_are_not_repaired(self):
  cases=[plan(clean='其他内容。'),plan(distilled='其他内容。'),plan(distilled='## 先验证需求。'),plan('不要删除。',clean='要删除。'),plan('预算-5元。',clean='预算5元。'),plan('预算1.5元。',distilled='预算15元。'),plan('预算1/2元。',clean='预算12元。'),plan('not able',clean='notable',distilled='notable'),plan(reason='promotion'),plan(role='background')]
  for v,u in cases:
   with self.subTest(v=v):
    changed,actions=retain_literal_repetitions(v,u);self.assertEqual(actions,[]);self.assertEqual(changed,v)
 def test_existing_exact_repeat_stays_excluded_and_numeric_difference_not_equal(self):
  v,u=plan();u[0]['text']=u[1]['text'];new,actions=retain_literal_repetitions(v,u);self.assertEqual(actions,[]);validate_reading(new['groups'][0],u)
  v,u=plan('预算1.5元。',clean='预算15元。',distilled='预算15元。');u[0]['text']='预算15元。';new,actions=retain_literal_repetitions(v,u)
  self.assertEqual(actions,[])
  with self.assertRaises(ValueError):validate_reading(new['groups'][0],u)
 def test_model_received_response_replays_once_with_history_intact_and_no_admission(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);row=Source('a'*64,'s',0,'2026-10-18',1792298100.,60,'请先验证需求。先验证需求。',digest('text'),'local','b'*64,({'text':'请先验证需求。先验证需求。','start':0.,'end':4.},));units=utterances([row]);v,_=plan();bid=digest({'source_batch':batch_identity([row]),'schema':GROUP_SCHEMA})
   w=Worker({'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions'),'feishu':False,'conversation_mode':True},provider=object())
   batch={'id':bid,'date':row.date,'session':row.session,'sequence':0,'sequences':[0],'captured':row.captured,'sources':[row.identity()],'attempts':2,'retry_after':time.time()+600,'last_error':'editor_validation_failed'};w.state.put('batch:'+bid,batch)
   admission=w.state.admit('2026-10-18',100,2000000);w.state.usage(admission,{'prompt_tokens':5,'completion_tokens':5,'total_tokens':10,'response_metadata':{'model_matches_requested':True,'finish_reason':'stop','effective_effort':'high'}})
   rejected={'reason':'editor_validation_failed','model_output':v,'sources':[row.identity()]};w.state.put('rejected:'+str(admission),rejected)
   binding={'approved_for_apply':True,'generation':0,'batch_id':bid,'rejected_hash':digest(rejected),'previous_hash':digest(None),'context_hash':digest(''),'reviewed_at':time.time()};before=list(w.state.db.execute('select * from admissions'));data={(row.date,row.session):{0:row}}
   with patch('manual_pending.discover',return_value=data),patch('text_worker.discover',return_value=data):result=replay(w,admission,binding,[row],None,'')
   self.assertEqual(result['model_api_calls'],0);saved=w.state.get('batch:'+bid);self.assertEqual(saved['provider']['admission_id'],admission);self.assertTrue(saved['provider']['response_reused']);self.assertEqual(w.state.get('rejected:'+str(admission)),rejected);self.assertEqual(list(w.state.db.execute('select * from admissions')),before)
   with patch('manual_pending.discover',return_value=data):replay(w,admission,binding,[row],None,'')
   self.assertEqual(len(w._saved_batches(row.date)),1)
   bad=dict(binding,context_hash=digest('changed'));snapshot=list(w.state.db.iterdump())
   with self.assertRaises(ValueError):replay(w,admission,bad,[row],None,'')
   self.assertEqual(snapshot,list(w.state.db.iterdump()));w.state.close()

if __name__=='__main__':unittest.main()
