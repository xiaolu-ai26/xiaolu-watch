"""Queue semantics tests independent of CUDA; real ASR receipt is separate."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('asr_worker', Path(__file__).resolve().parents[1] / 'asr_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

class QueueTests(unittest.TestCase):
    def test_failure_retry_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('inbox', 'outbox', 'state', 'work'):
                (root / name).mkdir()
            source = root / 'inbox' / 'test.wav'
            source.write_bytes(b'isolated-test')
            source.with_suffix('.wav.json').write_text(json.dumps({'capture_date': '2026-10-14', 'original_name': '测试.wav'}))
            args = SimpleNamespace(root=root, model='test-local', language='zh', capture_date='2026-10-15')
            p = worker.Processor(args)
            calls = []
            def transcribe(_source, _workspace):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError('retry me')
                return 1.5, [{'start': 0, 'end': 1.5, 'text': '测试'}]
            p.transcribe = transcribe
            p.process(source)
            sha = worker.sha256(source)
            state = root / 'state' / (sha + '.json')
            self.assertEqual(json.loads(state.read_text())['status'], 'failed')
            self.assertTrue(source.exists())
            self.assertFalse((root / 'outbox' / sha / 'receipt.json').exists())
            p.process(source)
            receipt = root / 'outbox' / sha / 'receipt.json'
            data = json.loads(receipt.read_text())
            self.assertEqual((data['status'], data['attempts'], data['capture_date']), ('done', 2, '2026-10-14'))
            before = receipt.read_bytes()
            self.assertTrue((root / 'archive' / (sha + '.wav')).exists())
            source.write_bytes(b'isolated-test')
            source.with_suffix('.wav.json').write_text(json.dumps({'capture_date': '2026-10-14', 'original_name': '测试.wav'}))
            p.process(source)
            self.assertEqual(len(calls), 2)
            self.assertEqual(before, receipt.read_bytes())

    def test_mid_processing_mutation_not_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('inbox', 'outbox', 'state', 'work'):
                (root / name).mkdir()
            source = root / 'inbox' / 'test.wav'; source.write_bytes(b'one')
            original_sha = worker.sha256(source)
            p = worker.Processor(SimpleNamespace(root=root, model='test', language='zh', capture_date=None))
            def change(*args):
                source.write_bytes(b'two')
                return 1, []
            p.transcribe = change; p.process(source)
            state = json.loads((root / 'state' / (original_sha + '.json')).read_text())
            self.assertEqual(state['status'], 'failed')
            self.assertFalse((root / 'outbox' / original_sha / 'receipt.json').exists())


    def test_metadata_conflict_preserves_receipt_and_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('inbox', 'outbox', 'state', 'work'):
                (root / name).mkdir()
            source = root / 'inbox' / 'test.wav'; source.write_bytes(b'identity-test')
            p = worker.Processor(SimpleNamespace(root=root, model='test', language='zh', capture_date='2026-10-15'))
            p.transcribe = lambda *args: (1, [])
            p.process(source)
            sha = worker.sha256(root / 'archive' / (worker.hashlib.sha256(b'identity-test').hexdigest() + '.wav'))
            receipt = root / 'outbox' / sha / 'receipt.json'
            before = receipt.read_bytes()
            source.write_bytes(b'identity-test')
            source.with_suffix('.wav.json').write_text(json.dumps({'capture_date': '2026-10-14'}))
            p.process(source)
            self.assertTrue(source.exists())
            self.assertEqual(before, receipt.read_bytes())

    def test_bad_metadata_does_not_abort_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('inbox', 'outbox', 'state', 'work'):
                (root / name).mkdir()
            source = root / 'inbox' / 'test.wav'; source.write_bytes(b'bad-meta')
            source.with_suffix('.wav.json').write_text('[]')
            p = worker.Processor(SimpleNamespace(root=root, model='test', language='zh', capture_date='2026-10-15'))
            p.process(source)
            state = json.loads((root / 'state' / (worker.sha256(source) + '.json')).read_text())
            self.assertEqual(state['status'], 'failed')
            self.assertIn('JSON object', state['error'])

    def test_corrupt_archive_never_deletes_retransmission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'inbox').mkdir(); (root / 'archive').mkdir()
            source = root / 'inbox' / 'test.wav'; source.write_bytes(b'correct')
            sha = worker.sha256(source)
            (root / 'archive' / (sha + '.wav')).write_bytes(b'corrupted')
            p = worker.Processor(SimpleNamespace(root=root))
            with self.assertRaisesRegex(RuntimeError, 'hash mismatch'):
                p.archive(source, sha)
            self.assertEqual(source.read_bytes(), b'correct')

if __name__ == '__main__':
    unittest.main()

class RetirementInferenceTests(unittest.TestCase):
    def prepare(self, root):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'receiver'))
        from context_store import ContextStore
        for name in ('inbox','outbox','state','work'):(root/name).mkdir()
        source=root/'inbox/public-fixture.wav';source.write_bytes(b'public-synthetic-fixture')
        args=SimpleNamespace(root=root,model='test-local',language='zh',capture_date='2026-01-08',context_root=root/'context')
        return source,worker.Processor(args),ContextStore(root/'context')

    def test_slow_inference_releases_context_lock_and_retired_result_cannot_return(self):
        import threading
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source,processor,ledger=self.prepare(root);sid=worker.sha256(source)
            entered=threading.Event();release=threading.Event();errors=[]
            def infer(*args):
                entered.set();release.wait(3)
                return 1,[{'start':0,'end':1,'text':'公开合成内容'}]
            processor.transcribe=infer
            def run():
                try:
                    with worker.exclusive_lock(root):processor.process(source)
                except Exception as error:errors.append(type(error).__name__)
            thread=threading.Thread(target=run);thread.start();self.assertTrue(entered.wait(2))
            # ASR's worker OS lock remains held, but independent context/delete work proceeds.
            with ledger.generation_guard(timeout=.2):
                with ledger.connect() as db:
                    db.execute("UPDATE meta SET v=1 WHERE k='generation'")
                    db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,1)',(sid,'fixture','pending','[]'))
                (root/'state'/(sid+'.json')).unlink()  # Simulate backend retiring its ASR state.
            release.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
            self.assertFalse((root/'outbox'/sid/'receipt.json').exists())
            self.assertFalse((root/'state'/(sid+'.json')).exists())
            self.assertTrue(source.exists())

    def test_unrelated_generation_change_discards_then_live_source_retries(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source,processor,ledger=self.prepare(root);sid=worker.sha256(source)
            def infer(*args):
                with ledger.generation_guard(timeout=.2):
                    with ledger.connect() as db:db.execute("UPDATE meta SET v=1 WHERE k='generation'")
                return 1,[{'start':0,'end':1,'text':'公开合成内容'}]
            processor.transcribe=infer;processor.process(source)
            state=json.loads((root/'state'/(sid+'.json')).read_text())
            self.assertEqual(state['status'],'failed');self.assertEqual(state['segments'],[])
            self.assertFalse((root/'outbox'/sid/'receipt.json').exists());self.assertTrue(source.exists())
            processor.transcribe=lambda *args:(1,[{'start':0,'end':1,'text':'公开合成内容'}])
            processor.process(source)
            self.assertEqual(json.loads((root/'outbox'/sid/'receipt.json').read_text())['status'],'done')

    def test_inference_error_after_retirement_does_not_recreate_state(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source,processor,ledger=self.prepare(root);sid=worker.sha256(source)
            def infer(*args):
                with ledger.generation_guard(timeout=.2):
                    with ledger.connect() as db:
                        db.execute("UPDATE meta SET v=1 WHERE k='generation'")
                        db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,1)',(sid,'fixture','pending','[]'))
                    (root/'state'/(sid+'.json')).unlink()
                raise RuntimeError('synthetic inference failure')
            processor.transcribe=infer;processor.process(source)
            self.assertFalse((root/'state'/(sid+'.json')).exists())
            self.assertFalse((root/'outbox'/sid).exists())
