import json,sys,tempfile,unittest,subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from text_output.store import State,TokenBudgetExceeded

def usage(prompt=12,completion=8,finish='stop'):
 return {'prompt_tokens':prompt,'completion_tokens':completion,'total_tokens':prompt+completion,'prompt_cache_hit_tokens':5,'prompt_cache_miss_tokens':prompt-5,'prompt_tokens_details':{'cached_tokens':5},'completion_tokens_details':{'reasoning_tokens':3},'response_metadata':{'model_matches_requested':True,'finish_reason':finish}}

class BudgetSettlementTests(unittest.TestCase):
 def test_settlement_preserves_admissions_and_restart_is_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'db';s=State(p);a=s.admit('today',80,100)
   with s.db:s.db.execute('UPDATE admissions SET usage=? WHERE id=?',(json.dumps(usage()),a))
   old=list(s.db.execute('select * from admissions'));self.assertEqual(s.settle_completed(),1);self.assertEqual(old,list(s.db.execute('select * from admissions')));self.assertEqual(s.budget('today')['committed_tokens'],20);s.close();s=State(p)
   self.assertEqual(s.settle_completed(),0);s.usage(a,usage());self.assertEqual(s.db.execute('select count(*) from admission_settlements').fetchone()[0],1);self.assertEqual(old,list(s.db.execute('select * from admissions')));s.close()
 def test_unknown_partial_and_invalid_usage_never_release_reservation(self):
  bads=[None,usage(finish='length'),dict(usage(),total_tokens=1),dict(usage(),prompt_tokens=True),dict(usage(),prompt_cache_hit_tokens=999),dict(usage(),completion_tokens_details={'reasoning_tokens':9}),dict(usage(),prompt_tokens_details={'cached_tokens':6})]
  for item in bads:
   with self.subTest(item=item),tempfile.TemporaryDirectory() as td:
    s=State(Path(td)/'db');a=s.admit('today',80,100)
    if item is not None:s.usage(a,item)
    s.settle_completed();self.assertEqual(s.budget('today')['committed_tokens'],80)
    with self.assertRaises(TokenBudgetExceeded):s.admit('today',21,100)
    s.close()
 def test_cache_and_reasoning_are_not_double_counted_or_discounted(self):
  with tempfile.TemporaryDirectory() as td:
   s=State(Path(td)/'db');a=s.admit('today',80,100);s.usage(a,usage());self.assertEqual(s.budget('today')['committed_tokens'],20);self.assertEqual(s.budget('today')['reserved_tokens'],80);self.assertEqual(s.budget('today')['unsettled_reserved_tokens'],0);s.close()
 def test_conflicting_usage_and_missing_admission_are_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   s=State(Path(td)/'db');a=s.admit('today',80,100);s.usage(a,usage());old=list(s.db.iterdump())
   with self.assertRaises(ValueError):s.usage(a,usage(completion=9))
   with self.assertRaises(ValueError):s.usage(999,usage())
   self.assertEqual(old,list(s.db.iterdump()));s.close()
 def test_concurrent_settlement_and_admission_cannot_double_spend(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'db';s=State(p);a=s.admit('today',80,100)
   with s.db:s.db.execute('UPDATE admissions SET usage=? WHERE id=?',(json.dumps(usage()),a))
   s.close()
   def run(_):
    state=State(p)
    try:state.admit('today',25,100);return True
    except TokenBudgetExceeded:return False
    finally:state.close()
   with ThreadPoolExecutor(max_workers=8) as pool:result=list(pool.map(run,range(8)))
   s=State(p);self.assertEqual(sum(result),3);self.assertEqual(s.budget('today')['committed_tokens'],95);self.assertEqual(s.db.execute('select count(*) from admission_settlements').fetchone()[0],1);s.close()
 def test_separate_processes_share_atomic_reservation_balance(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'db';s=State(p);a=s.admit('today',80,100)
   with s.db:s.db.execute('UPDATE admissions SET usage=? WHERE id=?',(json.dumps(usage()),a))
   s.close()
   code="import sys;sys.path.insert(0,sys.argv[2]);from text_output.store import State,TokenBudgetExceeded;s=State(sys.argv[1]);\ntry:s.admit('today',25,100);print('yes')\nexcept TokenBudgetExceeded:print('no')\nfinally:s.close()"
   children=[subprocess.Popen([sys.executable,'-B','-c',code,str(p),str(Path(__file__).resolve().parents[1])],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(6)]
   answers=[]
   for child in children:
    out,err=child.communicate(timeout=15);self.assertEqual(child.returncode,0,err);answers.append(out.strip())
   self.assertEqual(answers.count(b'yes'),3);s=State(p);self.assertEqual(s.budget('today')['committed_tokens'],95);s.close()
 def test_reported_overrun_is_charged_not_capped_to_reservation(self):
  with tempfile.TemporaryDirectory() as td:
   s=State(Path(td)/'db');a=s.admit('today',20,100);s.usage(a,usage(prompt=80,completion=30));self.assertEqual(s.budget('today')['committed_tokens'],110)
   with self.assertRaises(TokenBudgetExceeded):s.admit('today',1,100)
   s.close()

if __name__=='__main__':unittest.main()
