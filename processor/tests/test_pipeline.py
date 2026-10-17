import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pipeline', Path(__file__).resolve().parents[1] / 'pipeline.py')
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)

class QueueSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        w.configure({'notes': {'dir': str(self.root / 'notes')}, 'feishu': {'enabled': False}, 'timezone': 'Asia/Shanghai'}, self.root)
        (self.root/'input').mkdir(); self.db=w.db_open()
    def tearDown(self):
        self.db.close(); self.temp.cleanup()
    def sample(self, name='a.wav'):
        # Pipeline queue logic only hashes bytes; no real or third-party audio is needed.
        f=self.root/'input'/name; f.write_bytes(b'synthetic test bytes, not audio'); os.utime(f,(time.time()-120,)*2); return f
    def test_partial_append_rejected(self):
        job={'source_id':'abc','capture_date':'2026-10-15','is_test':True}
        w.save(self.db,'daily:test:2026-10-15',{'lark_doc_id':'doc','append_pending':'abc'})
        with patch.object(w,'lark',return_value={'data':{'document':{'content':'source_id: abc'}}}):
            with self.assertRaisesRegex(RuntimeError,'Partial daily'):
                w.publish_daily('test',['x'],job,self.db,'key',self.root)
    def test_ambiguous_append_rejected(self):
        job={'source_id':'abc','capture_date':'2026-10-15','is_test':True}
        w.save(self.db,'daily:test:2026-10-15',{'lark_doc_id':'doc','append_pending':'abc'})
        with patch.object(w,'lark',return_value={'data':{'document':{'content':'nothing'}}}):
            with self.assertRaisesRegex(RuntimeError,'outcome unknown'):
                w.publish_daily('test',['x'],job,self.db,'key',self.root)
    def test_complete_append_recovery(self):
        job={'source_id':'abc','capture_date':'2026-10-15','is_test':True}
        w.save(self.db,'daily:test:2026-10-15',{'lark_doc_id':'doc','append_pending':'abc'})
        with patch.object(w,'lark',return_value={'data':{'document':{'content':'abc record_end: abc'}}}) as api:
            w.publish_daily('test',['x'],job,self.db,'key',self.root)
            self.assertEqual(api.call_count,1);self.assertEqual(job['lark_doc_id'],'doc')
    def test_done_scan_does_not_rehash_unchanged_history(self):
        f=self.sample()
        marker=Path(str(f)+'.ready');marker.write_text('{"capture_date":"2026-10-15"}')
        key='2026-10-15:abc';w.save(self.db,key,{'status':'done'})
        fp=w.fingerprint(f)+':'+w.fingerprint(marker)
        self.db.execute('INSERT INTO completed_inputs VALUES (?,?,?)',(str(f),fp,key));self.db.commit()
        with patch.object(w,'process') as proc:
            w.scan(self.db,self.root/'input');proc.assert_not_called()
    def test_app_no_speech_consumed_once_without_empty_documents(self):
        f=self.root/'input/empty.caf';f.write_bytes(b'fixture empty-audio result');os.utime(f,(time.time()-120,)*2)
        sha,_=w.hash_stable(f);key='2026-10-16:'+sha;app={'source_app':'watch-memory'}
        job={'source_id':sha,'capture_date':'2026-10-16','is_test':True,'source_url':None,'original_name':f.name,'capture_date_basis':'watch_app_capture_utc','remote_done':True,'attempts':0,'app_metadata':app}
        w.save(self.db,key,job);dest=self.root/'receipts/2026-10-16'/sha;dest.mkdir(parents=True)
        receipt={**job,'status':'done','segments':[]};(dest/'receipt.json').write_text(json.dumps(receipt));(dest/'transcript.json').write_text('{"segments":[]}')
        with patch.object(w,'lark') as api,contextlib.redirect_stdout(io.StringIO()):
            result=w.process(self.db,f,'2026-10-16',True,None,False,f.name,'watch_app_capture_utc',app)
            again=w.process(self.db,f,'2026-10-16',True,None,False,f.name,'watch_app_capture_utc',app)
        self.assertEqual(result['status'],'no_speech');self.assertEqual(again['attempts'],1);api.assert_not_called()
        self.assertIsNone(result['note_path'])
    def test_candidate_preserves_source_verbatim(self):
        segments=[{'start':1.23,'end':3.45,'text':'我们计划先做一个演示。'}, {'start':4,'end':5,'text':'天气很好。'}]
        self.assertEqual(w.original_candidates(segments),['[1.2–3.5] 我们计划先做一个演示。'])
    def test_unrelated_pending_blocks_append(self):
        job={'source_id':'bbb','capture_date':'2026-10-15','is_test':True}
        w.save(self.db,'daily:test:2026-10-15',{'lark_doc_id':'doc','append_pending':'aaa'})
        with patch.object(w,'lark',return_value={'data':{'document':{'content':'nothing'}}}):
            with self.assertRaisesRegex(RuntimeError,'outcome unknown'):
                w.publish_daily('test',['x'],job,self.db,'key',self.root)
    def test_cross_date_audio_identity_rejected(self):
        f=self.sample()
        sha,_=w.hash_stable(f)
        w.save(self.db,'2026-10-14:'+sha,{'source_id':sha,'capture_date':'2026-10-14'})
        with self.assertRaisesRegex(ValueError,'another capture_date'):
            w.process(self.db,f,'2026-10-15')

