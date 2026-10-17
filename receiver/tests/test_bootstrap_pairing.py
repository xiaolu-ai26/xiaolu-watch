import _support  # noqa: F401  (adds receiver/ to sys.path)
import hashlib,http.client,json,sqlite3,tempfile,threading,unittest,uuid
from pathlib import Path
from bootstrap_pairing import Bootstrap,DENIED
from https_ingress import Server

class BootstrapChecks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'private').mkdir();self.now=[1000.]
  (self.root/'private/config.json').write_text(json.dumps({'pairing_secret':'A'*43}),encoding='utf-8');self.store=Bootstrap(self.root,lambda:self.now[0]);self.attempt=str(uuid.uuid4())
 def tearDown(self):self.tmp.cleanup()
 def test_hash_ttl_idempotent_binding_and_restart(self):
  code,end=self.store.issue('https://receiver.example');self.assertEqual(len(code),16);self.assertEqual(end,1600.)
  db=sqlite3.connect(self.store.db);row=db.execute('select digest from codes').fetchone();db.close();self.assertEqual(row[0],hashlib.sha256(code.encode()).hexdigest());self.assertNotIn(code,self.store.db.read_bytes().decode('latin1'))
  body={'code':code,'attempt_id':self.attempt};first=self.store.claim(body,'peer');self.assertEqual(first[0],200)
  self.assertEqual(self.store.claim({'code':code,'attempt_id':str(uuid.uuid4())},'peer'),(401,DENIED))
  recovered=Bootstrap(self.root,lambda:self.now[0]).claim(body,'peer');self.assertEqual(recovered,first)
  self.now[0]=1600.;self.assertEqual(self.store.claim(body,'peer'),(401,DENIED))
 def test_unknown_uniform_and_global_peer_limits(self):
  a=self.store.claim({'code':'B'*16,'attempt_id':self.attempt},'peer');b=self.store.claim({'code':'bad','attempt_id':self.attempt},'peer');self.assertEqual(a,b);self.assertEqual(a,(401,DENIED))
  for _ in range(3):self.store.claim({'code':'B'*16,'attempt_id':self.attempt},'peer')
  self.assertEqual(self.store.claim({'code':'B'*16,'attempt_id':self.attempt},'peer')[0],429)
  for n in range(5):self.store.claim({'code':'B'*16,'attempt_id':self.attempt},'other'+str(n))
  self.assertEqual(self.store.claim({'code':'B'*16,'attempt_id':self.attempt},'new')[0],429)
 def test_config_rotation_fails_closed(self):
  code,_=self.store.issue('https://receiver.example');(self.root/'private/config.json').write_text(json.dumps({'pairing_secret':'B'*43}),encoding='utf-8');self.assertEqual(self.store.claim({'code':code,'attempt_id':self.attempt},'peer'),(401,DENIED))
 def test_concurrent_first_claim_one_attempt_wins(self):
  code,_=self.store.issue('https://receiver.example');barrier=threading.Barrier(2);results=[]
  def claim():
   barrier.wait();results.append(self.store.claim({'code':code,'attempt_id':str(uuid.uuid4())},'peer')[0])
  threads=[threading.Thread(target=claim) for _ in range(2)]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertEqual(sorted(results),[200,401])
 def test_bound_claim_budget_is_strict(self):
  code,_=self.store.issue('https://receiver.example');body={'code':code,'attempt_id':self.attempt}
  for _ in range(8):
   self.assertEqual(self.store.claim(body,'peer')[0],200);self.now[0]+=13
  self.assertEqual(self.store.claim(body,'peer'),(401,DENIED))
 def test_http_bound_route_limits_and_no_store(self):
  code,_=self.store.issue('https://receiver.example');server=Server(('127.0.0.1',0),1,self.store);threading.Thread(target=server.serve_forever,daemon=True).start()
  def request(path,payload,typ='application/json'):
   c=http.client.HTTPConnection(*server.server_address,timeout=3);c.request('POST',path,payload,{'Content-Type':typ});r=c.getresponse();raw=r.read();cache=r.getheader('Cache-Control');c.close();return r.status,raw,cache
  try:
   result=request('/v2/pair/claim',json.dumps({'code':code,'attempt_id':self.attempt}));self.assertEqual(result[0],200);self.assertEqual(result[2],'no-store');self.assertEqual(json.loads(result[1])['endpoint'],'https://receiver.example')
   self.assertEqual(request('/v2/pair/claim?code='+code,b'{}')[0],404)
   self.assertEqual(request('/v2/pair/claim',b'x'*1025)[0],413)
   self.assertEqual(request('/v2/pair/claim',b'{}','text/plain')[0],415)
   self.assertEqual(request('/v2/pair/claim',b'{"code":"x","code":"y","attempt_id":"z"}')[0],400)
  finally:server.shutdown();server.server_close()
if __name__=='__main__':unittest.main()
