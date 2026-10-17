import importlib.util
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from speaker_identity.core import cosine,decide,validate_profile,windows
from speaker_identity.enrollment import coverage
from speaker_identity.runtime import analyze
spec=importlib.util.spec_from_file_location('speaker_worker_test',Path(__file__).resolve().parents[1]/'speaker_worker.py');worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
T={'self_min':.75,'other_max':.3,'consistency_min':.6}
class Decisions(unittest.TestCase):
    def test_three_states_and_unstable_mix(self):
        self.assertEqual(decide(.85,[.83,.84],.9,4,True,T)[0],'self')
        self.assertEqual(decide(.1,[.15,.14],.9,4,True,T)[0],'other')
        self.assertEqual(decide(.55,[.5,.52],.9,4,True,T)[0],'unknown')
        self.assertEqual(decide(.85,[.8,.2],.3,4,True,T)[0],'unknown')
    def test_short_overlap_silence_never_owner(self):
        for seconds,quality,overlap in [(2,True,False),(4,False,False),(4,True,True)]:
            self.assertEqual(decide(.99,[.99,.99],1,seconds,quality,T,overlap)[0],'unknown')
        self.assertEqual(decide(float('nan'),[.9,.9],.8,4,True,T)[0],'unknown')
    def test_no_cluster_owner_and_profile_binding(self):
        p={'version':1,'profile_version':'v','model_sha256':'a','centroid':[1,0],'thresholds':T,'calibration_ready':True,'enrollment_authorization':'user_confirmed_seed'}
        validate_profile(p,'a',2)
        with self.assertRaises(ValueError):validate_profile(p,'b',2)
        p['enrollment_authorization']='largest_cluster'
        with self.assertRaises(ValueError):validate_profile(p,'a',2)
    def test_window_and_speech_coverage(self):
        w=windows(4,39);self.assertAlmostEqual(w[0][0],4);self.assertAlmostEqual(w[-1][1],39)
        self.assertTrue(all(3<=b-a<=6 for a,b in w))
        self.assertAlmostEqual(coverage(1,5,[{'start':0,'end':3},{'start':2,'end':6}]),1)
        with self.assertRaises(ValueError):cosine([0,0],[1,0])

class SourceBinding(unittest.TestCase):
    def test_actual_span_label_and_receipt_binding_no_vector_export(self):
        import numpy as np
        class Fake:
            dim=2;model_sha256='m';provider='cuda';cuda_node_count=1;cuda_network_node_count=1
            def audio(self,path):return np.ones(10*16000,dtype=np.float32)*.05
            def embedding(self,audio):return [1,0]
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);audio=p/'source.wav';audio.write_bytes(b'fixture');sha=hashlib.sha256(audio.read_bytes()).hexdigest();r=p/'receipt.json';r.write_text(json.dumps({'source_id':sha,'status':'done','segments':[{'start':0,'end':6},{'start':6,'end':8}]}))
            profile={'version':1,'profile_version':'v','model_id':'model','model_revision':'commit','model_sha256':'m','centroid':[1,0],'thresholds':T,'calibration_ready':True,'enrollment_authorization':'user_confirmed_seed'}
            result=analyze(Fake(),profile,audio,r,'model','commit')
            self.assertEqual(result['asr_receipt_sha256'],hashlib.sha256(r.read_bytes()).hexdigest())
            self.assertEqual([x['label'] for x in result['turns']],['self','unknown'])
            self.assertNotIn('centroid',json.dumps(result));self.assertNotIn('fixture',json.dumps(result))
            r.write_text(json.dumps({'source_id':'0'*64,'status':'done','segments':[]}))
            with self.assertRaises(ValueError):analyze(Fake(),profile,audio,r,'model','commit')
    def test_pre_cutoff_never_analyzed_or_reimported(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);sessions=p/'sessions/s';sessions.mkdir(parents=True);(sessions/'old.json').write_text(json.dumps({'metadata':{'is_test':False,'capture_started_at':'2026-01-01T00:00:00Z'}}));profile=p/'profile.json';profile.write_text('{"profile_version":"v","model_id":"m","model_revision":"r"}')
            w=worker.Worker({'capture_since':'2026-10-17T17:53:05Z','state_dir':str(p/'state'),'profile_path':str(profile),'profile_version':'v','receiver_sessions':str(p/'sessions'),'model_id':'m','model_revision':'r','provider':'cuda'})
            with patch.object(w,'get_extractor',side_effect=AssertionError('old source must not load GPU')):
                result=w.tick()
            self.assertEqual(result['excluded_pre_cutoff'],1);self.assertEqual(result['completed_this_tick'],0)
