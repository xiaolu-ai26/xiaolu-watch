import datetime as dt
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from _support import REPO
from context_store import ContextStore,StoreError,atomic_json
from context_api import Server
from deletion import Deletions

def uid():return str(uuid.uuid4())
def payload():return {'version':3,'request_id':uid(),'observed_at':'2026-01-01T01:00:00Z','timezone':'Asia/Taipei','function_flags':{'health_enabled':True,'location_enabled':False,'share_derived_with_ai':False,'control_revision':1},'collection_state':{'health':'available','location':'not_queried'},'health':[{'id':uid(),'source':'public.fixture','revision':1,'kind':'heart_rate','start_at':'2026-01-01T00:00:00Z','end_at':'2026-01-01T00:00:01Z','value':70,'unit':'count/min'}],'health_deleted':[],'places':[]}
class ContextTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=ContextStore(self.root/'private');issued=self.store.issue_pair();self.principal=issued['principal_id'];self.code=issued['code'];self.token=self.store.token(self.principal)
 def tearDown(self):self.tmp.cleanup()
 def test_claim_persistent_binding_and_purpose_separation(self):
  body={'code':self.code,'attempt_id':uid()};status,result=self.store.claim(body,'fixture','https://example.invalid');self.assertEqual(status,200);self.assertEqual(self.store.claim(body,'fixture','https://example.invalid')[1]['token'],result['token'])
  self.assertEqual(self.store.claim(dict(body,attempt_id=uid()),'fixture','https://example.invalid')[0],401)
  self.assertEqual(self.store.authorize(result['token'],'context:write'),self.principal)
  with self.assertRaises(StoreError):self.store.authorize('A'*43,'context:write')
 def test_context_durable_retry_disable_and_deleted_no_resurrection(self):
  v=payload();ack=self.store.ingest(self.principal,v);self.assertEqual(ack,self.store.ingest(self.principal,v));self.assertEqual(len(ContextStore(self.root/'private').export_context('2026-01-01')['health']),1)
  disabled=payload();disabled['health']=[];disabled['function_flags']['control_revision']=2;disabled['function_flags']['health_enabled']=False;self.store.ingest(self.principal,disabled)
  late=payload();self.assertEqual(self.store.ingest(self.principal,late)['ignored_disabled_rows'],1)
  deleted=payload();deleted['health']=[];deleted['health_deleted']=[v['health'][0]['id']];deleted['function_flags']['control_revision']=3;self.store.ingest(self.principal,deleted)
  replay=payload();replay['health']=v['health'];replay['function_flags']['control_revision']=3;self.store.ingest(self.principal,replay);self.assertEqual(self.store.export_context('2026-01-01')['health'],[])
 def test_strict_validation_and_idempotency_conflict(self):
  v=payload();self.store.ingest(self.principal,v);v['health'][0]['value']=80
  with self.assertRaises(StoreError) as err:self.store.ingest(self.principal,v)
  self.assertEqual(err.exception.status,409)
  for edit in [lambda x:x['health'][0].update(value=float('nan')),lambda x:x['health'][0].update(unit='ms'),lambda x:x.update(timezone='../../bad')]:
   v=payload();edit(v)
   with self.assertRaises(StoreError):self.store.ingest(self.principal,v)
 def projection(self,coarse=False):
  root=self.root/'reading';root.mkdir(exist_ok=True);sid='a'*64
  cards=[dict(id=k,version=1,title=k,summary='summary',start_at='2026-01-01T00:00:00Z',end_at='2026-01-01T00:01:00Z',owner_label='unknown',clean='clean',distilled='distilled') for k in ('A','B')]
  atomic_json(root/'2026-01-01.json',dict(version=3,date='2026-01-01',timezone='UTC',generated_at='2026-01-01T00:00:00Z',revision=1,overview={'state':'ready','text':'summary A+B'},body={'state':'unknown','lines':[],'sample_count':0},coverage=[],cards=cards,private_secret='must not escape'))
  atomic_json(root/'2026-01-01.sources.json',dict(version=1,revision=1,cards=[dict(id=k,version=1,group_id=k,generation=0,source_spans=[dict(source_id=sid,start=i*2,end=(i+1)*2,precision='parent_span' if coarse else 'asr_word_span',overlap=False)]) for i,k in enumerate(('A','B'))]));return root
 def test_deletion_retires_only_A_and_original_replay_keeps_B(self):
  deletion=Deletions(self.store,self.projection());plan=deletion.plan(self.principal,'A',{'request_id':uid(),'expected_card_version':1});self.assertEqual(plan['audio_status'],'deletable')
  commit={'request_id':uid(),'plan_revision':1,'confirmation_token':plan['confirmation_token']};result=deletion.commit(self.principal,plan['plan_id'],commit);self.assertEqual(result['retained_other_cards'],['B']);self.assertEqual(deletion.commit(self.principal,plan['plan_id'],commit),result)
  snap=self.store.retirement_snapshot();self.assertEqual(snap['group_ids'],['A']);self.assertEqual(snap['source_ids'],[]);self.assertEqual(snap['retired_raw_source_ids'],['a'*64]);self.assertIsNotNone(self.store.retirement_for('a'*64))
 def test_shared_coarse_audio_is_pending_not_false_completion(self):
  deletion=Deletions(self.store,self.projection(True));plan=deletion.plan(self.principal,'A',{'request_id':uid(),'expected_card_version':1});self.assertEqual(plan['audio_status'],'shared_audio_pending')
 def test_actual_http_auth_and_raw_data_never_read(self):
  server=Server(('127.0.0.1',0),self.store,self.projection(),'https://example.invalid');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   for path,auth,expected in [('/v3/phone/today?date=2026-01-01',None,401),('/v3/phone/today?date=2026-01-01',self.token,200),('/v3/phone/context',self.token,404),('/private/context.sqlite3',self.token,404)]:
    con=http.client.HTTPConnection(*server.server_address,timeout=5);con.request('GET',path,headers={'Authorization':'Bearer '+auth} if auth else {});r=con.getresponse();raw=r.read();self.assertEqual(r.status,expected);self.assertEqual(r.getheader('Cache-Control'),'no-store');self.assertNotIn(b'private_secret',raw);con.close()
  finally:server.shutdown();server.server_close();thread.join()

 def test_ingress_exact_phone_routes_and_durable_http_post(self):
  import https_ingress
  api=Server(('127.0.0.1',0),self.store,self.projection(),'https://example.invalid');a=threading.Thread(target=api.serve_forever,daemon=True);a.start()
  gate=https_ingress.Server(('127.0.0.1',0),1,phone_port=api.server_port);g=threading.Thread(target=gate.serve_forever,daemon=True);g.start()
  try:
   v=payload();raw=json.dumps(v).encode();headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'}
   con=http.client.HTTPConnection(*gate.server_address,timeout=5);con.request('POST','/v3/phone/context',raw,headers);r=con.getresponse();ack=json.loads(r.read());self.assertEqual(r.status,200);self.assertEqual(ack['request_id'],v['request_id']);self.assertTrue(ack['accepted']);self.assertEqual(r.getheader('Cache-Control'),'no-store');con.close()
   self.assertEqual(len(ContextStore(self.store.root).export_context('2026-01-01')['health']),1)
   for path in ['/v3/phone/today?date=2026-01-01&path=secret','/v3/phone/private','/v3/phone/context?secret=x','/v3/phone/today/../../private']:
    con=http.client.HTTPConnection(*gate.server_address,timeout=5);con.request('GET',path,headers=headers);r=con.getresponse();r.read();self.assertEqual(r.status,404);con.close()
  finally:gate.shutdown();gate.server_close();api.shutdown();api.server_close();g.join();a.join()

 def test_unqueried_other_domain_does_not_erase_available(self):
  first=payload();self.store.ingest(self.principal,first)
  location=payload();location['health']=[];location['function_flags']['control_revision']=2;location['collection_state']={'health':'not_queried','location':'available'};self.store.ingest(self.principal,location)
  self.assertEqual(self.store.export_context('2026-01-01')['collection_state'],{'health':'available','location':'available'})
