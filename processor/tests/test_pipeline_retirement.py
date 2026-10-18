"""Synthetic bytes only. No ASR model, network, notes, or real deletion."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'receiver'))
from context_store import ContextStore,StoreError

spec=importlib.util.spec_from_file_location('retirement_pipeline',Path(__file__).resolve().parents[1]/'pipeline.py');pipeline=importlib.util.module_from_spec(spec);spec.loader.exec_module(pipeline)
class RetirementBridge(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.ledger=ContextStore(self.root/'context');self.audio=self.root/'fixture.wav';self.audio.write_bytes(b'PUBLIC pipeline synthetic fixture');os.utime(self.audio,(time.time()-120,)*2);self.sha=hashlib.sha256(self.audio.read_bytes()).hexdigest();self.env=patch.dict(os.environ,{'WATCH_MEMORY_CONTEXT_ROOT':str(self.ledger.root)});self.env.start()
  pipeline.configure({'notes':{'dir':str(self.root/'notes')},'feishu':{'enabled':False}},self.root/'home');self.db=pipeline.db_open()
 def tearDown(self):self.db.close();self.env.stop();self.tmp.cleanup()
 def retire(self):
  with self.ledger.generation_guard():
   with self.ledger.connect() as c:c.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,1)',(self.sha,'public-fixture','retired','[]'))
 def test_retired_process_and_pull_do_not_recreate_receipts_or_mutate_old_job(self):
  key='2026-01-01:'+self.sha;old={'source_id':self.sha,'status':'done','historic_ack':'preserve'};pipeline.save(self.db,key,old);before=self.db.execute('SELECT payload FROM jobs WHERE id=?',(key,)).fetchone()[0];self.retire()
  with patch.object(pipeline,'_process_unretired',side_effect=AssertionError('must not process')):
   result=pipeline.process(self.db,self.audio,'2026-01-01');self.assertEqual(result['status'],'retired')
  self.assertEqual(self.db.execute('SELECT payload FROM jobs WHERE id=?',(key,)).fetchone()[0],before)
  target=self.root/'copied'
  with self.assertRaises(StoreError):pipeline.pull(self.sha,target)
  self.assertFalse(target.exists())
 def test_same_root_nested_layers_reenter_but_other_thread_still_blocks(self):
  result=[]
  with self.ledger.generation_guard() as first:
   with ContextStore(self.ledger.root).generation_guard(first['generation']):pass
   def contender():
    try:
     with ContextStore(self.ledger.root).generation_guard(timeout=.15):result.append('bad-acquired')
    except StoreError as e:result.append(e.code)
   t=threading.Thread(target=contender);t.start();t.join(2);self.assertFalse(t.is_alive())
  self.assertEqual(result,['publication_busy'])
 def test_process_publication_scope_holds_guard_against_concurrent_retirement(self):
  def inner(*args,**kwargs):
   with ContextStore(self.ledger.root).generation_guard():return {'status':'nested-ASR-and-pull-safe'}
  with patch.object(pipeline,'_process_unretired',side_effect=inner):self.assertEqual(pipeline.process(self.db,self.audio,'2026-01-01')['status'],'nested-ASR-and-pull-safe')
 def test_queue_gap_unlocks_other_threads_and_rechecks_before_copy(self):
  observed=[]
  def worker():
   def writer():
    with ContextStore(self.ledger.root).generation_guard(timeout=.2):observed.append('acquired-during-ASR-queue')
   t=threading.Thread(target=writer);t.start();t.join(1);self.assertFalse(t.is_alive());self.retire()
  with patch.object(pipeline,'transfer'),patch.object(pipeline,'run_asr',side_effect=worker),patch.object(pipeline,'pull',side_effect=AssertionError('retired result must not copy')):
   result=pipeline.process(self.db,self.audio,'2026-01-01')
  self.assertEqual(observed,['acquired-during-ASR-queue']);self.assertEqual(result['status'],'retired');self.assertFalse((pipeline.HOME/'receipts/2026-01-01'/self.sha/'client-receipt.json').exists())
 def test_final_copy_provenance_job_ack_share_one_reacquired_guard(self):
  observed=[];pipeline.PUBLISH_OUTPUTS=False
  def attempts(expected):
   def other():
    try:
     with ContextStore(self.ledger.root).generation_guard(timeout=.1):observed.append(True)
    except StoreError:observed.append(False)
   t=threading.Thread(target=other);t.start();t.join(1);self.assertFalse(t.is_alive());self.assertEqual(observed[-1],expected)
  receipt={'source_id':self.sha,'capture_date':'2026-01-01','original_name':self.audio.name,'is_test':True,'source_url':None,'capture_date_basis':'explicit','segments':[{'start':0,'end':1,'text':'PUBLIC fixture'}]}
  def pull(sid,dest):
   attempts(False)
   for name in ('receipt.json','transcript.json'):(dest/name).write_text(json.dumps(receipt))
   return receipt
  original_save=pipeline.save
  def save(*args):attempts(False);return original_save(*args)
  with patch.object(pipeline,'transfer',side_effect=lambda *a,**k:attempts(False)),patch.object(pipeline,'run_asr',side_effect=lambda:attempts(True)),patch.object(pipeline,'pull',side_effect=pull),patch.object(pipeline,'save',side_effect=save):
   result=pipeline.process(self.db,self.audio,'2026-01-01',is_test=True)
  self.assertEqual(result['status'],'done');self.assertTrue((pipeline.HOME/'receipts/2026-01-01'/self.sha/'client-receipt.json').is_file());self.assertEqual(observed,[False,True,False,False,False])
