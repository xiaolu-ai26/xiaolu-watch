import base64
import datetime as dt
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import uuid

from _support import RECEIVER, synthetic_audio
spec=importlib.util.spec_from_file_location('receiver',RECEIVER/'receiver.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

class ChunkProtocol(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=r.Store(self.root)
        self.server=r.Server(('127.0.0.1',0),self.store,'fixture-only-not-real-token',allow_legacy_fixture=True);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.audio,self.duration=synthetic_audio('.caf');self.sha=hashlib.sha256(self.audio).hexdigest();self.meta={'schema_version':1,'chunk_id':str(uuid.uuid4()),'session_id':str(uuid.uuid4()),'sequence':0,'capture_started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'duration_s':self.duration,'sha256':self.sha,'byte_count':len(self.audio),'original_name':'','sample_rate_hz':16000,'channels':1,'is_test':True}
        self.meta['original_name']=self.meta['chunk_id']+'.caf'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def upload(self,meta=None,audio=None,sha=None,auth=True):
        meta=meta or self.meta;body=self.audio if audio is None else audio
        headers={'Content-Type':'audio/x-caf','X-Watch-Metadata':base64.b64encode(json.dumps(meta).encode()).decode()}
        if auth:headers['Authorization']='Bearer fixture-only-not-real-token'
        con=http.client.HTTPConnection(*self.server.server_address,timeout=5)
        con.request('PUT','/v1/chunks/'+(sha or self.sha),body,headers);response=con.getresponse();result=(response.status,json.loads(response.read()));con.close();return result
    def test_real_http_accept_and_idempotence(self):
        first=self.upload();second=self.upload()
        self.assertEqual(first[0],201);self.assertEqual(second[0],200);self.assertTrue(first[1]['stored']);self.assertEqual(first[1]['stored_at'],second[1]['stored_at'])
        self.assertEqual(len(list((self.root/'inbox').glob('*.ready'))),1)
        con=self.store.connect();self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0],1);con.close()
    def test_no_auth_no_commit(self):
        con=http.client.HTTPConnection(*self.server.server_address,timeout=5);con.request('GET','/health');response=con.getresponse();response.read();con.close();self.assertEqual(response.status,401);self.assertEqual(list((self.root/'objects').iterdir()),[])
    def test_wrong_hash_rejected(self):
        fake='0'*64;meta={**self.meta,'sha256':fake};self.assertEqual(self.upload(meta=meta,sha=fake)[0],422)
        self.assertEqual(list((self.root/'inbox').iterdir()),[])
    def test_conflicting_same_event_rejected(self):
        self.upload();meta={**self.meta,'duration_s':self.duration+0.01};self.assertEqual(self.upload(meta=meta)[0],409)
    def test_identical_audio_preserves_new_session_event(self):
        self.upload();meta={**self.meta,'chunk_id':str(uuid.uuid4()),'sequence':1};meta['original_name']=meta['chunk_id']+'.caf'
        self.assertEqual(self.upload(meta=meta)[0],201)
        con=self.store.connect();self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0],2);con.close()
        self.assertEqual(len(list((self.root/'inbox').glob('*.ready'))),1)
    def test_metadata_path_traversal_rejected(self):
        meta={**self.meta,'original_name':'../escape.caf'}
        with self.assertRaises(r.RequestError) as error:r.metadata(base64.b64encode(json.dumps(meta).encode()).decode(),self.sha,len(self.audio))
        self.assertEqual(error.exception.status,400)
    def test_wrong_audio_format_rejected(self):
        body=b'fake non CAF';sha=hashlib.sha256(body).hexdigest();meta={**self.meta,'sha256':sha,'byte_count':len(body)}
        self.assertEqual(self.upload(meta=meta,audio=body,sha=sha)[0],415)
    def test_truncated_upload_never_commits(self):
        declared=len(self.audio)+100;meta={**self.meta,'byte_count':declared};header=base64.b64encode(json.dumps(meta).encode()).decode()
        payload=(f'PUT /v1/chunks/{self.sha} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer fixture-only-not-real-token\r\nContent-Type: audio/x-caf\r\nX-Watch-Metadata: {header}\r\nContent-Length: {declared}\r\n\r\n').encode()
        con=socket.create_connection(self.server.server_address);con.sendall(payload+self.audio[:500]);con.shutdown(socket.SHUT_WR);response=b''
        while True:
            data=con.recv(4096)
            if not data:break
            response+=data
        con.close();self.assertIn(b'400',response.split(b'\r\n')[0]);self.assertEqual(list((self.root/'inbox').iterdir()),[])
        self.assertEqual(list((self.root/'tmp').iterdir()),[])
    def test_retry_repairs_missing_object_without_losing_receipt(self):
        first=self.upload();next((self.root/'objects').iterdir()).unlink()
        second=self.upload();self.assertEqual(second[0],200)
        self.assertEqual(first[1]['stored_at'],second[1]['stored_at']);self.assertTrue(next((self.root/'objects').iterdir()).is_file())
    def test_corrupt_custody_is_not_acknowledged(self):
        self.upload();next((self.root/'objects').iterdir()).write_bytes(b'corrupt stored bytes')
        self.assertEqual(self.upload()[0],409)
    def test_storage_backpressure_returns507(self):
        self.store.minimum_free_bytes=10**30
        status,result=self.upload();self.assertEqual(status,507);self.assertFalse(result['stored'])
        self.assertEqual(list((self.root/'objects').iterdir()),[])
    def test_uppercase_uuid_is_rejected(self):
        meta={**self.meta,'session_id':self.meta['session_id'].upper()}
        with self.assertRaises(r.RequestError) as error:r.metadata(base64.b64encode(json.dumps(meta).encode()).decode(),self.sha,len(self.audio))
        self.assertEqual(error.exception.status,400)
    def test_health_verifies_pairing_token(self):
        con=http.client.HTTPConnection(*self.server.server_address,timeout=5);con.request('GET','/health',headers={'Authorization':'Bearer wrong'});response=con.getresponse();response.read();self.assertEqual(response.status,401);con.close()
        con=http.client.HTTPConnection(*self.server.server_address,timeout=5);con.request('GET','/health',headers={'Authorization':'Bearer fixture-only-not-real-token'});response=con.getresponse();payload=json.loads(response.read());self.assertEqual(response.status,200);self.assertEqual(payload['protocol'],'watch-memory-chunks-v1');con.close()
    def test_private_bind_and_timezone(self):
        self.assertTrue(r.private_address('100.100.1.2'));self.assertFalse(r.private_address('0.0.0.0'));self.assertFalse(r.private_address('8.8.8.8'))
        meta={**self.meta,'capture_started_at':'2026-10-15T23:30:00Z'};out=r.metadata(base64.b64encode(json.dumps(meta).encode()).decode(),self.sha,len(self.audio));self.assertEqual(out['capture_date'],'2026-10-16')

if __name__=='__main__':unittest.main()