class CUDAAttestation(unittest.TestCase):
    def test_cuda_copy_or_add_does_not_unlock_future_embeddings(self):
        import types,numpy as np
        from speaker_identity.runtime import Extractor
        class Fbank:
            num_frames_ready=100
            def __init__(self,options):pass
            def accept_waveform(self,*a):pass
            def input_finished(self):pass
            def get_frame(self,i):return [0]*80
        knf=types.SimpleNamespace(FbankOptions=lambda:types.SimpleNamespace(frame_opts=types.SimpleNamespace(),mel_opts=types.SimpleNamespace()),OnlineFbank=Fbank)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'profile.json';path.write_text(json.dumps([{'args':{'provider':'CUDAExecutionProvider','op_name':'Add'}},{'args':{'provider':'CUDAExecutionProvider','op_name':'MemcpyToHost'}}]))
            session=types.SimpleNamespace(run=lambda *a:[np.array([[1,0]])],get_inputs=lambda:[types.SimpleNamespace(name='feats')],end_profiling=lambda:str(path))
            obj=Extractor.__new__(Extractor);obj.provider='cuda';obj.cuda_attestation_failed=False;obj.cuda_node_count=0;obj.cuda_network_node_count=0;obj.session=session
            with patch.dict(sys.modules,{'kaldi_native_fbank':knf}):
                with self.assertRaises(RuntimeError):obj.embedding(np.ones(48000,dtype=np.float32))
                self.assertTrue(obj.cuda_attestation_failed)
                with self.assertRaises(RuntimeError):obj.embedding(np.ones(48000,dtype=np.float32))

class SnapshotBinding(unittest.TestCase):
    def test_profile_parse_and_hash_are_one_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);old=json.dumps({'profile_version':'v','model_id':'m','model_revision':'r','centroid':[1,0]}).encode();new=old.replace(b'[1, 0]',b'[0, 1]')
            profile=p/'profile.json';profile.write_bytes(new)
            w=worker.Worker({'capture_since':'2026-10-17T17:53:05Z','state_dir':str(p/'state'),'profile_path':str(profile),'profile_version':'v','profile_sha256':hashlib.sha256(new).hexdigest(),'model_id':'m','model_revision':'r'})
            with patch.object(worker,'snapshot',return_value=old),patch.object(w,'get_extractor',side_effect=AssertionError('mismatched snapshot cannot infer')):
                with self.assertRaisesRegex(ValueError,'profile bytes'):w.tick()
    def test_enrollment_receipt_change_refuses_profile_commit(self):
        import numpy as np
        import speaker_identity.enrollment as enrollment
        class Fake:
            model_sha256='m'
            def audio(self,path):return np.ones(20*16000,dtype=np.float32)*.05
            def embedding(self,a):return [1,0]
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);source=p/'a.wav';source.write_bytes(b'fixture');sha=hashlib.sha256(source.read_bytes()).hexdigest();receipt=p/'receipt.json';raw=json.dumps({'source_id':sha,'status':'done','segments':[{'start':0,'end':20}]}).encode();receipt.write_bytes(raw)
            plan={'authorization':'user_confirmed_seed','source_path':str(source),'source_id':sha,'asr_receipt':str(receipt),'enrollment_windows':[[0,3],[3,6],[6,9]],'holdout_windows':[[9,12],[12,15]]}
            target=p/'profile.json'
            with patch.object(enrollment,'snapshot',side_effect=[raw,b'changed']):
                with self.assertRaisesRegex(ValueError,'ASR receipt changed'):enrollment.enroll(Fake(),plan,target,'m','r')
            self.assertFalse(target.exists())

class GPUSharing(unittest.TestCase):
    def test_shared_lock_is_nonblocking_and_released(self):
        from speaker_identity.resources import gpu_slot
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'worker.lock'
            with gpu_slot(path) as first:
                self.assertTrue(first)
                with gpu_slot(path) as second:self.assertFalse(second)
            with gpu_slot(path) as third:self.assertTrue(third)
    def test_eligible_asr_has_priority_until_receipt_done(self):
        import os,time
        from speaker_identity.resources import asr_ready_first
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);inbox=p/'inbox';inbox.mkdir();sha='a'*64;audio=inbox/(sha+'.m4a');audio.write_bytes(b'fixture');(inbox/(sha+'.m4a.ready')).write_text('{}');os.utime(audio,(time.time()-60,)*2)
            cfg={'asr_receiver_inbox':str(inbox),'asr_outbox':str(p/'outbox')}
            self.assertTrue(asr_ready_first(cfg))
            r=p/'outbox'/sha/'receipt.json';r.parent.mkdir(parents=True);r.write_text('{"status":"done"}');self.assertFalse(asr_ready_first(cfg))

if __name__=='__main__':unittest.main()
