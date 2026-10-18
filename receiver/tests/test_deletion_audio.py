import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from _support import REPO
from context_store import ContextStore,atomic_json
from deletion import Deletions
from deletion_audio import AudioRetirement,RATE

class AudioTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=ContextStore(self.root/'private');self.principal=self.store.issue_pair()['principal_id'];self.audio=self.root/'audio';self.audio.mkdir();self.reading=self.root/'reading';self.reading.mkdir();self.ffmpeg=shutil.which('ffmpeg');self.assertTrue(self.ffmpeg)
  original=self.audio/'public-tone.wav'
  subprocess.run([self.ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','aevalsrc=if(lt(t\\,2)\\,0.3*sin(2*PI*440*t)\\,0.3*sin(2*PI*880*t)):s=16000:d=4','-c:a','pcm_f32le',str(original)],check=True,capture_output=True)
  self.sha=hashlib.sha256(original.read_bytes()).hexdigest();self.original=self.audio/(self.sha+'.wav');original.rename(self.original)
  self.asr=self.root/'asr';target=self.asr/'outbox'/self.sha;target.mkdir(parents=True);(self.asr/'state').mkdir()
  value={'source_id':self.sha,'status':'done','segments':[{'start':0,'end':4,'text':'PUBLIC FIXTURE A B','words':[{'start':.2,'end':1.8,'word':'PUBLIC A'},{'start':2.2,'end':3.8,'word':'PUBLIC B'}]}]}
  atomic_json(target/'receipt.json',value);atomic_json(target/'transcript.json',value);(target/'transcript.md').write_text('PUBLIC FIXTURE A B');atomic_json(self.asr/'state'/(self.sha+'.json'),value)
  self.worker=AudioRetirement(self.store,{'ffmpeg_path':self.ffmpeg,'managed_audio_roots':[str(self.audio)],'managed_asr_roots':[str(self.asr)]});self.pcm=self.worker.decode(self.original)
  self.cards(False)
 def tearDown(self):self.tmp.cleanup()
 def cards(self,coarse):
  atomic_json(self.reading/'2026-01-01.json',{'revision':1,'cards':[{'id':k,'version':1} for k in ('A','B')]})
  atomic_json(self.reading/'2026-01-01.sources.json',{'revision':1,'cards':[{'id':k,'version':1,'group_id':k,'source_spans':[{'source_id':self.sha,'start':i*2,'end':i*2+2,'precision':'parent_span' if coarse else 'asr_word_span','overlap':False}]} for i,k in enumerate(('A','B'))]})
 def commit(self):
  d=Deletions(self.store,self.reading);plan=d.plan(self.principal,'A',{'request_id':str(uuid.uuid4()),'expected_card_version':1});d.commit(self.principal,plan['plan_id'],{'request_id':str(uuid.uuid4()),'plan_revision':1,'confirmation_token':plan['confirmation_token']});return plan['plan_id']
 def test_delete_A_preserves_B_pcm_and_recoverable_original(self):
  txid=self.commit();self.worker.advance(txid);record=self.store.retirement_for(self.sha);self.assertEqual(record['state'],'retired');self.assertFalse(self.original.exists());self.assertEqual(len(record['retained_assets']),1)
  asset=record['retained_assets'][0];self.assertEqual((asset['original_start_sample'],asset['original_end_sample']),(2*RATE,4*RATE));pcm=self.worker.decode(self.worker.assets/(asset['asset_id']+'.wav'));self.assertEqual(pcm,self.pcm[2*RATE*4:]);self.assertNotEqual(asset['asset_id'],self.sha)
  trash=self.store.root/'deletion'/txid/self.sha;raw=list(trash.glob('audio-[0-9]*'));self.assertEqual(hashlib.sha256(raw[0].read_bytes()).hexdigest(),self.sha)
  self.assertFalse((self.asr/'outbox'/self.sha/'receipt.json').exists());retained=json.loads((trash/'retained-transcript.json').read_text());self.assertEqual([w['word'] for w in retained['words']],['PUBLIC B'])
  # Restart retry consumes its journal, never recreates A or changes B's bytes.
  restarted=AudioRetirement(ContextStore(self.store.root),self.worker.config);restarted.advance(txid);self.assertEqual(restarted.decode(restarted.assets/(asset['asset_id']+'.wav')),pcm)
 def test_coarse_boundaries_keep_original_explicitly_pending(self):
  self.cards(True);txid=self.commit();self.worker.advance(txid);self.assertTrue(self.original.exists())
  with self.store.connect() as c:state=c.execute('SELECT state FROM deletion_transactions WHERE id=?',(txid,)).fetchone()[0]
  self.assertEqual(state,'shared_audio_pending')
 def test_crash_after_copy_before_unlink_resumes_without_loss(self):
  txid=self.commit();original=self.worker.retire_file;calls=[]
  def crash(path,trash,expected):
   if not calls:
    calls.append(True);shutil.copyfile(path,trash);raise OSError('PUBLIC injected power loss before unlink')
   return original(path,trash,expected)
  self.worker.retire_file=crash;self.worker.advance(txid);self.assertTrue(self.original.exists());self.worker.retire_file=original;self.worker.advance(txid);self.assertFalse(self.original.exists());self.assertEqual(self.store.retirement_for(self.sha)['state'],'retired')

 def test_cross_source_identical_pcm_asset_stays_live_for_other_card(self):
  # Distinct original containers contain the same public synthetic PCM.
  second=self.audio/'second.wav'
  subprocess.run([self.ffmpeg,'-nostdin','-v','error','-f','f32le','-ar','16000','-ac','1','-i','pipe:0','-metadata','comment=PUBLIC SECOND SOURCE','-c:a','pcm_f32le',str(second)],input=self.pcm,check=True,capture_output=True)
  sid2=hashlib.sha256(second.read_bytes()).hexdigest();self.assertNotEqual(sid2,self.sha);second.rename(self.audio/(sid2+'.wav'))
  pub=json.loads((self.reading/'2026-01-01.json').read_text());private=json.loads((self.reading/'2026-01-01.sources.json').read_text())
  pub['cards'] += [{'id':k,'version':1} for k in ('C','D')]
  private['cards'] += [{'id':k,'version':1,'group_id':k,'source_spans':[{'source_id':sid2,'start':i*2,'end':i*2+2,'precision':'asr_word_span','overlap':False}]} for i,k in enumerate(('C','D'))]
  atomic_json(self.reading/'2026-01-01.json',pub);atomic_json(self.reading/'2026-01-01.sources.json',private)
  txA=self.commit();self.worker.advance(txA)
  def delete(card):
   d=Deletions(self.store,self.reading);plan=d.plan(self.principal,card,{'request_id':str(uuid.uuid4()),'expected_card_version':1});d.commit(self.principal,plan['plan_id'],{'request_id':str(uuid.uuid4()),'plan_revision':1,'confirmation_token':plan['confirmation_token']});self.worker.advance(plan['plan_id']);return plan['plan_id']
  delete('C');asset1=self.store.retirement_for(self.sha)['retained_assets'][0]['asset_id'];asset2=self.store.retirement_for(sid2)['retained_assets'][0]['asset_id'];self.assertEqual(asset1,asset2)
  txB=delete('B');shared=self.worker.assets/(asset2+'.wav');self.assertTrue(shared.is_file());self.assertEqual(self.worker.decode(shared),self.pcm[2*RATE*4:])
  # An older transaction retry must not restore its superseded source mapping.
  self.worker.advance(txA);self.worker.advance(txB);self.assertEqual(self.store.retirement_for(self.sha)['retained_assets'],[]);self.assertTrue(shared.is_file())

 def test_copied_asr_layer_archived_but_historical_client_ack_preserved(self):
  copied=self.root/'receipts';day=copied/'2026-01-01'/self.sha;day.mkdir(parents=True)
  for name in ('receipt.json','transcript.json','transcript.md'):
   shutil.copyfile(self.asr/'outbox'/self.sha/name,day/name)
  ack={'source_id':self.sha,'status':'done','publication_status':'disabled'};atomic_json(day/'client-receipt.json',ack)
  worker=AudioRetirement(self.store,dict(self.worker.config,managed_receipt_roots=[str(copied)]));txid=self.commit();worker.advance(txid)
  self.assertEqual(json.loads((day/'client-receipt.json').read_text()),ack)
  self.assertFalse(any((day/name).exists() for name in ('receipt.json','transcript.json','transcript.md')))
  archived=worker.root/txid/self.sha
  self.assertEqual(len(list(archived.glob('receipt-copy-*'))),3)
  remaining=json.loads((archived/'retained-transcript.json').read_text());self.assertEqual([w['word'] for w in remaining['words']],['PUBLIC B'])
