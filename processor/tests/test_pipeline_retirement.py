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
