import base64
import datetime as dt
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from _support import REPO, synthetic_audio
import receiver as r
import wire_v2 as w

# PUBLIC TEST FIXTURE SECRET (bytes 0..31). Never use it for a real pairing.
SECRET=base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')

class SealedV2(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=r.Store(self.root,minimum_free_bytes=0)
        self.server=r.Server(('127.0.0.1',0),self.store,SECRET);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.audio,duration=synthetic_audio('.m4a');self.sha=hashlib.sha256(self.audio).hexdigest();chunk=str(uuid.uuid4());self.path='/v2/chunks/'+chunk
        self.meta={'schema_version':1,'chunk_id':chunk,'session_id':str(uuid.uuid4()),'sequence':0,'capture_started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'duration_s':duration,'sha256':self.sha,'byte_count':len(self.audio),'original_name':chunk+'.m4a','sample_rate_hz':16000,'channels':1,'mime':'audio/mp4','is_test':True}
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,body,path,challenge):
        con=http.client.HTTPConnection(*self.server.server_address,timeout=5);con.request('PUT',path,body,{'Content-Type':'application/octet-stream','X-Watch-Challenge':challenge});response=con.getresponse();raw=response.read();mac=response.getheader('X-Watch-Receipt-MAC');status=response.status;con.close();return status,raw,mac
    def test_real_encrypted_http_and_fresh_attempt_idempotence(self):
        one=str(uuid.uuid4());body=w.seal(SECRET,self.meta,self.audio,self.path,one);status,raw,mac=self.request(body,self.path,one)
        self.assertEqual(status,201);self.assertTrue(w.verify_receipt(SECRET,one,hashlib.sha256(body).hexdigest(),status,raw,mac));receipt=json.loads(raw)
        self.assertEqual(receipt['schema_version'],2);self.assertEqual(receipt['protocol'],w.PROTOCOL);self.assertEqual(receipt['chunk_id'],self.meta['chunk_id'])
        two=str(uuid.uuid4());body2=w.seal(SECRET,self.meta,self.audio,self.path,two);status2,raw2,mac2=self.request(body2,self.path,two)
        self.assertEqual(status2,200);self.assertTrue(w.verify_receipt(SECRET,two,hashlib.sha256(body2).hexdigest(),status2,raw2,mac2));self.assertFalse(w.verify_receipt(SECRET,two,hashlib.sha256(body2).hexdigest(),status,raw,mac))
    def test_bitflip_wrong_key_path_and_challenge_rejected(self):
        challenge=str(uuid.uuid4());body=w.seal(SECRET,self.meta,self.audio,self.path,challenge)
        damaged=body[:-1]+bytes([body[-1]^1]);cases=[(damaged,self.path,challenge),(w.seal(base64.urlsafe_b64encode(b'x'*32).decode().rstrip('='),self.meta,self.audio,self.path,challenge),self.path,challenge),(body,'/v2/chunks/'+str(uuid.uuid4()),challenge),(body,self.path,str(uuid.uuid4()))]
        for payload,path,ch in cases:
            status,raw,mac=self.request(payload,path,ch);self.assertEqual(status,401);self.assertFalse(json.loads(raw)['stored'])
        self.assertEqual(list((self.root/'objects').iterdir()),[])
    def test_response_status_bytes_and_old_attempt_cannot_be_forged(self):
        ch=str(uuid.uuid4());body=w.seal(SECRET,self.meta,self.audio,self.path,ch);status,raw,mac=self.request(body,self.path,ch);digest=hashlib.sha256(body).hexdigest()
        self.assertFalse(w.verify_receipt(SECRET,ch,digest,200,raw,mac));self.assertFalse(w.verify_receipt(SECRET,ch,digest,status,raw+b' ',mac));self.assertFalse(w.verify_receipt(SECRET,str(uuid.uuid4()),digest,status,raw,mac))
    def test_production_plaintext_rejected(self):
        con=http.client.HTTPConnection(*self.server.server_address,timeout=5);con.request('PUT','/v1/chunks/'+self.sha,b'x',{'Content-Type':'audio/mp4'});response=con.getresponse();raw=json.loads(response.read());con.close();self.assertEqual(response.status,426);self.assertFalse(raw['stored'])
    def test_public_swift_fixture_interoperation(self):
        path=REPO/'apple/WatchMemory/Tests/Fixtures/wire-v2-public.json';v=json.loads(path.read_text());a=v['attempt'];body=base64.b64decode(v['sealed_base64']);meta,audio=w.unseal(v['configuration']['pairing_secret'],body,a['path'],a['challenge']);self.assertEqual(meta,a['expectedMetadata']);self.assertEqual(audio,base64.b64decode(v['expected_audio_base64']));self.assertTrue(w.verify_receipt(v['configuration']['pairing_secret'],a['challenge'],a['cipherSHA256'],v['status_code'],base64.b64decode(v['receipt_body_base64']),v['mac_base64']))

if __name__=='__main__':unittest.main()