class EndToEndWithStubASR(unittest.TestCase):
    """receiver inbox marker -> local ASR queue (stubbed model) -> Markdown note + manifest."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        settings = {'notes': {'dir': str(self.root / 'outbox'), 'manifest': True}, 'feishu': {'enabled': False},
                    'timezone': 'Asia/Shanghai', 'asr': {'model': 'stub-model', 'device': 'cpu', 'compute_type': 'int8'}}
        w.configure(settings, self.root)
        self.inbox = self.root / 'receiver' / 'inbox'; self.inbox.mkdir(parents=True)
    def tearDown(self):
        self.temp.cleanup()
    def publish(self, segments):
        audio = self.inbox / 'x.m4a'; audio.write_bytes(b'synthetic bytes ' + str(segments).encode())
        sha = w._sha256(audio); final = self.inbox / (sha + '.m4a'); audio.rename(final)
        app = {'source_app': 'watch-memory', 'session_id': '00000000-0000-4000-8000-000000000002', 'sequence': 0,
               'chunk_id': '00000000-0000-4000-8000-000000000001'}
        (self.inbox / (sha + '.m4a.ready')).write_text(json.dumps({'capture_date': '2026-10-16', 'capture_date_basis': 'watch_app_capture_utc',
            'original_name': app['chunk_id'] + '.m4a', 'is_test': True, 'source_url': None, 'app_metadata': app}), encoding='utf-8')
        os.utime(final, (time.time() - 120,) * 2)
        return sha
    def run_scan(self, segments):
        with patch.object(w.asr_worker.Processor, 'transcribe', lambda _self, _src, _work: (60.0, segments)):
            db = w.db_open()
            try:
                self.assertEqual(w.scan(db), 0)   # first observation only
                self.assertEqual(w.scan(db), 0)   # stable -> processed
                w.write_manifest()
                return [json.loads(r[0]) for r in db.execute('SELECT payload FROM jobs')]
            finally:
                db.close()
    def test_speech_becomes_note_and_manifest(self):
        sha = self.publish('speech')
        jobs = self.run_scan([{'start': 0.0, 'end': 2.0, 'text': '我们计划明天整理笔记。', 'avg_logprob': -0.2, 'no_speech_prob': 0.1, 'compression_ratio': 1.1}])
        self.assertEqual(jobs[0]['status'], 'done')
        note = Path(jobs[0]['note_path'])
        self.assertEqual(note.parent, self.root / 'outbox' / '录音资料')
        text = note.read_text(encoding='utf-8')
        self.assertIn('我们计划明天整理笔记。', text); self.assertIn('record_end: ' + sha, text); self.assertIn('is_test: true', text)
        self.assertNotIn('stub-model/', text)
        manifest = json.loads((self.root / 'outbox' / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual([f['relative_path'] for f in manifest['files']], ['录音资料/' + note.name])
        self.assertTrue((self.root / 'receipts' / '2026-10-16' / sha / 'client-receipt.json').is_file())
        self.assertTrue((self.inbox / (sha + '.m4a')).is_file())  # receiver original retained
    def test_silence_is_consumed_without_empty_note(self):
        self.publish('silence')
        jobs = self.run_scan([])
        self.assertEqual(jobs[0]['status'], 'no_speech')
        self.assertFalse((self.root / 'outbox' / '录音资料').exists())

if __name__=='__main__':unittest.main()
