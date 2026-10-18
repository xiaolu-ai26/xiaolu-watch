import copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manual_pending import apply_pending,ManualProvider,unit_hash
from text_output.core import Source,digest,batch_identity
from text_output.conversation import utterances,GROUP_SCHEMA
from text_worker import Worker
from text_output.editorial import READING_POLICY_VERSION

class ManualPendingTests(unittest.TestCase):
 def fixture(self,root):
  row=Source('a'*64,'session',0,'2026-10-18',1792298100.,60,'先验证需求。',digest('先验证需求。'),'local','b'*64,({'text':'先验证需求。','start':0.,'end':2.},))
  units=utterances([row]);bid=digest({'source_batch':batch_identity([row]),'schema':GROUP_SCHEMA})
  worker=Worker({'version':1,'enabled':True,'home':str(root/'state'),'notes_outbox':str(root/'notes'),'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions'),'feishu':False,'conversation_mode':True},provider=object())
  pending={'id':bid,'date':row.date,'session':row.session,'sequence':0,'sequences':[0],'captured':row.captured,'sources':[row.identity()],'attempts':1,'retry_after':9999999999.,'last_error':'daily_text_token_budget_reached'}
  worker.state.put('batch:'+bid,pending);worker.state.put('batch:old',dict(pending,id='old'));worker.state.admit('2026-10-18',100,2000000)
  value={'groups':[{'title':'验证需求','indices':[0],'continue_previous':False,'clean':'先验证需求。','distilled':'## 验证需求\n\n先验证需求。','reading':{'role':'main','retained_indices':[0],'excluded':[],'summary':'先验证需求。'}}],'uncertain_terms':[]}
  candidate={'approved_for_apply':True,'provenance':'codex_reviewed_manual','policy':READING_POLICY_VERSION,'generation':0,'batch_id':bid,'source_ids':[row.source_id],'units_hash':unit_hash(units),'previous_group_id':None,'reviewed_at':123,'value':value}
  return worker,row,candidate,units
 def test_real_worker_materializes_once_and_supersedes_duplicate_pending_without_budget(self):
  with tempfile.TemporaryDirectory() as td:
   w,row,c,u=self.fixture(Path(td))
   with patch('text_worker.discover',return_value={(row.date,row.session):{0:row}}),patch('manual_pending.discover',return_value={(row.date,row.session):{0:row}}):result=apply_pending(w,c)
   self.assertEqual(result['provider_calls'],1);self.assertEqual(w.state.get('consumed:'+row.date+':'+row.session),[0]);self.assertEqual(w.state.get('batch:old')['superseded_by'],c['batch_id'])
   with patch('text_worker.discover',side_effect=AssertionError('retry must not tick')),patch('manual_pending.discover',return_value={(row.date,row.session):{0:row}}):again=apply_pending(w,c)
   self.assertEqual(again['provider_calls'],0);self.assertEqual(w.state.db.execute('select count(*) from admissions').fetchone()[0],1);self.assertEqual(len(w._saved_batches(row.date)),1);w.state.close()
 def test_provider_rejects_changed_unit_and_other_source(self):
  with tempfile.TemporaryDirectory() as td:
   w,row,c,u=self.fixture(Path(td));bad=copy.deepcopy(u);bad[0]['text']='不同内容'
   with self.assertRaises(ValueError):ManualProvider(c).edit_conversations([row],bad,None,'')
   c['source_ids']=['c'*64]
   with self.assertRaises(ValueError):ManualProvider(c).edit_conversations([row],u,None,'')
   w.state.close()
 def test_unapproved_and_already_consumed_elsewhere_preserve_pending(self):
  with tempfile.TemporaryDirectory() as td:
   w,row,c,u=self.fixture(Path(td));old=w.state.get('batch:'+c['batch_id']);c['approved_for_apply']=False
   with self.assertRaises(ValueError):apply_pending(w,c)
   self.assertEqual(old,w.state.get('batch:'+c['batch_id']));c['approved_for_apply']=True
   other=dict(old,id='elsewhere',output={'clean':'已处理','distilled':'已处理'});w.state.put('batch:elsewhere',other)
   with self.assertRaises(ValueError):apply_pending(w,c)
   self.assertEqual(old,w.state.get('batch:'+c['batch_id']));w.state.close()

 def test_invalid_value_or_daily_overview_leaves_entire_state_unchanged(self):
  with tempfile.TemporaryDirectory() as td:
   w,row,c,u=self.fixture(Path(td));before=list(w.state.db.iterdump())
   bad=copy.deepcopy(c);bad['value']['groups'][0]['indices']=[]
   with patch('manual_pending.discover',return_value={(row.date,row.session):{0:row}}),self.assertRaises(ValueError):apply_pending(w,bad)
   self.assertEqual(list(w.state.db.iterdump()),before)
   bad=copy.deepcopy(c);bad['daily_overview']={'text':'错误来源','source_ids':['not-real']}
   with patch('manual_pending.discover',return_value={(row.date,row.session):{0:row}}),self.assertRaises(ValueError):apply_pending(w,bad)
   self.assertEqual(list(w.state.db.iterdump()),before);w.state.close()
 def test_other_pending_and_new_tail_are_not_touched_and_changed_value_retry_refused(self):
  with tempfile.TemporaryDirectory() as td:
   w,row,c,u=self.fixture(Path(td))
   tail=Source('c'*64,row.session,1,row.date,row.captured+60,60,'后续不属于这次。',digest('后续不属于这次。'),'local','d'*64,({'text':'后续不属于这次。','start':0.,'end':2.},))
   other=Source('e'*64,'earlier-session',0,row.date,row.captured-60,60,'其他内容。',digest('其他内容。'),'local','f'*64,({'text':'其他内容。','start':0.,'end':2.},))
   untouched={'id':'unrelated','date':row.date,'session':other.session,'sequence':0,'sequences':[0],'captured':other.captured,'sources':[other.identity()],'attempts':2,'retry_after':9999999999}
   w.state.put('batch:unrelated',untouched);w.state.put('consumed:'+row.date+':'+other.session,[9])
   data={(row.date,row.session):{0:row,1:tail},(row.date,other.session):{0:other}}
   with patch('text_worker.discover',return_value=data),patch('manual_pending.discover',return_value=data):apply_pending(w,c)
   self.assertEqual(w.state.get('batch:unrelated'),untouched);self.assertEqual(w.state.get('consumed:'+row.date+':'+other.session),[9]);self.assertEqual(w.state.get('consumed:'+row.date+':'+row.session),[0])
   changed=copy.deepcopy(c);changed['value']['groups'][0]['clean']='先研究需求。'
   before=list(w.state.db.iterdump())
   with self.assertRaises(ValueError):apply_pending(w,changed)
   self.assertEqual(list(w.state.db.iterdump()),before);w.state.close()

 def test_generation_change_after_preflight_never_calls_provider_or_publishes(self):
  with tempfile.TemporaryDirectory() as td:
   w,row,c,u=self.fixture(Path(td));original_tick=w.tick
   def changed_tick(*args,**kwargs):
    with patch('text_worker.retirement.snapshot',return_value={'generation':1,'group_ids':[],'retired_raw_source_ids':[]}):return original_tick(*args,**kwargs)
   with patch('manual_pending.discover',return_value={(row.date,row.session):{0:row}}),patch.object(w,'tick',side_effect=changed_tick),patch.object(w,'_publish') as publish,self.assertRaisesRegex(RuntimeError,'generation_changed'):
    apply_pending(w,c)
   publish.assert_not_called();self.assertNotIn('output',w.state.get('batch:'+c['batch_id']));self.assertIsNone(w.state.get('consumed:'+row.date+':'+row.session));self.assertEqual(w.state.db.execute('select count(*) from admissions').fetchone()[0],1);w.state.close()

 def test_absent_pending_created_only_after_full_source_validation(self):
  with tempfile.TemporaryDirectory() as td:
   w,row,c,u=self.fixture(Path(td));frozen=w.state.get('batch:'+c['batch_id']);c['pending_batch']=frozen
   with w.state.db:w.state.db.execute('delete from kv where key=?',('batch:'+c['batch_id'],))
   before=list(w.state.db.iterdump());bad=copy.deepcopy(c);bad['pending_batch']['captured']+=1
   with patch('manual_pending.discover',return_value={(row.date,row.session):{0:row}}),self.assertRaises(ValueError):apply_pending(w,bad)
   self.assertEqual(list(w.state.db.iterdump()),before)
   data={(row.date,row.session):{0:row}}
   with patch('manual_pending.discover',return_value=data),patch('text_worker.discover',return_value=data):result=apply_pending(w,c)
   self.assertEqual(result['provider_calls'],1);self.assertEqual(w.state.get('batch:'+c['batch_id'])['sources'],[row.identity()]);w.state.close()

if __name__=='__main__':unittest.main()
