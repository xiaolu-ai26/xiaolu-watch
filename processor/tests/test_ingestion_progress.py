import sys,tempfile,json,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from text_output.store import State
from text_output.core import Source,digest,batch_identity
from text_output.conversation import GROUP_SCHEMA
from context_analysis.ingestion import progress

class IngestionTests(unittest.TestCase):
 def test_ready_frontier_does_not_jump_missing_and_status_counts_unique_sources(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);session=root/'sessions/s';session.mkdir(parents=True);state=State(root/'db')
   for i in range(4):
    m={'is_test':False,'capture_date':'2026-01-01','sha256':str(i)*64,'capture_started_at':f'2026-01-01T10:0{i}:00Z','duration_s':60}
    (session/(str(i)+'.json')).write_text(json.dumps({'metadata':m}))
   state.put('batch:a',{'date':'2026-01-01','output':{'clean':'x'},'sources':[{'source_id':'0'*64},{'source_id':'2'*64}]})
   state.put('batch:b',{'date':'2026-01-01','last_error':'daily_text_token_budget_reached','sources':[{'source_id':'1'*64}]})
   ready={i:Source(str(i)*64,'s',i,'2026-01-01',1767261600+i*60,60,'text',digest('text'),'local','f'*64,()) for i in range(3)};ready[3]=None
   active=state.get('batch:b');active['id']=batch_identity([ready[1]]);state.put('batch:b',active)
   with patch('context_analysis.ingestion.discover',return_value={('2026-01-01','s'):ready}):p=progress({'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions')},state,'2026-01-01',None)
   self.assertEqual(p['latest_recorded_at'],'2026-01-01T10:04:00Z');self.assertEqual(p['latest_transcribed_at'],'2026-01-01T10:03:00Z');self.assertEqual(p['latest_organized_at'],'2026-01-01T10:01:00Z');self.assertEqual(p['queued_asr_count'],1);self.assertEqual(p['queued_text_count'],1);self.assertEqual(p['state'],'budget_wait');self.assertNotIn('source_id',str(p))
   active['id']='legacy-schema-id';state.put('batch:b',active)
   with patch('context_analysis.ingestion.discover',return_value={('2026-01-01','s'):ready}):p=progress({'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions')},state,'2026-01-01',None)
   self.assertEqual(p['state'],'processing');self.assertEqual(p['queued_text_count'],1)
   active['id']=batch_identity([ready[0],ready[1]]);active['sources']=[{'source_id':'0'*64},{'source_id':'1'*64}];state.put('batch:b',active)
   with patch('context_analysis.ingestion.discover',return_value={('2026-01-01','s'):ready}):p=progress({'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions')},state,'2026-01-01',None)
   self.assertEqual(p['state'],'processing');self.assertEqual(p['queued_text_count'],1);state.close()
 def test_empty_day_times_unknown(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);state=State(root/'db')
   with patch('context_analysis.ingestion.discover',return_value={}):p=progress({'asr_outbox':str(root/'asr'),'receiver_sessions':str(root/'sessions')},state,'2026-01-01',None)
   self.assertEqual(p['state'],'empty');self.assertIsNone(p['latest_recorded_at']);self.assertIsNone(p['latest_organized_at']);state.close()
