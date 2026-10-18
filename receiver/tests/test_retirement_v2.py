import base64
import hashlib
import json
import http.client
import uuid
from test_wire_v2 import SealedV2,SECRET
from context_store import ContextStore
import wire_v2 as w

class RetirementV2(SealedV2):
 def test_authenticated_retirement_is_not_stored_ack_and_prevents_restore(self):
  ledger=ContextStore(self.root/'context');txid=str(uuid.uuid4())
  with ledger.connect() as c:c.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,1)',(self.sha,txid,'pending','[]'))
  self.server.context_store=ledger
  ch=str(uuid.uuid4());body=w.seal(SECRET,self.meta,self.audio,self.path,ch)
  con=http.client.HTTPConnection(*self.server.server_address,timeout=5);con.request('PUT',self.path,body,{'Content-Type':'application/octet-stream','X-Watch-Challenge':ch});r=con.getresponse();raw=r.read();retired=r.getheader('X-Watch-Retirement-MAC');self.assertEqual(r.status,410);self.assertIsNone(r.getheader('X-Watch-Receipt-MAC'));con.close()
  digest=hashlib.sha256(body).hexdigest();self.assertTrue(w.verify_retirement(SECRET,ch,digest,raw,retired));self.assertFalse(w.verify_receipt(SECRET,ch,digest,410,raw,retired));self.assertFalse(w.verify_retirement(SECRET,str(uuid.uuid4()),digest,raw,retired));self.assertFalse(w.verify_retirement(SECRET,ch,digest,raw+b' ',retired))
  value=json.loads(raw);self.assertTrue(value['retired']);self.assertNotIn('stored',value);self.assertEqual(value['deletion_id'],txid);self.assertEqual(value['source_id'],self.sha);self.assertEqual(list((self.root/'objects').iterdir()),[])
