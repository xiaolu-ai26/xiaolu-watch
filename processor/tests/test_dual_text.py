import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from text_output.core import Source, batch_identity, discover, next_batch, validate_output
from text_output.lark_output import Lark, document_blocks, expected
from text_output.provider import DeepSeek
from text_output.store import State, TokenBudgetExceeded, atomic
from text_worker import Worker


def source(n, text='我可能下周再试，唔系今日决定。', captured=100):
    return Source(hashlib.sha256(str(n).encode()).hexdigest(), 'test-session', n, '2026-10-18', captured + n * 60, 60, text, hashlib.sha256(text.encode()).hexdigest())


class FakeLark:
    def __init__(self, home):
        self.home = home; self.items = []; self.ids = []; self.next_id = 0; self.writes = 0; self.fail_after_insert = False

    def command(self, args):
        op = args[0]
        if op == '+script' and args[2] == 'init-draft':
            p = self.home / 'draft_test'; p.mkdir(exist_ok=True)
            return {'ok': True, 'data': {'workspace': 'draft_test', 'draft_path': 'draft_test/doc.xml'}}
        if op == '+script':
            return {'ok': True, 'data': {'assessment': {'status': 'pass'}}}
        if op == '+fetch':
            while len(self.ids) < len(self.items):
                self.ids.append('id'+str(self.next_id)); self.next_id += 1
            content = ''.join(f'<{tag} id="{self.ids[i]}">{text}</{tag}>' for i, (tag, text) in enumerate(self.items))
            return {'ok': True, 'data': {'document': {'content': content}}}
        if op == '+update' and args[args.index('--command')+1] == 'block_delete':
            index = self.ids.index(args[args.index('--block-id')+1]); self.items.pop(index); self.ids.pop(index); self.writes += 1
            if self.fail_after_insert:
                self.fail_after_insert = False
                raise RuntimeError('simulated delete response lost after server commit')
            return {'ok':True,'data':{'result':'success','warnings':[]}}
        path = self.home / args[args.index('--content') + 1][3:]
        parsed = document_blocks(path.read_text())
        insert = [(x['tag'], x['text']) for x in parsed]
        self.writes += 1
        if op == '+create':
            self.items = insert
            self.ids = ['id'+str(i) for i in range(self.next_id, self.next_id+len(insert))]; self.next_id += len(insert)
            return {'ok': True, 'data': {'document': {'document_id': 'fake-doc', 'url': 'https://example.invalid/doc'}, 'warnings': []}}
        index = self.ids.index(args[args.index('--block-id') + 1]) + 1
        self.items[index:index] = insert
        self.ids[index:index] = ['id'+str(i) for i in range(self.next_id, self.next_id+len(insert))]; self.next_id += len(insert)
        if self.fail_after_insert:
            self.fail_after_insert = False
            raise RuntimeError('simulated response lost after server commit')
        return {'ok': True, 'data': {'result': 'success', 'warnings': []}}


class CoreTests(unittest.TestCase):
    def test_batch_tail_gap_and_source_boundary(self):
        slots = {i: source(i) for i in range(6)}
        self.assertEqual([s.sequence for s in next_batch(slots, set(), 1000)], list(range(5)))
        self.assertEqual(next_batch({0: source(0)}, set(), 200), [])
        self.assertEqual(len(next_batch({0: source(0)}, set(), 400)), 1)
        self.assertEqual(next_batch({0: None, 1: source(1)}, set(), 1000), [])
        self.assertEqual(next_batch({1: source(1)}, set(), 1000), [])
        self.assertEqual(len(next_batch({10: source(10)}, set(), 2000, floor=10)), 1)
        self.assertEqual(next_batch({0: source(0), 2: source(2)}, {0}, 1000), [])
        self.assertNotEqual(batch_identity([source(0)]), batch_identity([source(0, '另一句')]))

    def test_no_summary_technical_or_todo(self):
        good = {'clean': '我可能下周再试，唔系今日决定。', 'distilled': '## 试验安排\n\n我可能下周再试，唔系今日决定。', 'uncertain_terms': []}
        self.assertEqual(validate_output(good, good['clean']), good)
        for text in ['# 一级标题', '- [ ] 自动待办', 'source_id: secret', '## 核心要点\n\n三句话']:
            with self.assertRaises(ValueError):
                validate_output(dict(good, distilled=text), good['clean'])
        # No percentage minimum that would force filler retention. Semantic fidelity is reviewed separately.
        self.assertEqual(validate_output(good, '嗯然后就是'*100+good['clean']),good)
        with self.assertRaises(ValueError):validate_output(dict(good,distilled='## 待确认说法\n\n**半年**'),good['clean'])
        with self.assertRaises(ValueError):validate_output(dict(good,distilled='只有三个自然段，没有主题标题。'),good['clean'])

    def test_budget_admission_counts_failure_and_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'state.db'; s = State(path)
            s.admit('day', 80, 100); s.close(); s = State(path)
            with self.assertRaises(RuntimeError):
                s.admit('day', 30, 100)
            self.assertEqual(s.db.execute('select sum(reserved) from admissions').fetchone()[0], 80)
            s.close()

    def test_provider_truncated_response_never_used(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *_):
                return json.dumps({'model': 'deepseek-flash', 'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}], 'usage': {'total_tokens': 10}}).encode()
        class Opener:
            def open(self, *_args, **_kw): return Response()
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / 'key'; key.write_text('synthetic-not-a-real-key')
            s = State(Path(td) / 'db')
            p = DeepSeek({'provider': 'deepseek', 'model': 'deepseek-flash', 'api_key_file': str(key)}, s)
            with patch('urllib.request.build_opener', return_value=Opener()):
                with self.assertRaises(ValueError): p.edit([source(0)])
            self.assertEqual(s.db.execute('select count(*) from admissions').fetchone()[0], 1)
            self.assertEqual(json.loads(s.db.execute('select usage from admissions').fetchone()[0])['response_metadata']['finish_reason'], 'length')
            s.close()

    def test_thinking_low_is_bounded_and_usage_includes_reasoning(self):
        captured = {}
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*_):return json.dumps({'model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':json.dumps({'clean':'我可能再试。','distilled':'## 尝试\n\n我可能再试。','uncertain_terms':[]})}}],'usage':{'prompt_tokens':100,'completion_tokens':120,'completion_tokens_details':{'reasoning_tokens':90}}}).encode()
        class Opener:
            def open(self,request,**kw):captured.update(json.loads(request.data));return Response()
        with tempfile.TemporaryDirectory() as td:
            key=Path(td)/'key';key.write_text('synthetic-not-real-key');state=State(Path(td)/'db')
            provider=DeepSeek({'provider':'deepseek','model':'deepseek-flash','api_key_file':str(key),'thinking_effort':'low','max_output_tokens':10000},state)
            with patch('urllib.request.build_opener',return_value=Opener()):output,proof=provider.edit([source(0)])
            self.assertEqual(captured['thinking'],{'type':'enabled'});self.assertEqual(captured['reasoning_effort'],'low');self.assertEqual(captured['max_tokens'],8000);self.assertLessEqual(captured['max_tokens'],10000)
            usage=json.loads(state.db.execute('select usage from admissions').fetchone()[0]);self.assertEqual(usage['completion_tokens'],120);self.assertEqual(usage['completion_tokens_details']['reasoning_tokens'],90);state.close()

    def test_default_high_and_official_medium_alias_have_bounded_reservation(self):
        requests=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,*_):return json.dumps({'model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':json.dumps({'clean':'可能再试。','distilled':'## 尝试\n\n可能再试。','uncertain_terms':[]})}}],'usage':{'prompt_tokens':50,'completion_tokens':100}}).encode()
        class Opener:
            def open(self,r,**_):requests.append(json.loads(r.data));return Response()
        with tempfile.TemporaryDirectory() as td:
            key=Path(td)/'key';key.write_text('synthetic-not-real');state=State(Path(td)/'db')
            for configured in [None,'medium','high','max']:
                config={'provider':'deepseek','model':'deepseek-flash','api_key_file':str(key)}
                if configured is not None:config['thinking_effort']=configured
                with patch('urllib.request.build_opener',return_value=Opener()):_,proof=DeepSeek(config,state).edit([source(0)])
                effective='max' if configured=='max' else 'high'
                self.assertEqual(requests[-1]['reasoning_effort'],effective);self.assertEqual(proof['effective_effort'],effective)
                self.assertGreaterEqual(requests[-1]['max_tokens'],24000 if effective=='max' else 16000);self.assertLessEqual(requests[-1]['max_tokens'],32768)
            self.assertEqual(state.db.execute('select count(*) from admissions').fetchone()[0],4);state.close()

    def test_rejected_editor_json_is_private_with_fixed_reason(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,*_):return json.dumps({'model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':json.dumps({'clean':'','distilled':'','uncertain_terms':[]})}}],'usage':{'total_tokens':100}}).encode()
        class Opener:
            def open(self,*_,**__):return Response()
        with tempfile.TemporaryDirectory() as td:
            key=Path(td)/'key';key.write_text('synthetic-not-real-key');state=State(Path(td)/'db');provider=DeepSeek({'provider':'deepseek','model':'deepseek-flash','api_key_file':str(key)},state)
            with patch('urllib.request.build_opener',return_value=Opener()):
                with self.assertRaises(ValueError) as caught:provider.edit([source(0)])
            self.assertEqual(caught.exception.code,'editor_empty_or_oversized');saved=state.items('rejected:')[0][1]
            self.assertEqual(saved['model_output']['clean'],'');self.assertEqual(saved['reason'],'editor_empty_or_oversized');self.assertNotIn('reasoning_content',saved);state.close()


class LarkTests(unittest.TestCase):
    def test_unknown_append_recovers_without_duplicate_or_body_marker(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td); state = State(home / 'db'); fake = FakeLark(home); p = Lark(state, home, fake.command)
            first = {'id': 'one', 'clean': '第一句话。', 'distilled': '## 第一主题\n\n第一句话。'}
            second = {'id': 'two', 'clean': '第二句话。', 'distilled': '## 第二主题\n\n第二句话。'}
            p.append('2026-10-18', first)
            fake.fail_after_insert = True
            with self.assertRaises(RuntimeError): p.append('2026-10-18', second)
            p.append('2026-10-18', second)
            self.assertEqual(sum(t == '第二句话。' for _, t in fake.items), 2)
            self.assertEqual(fake.writes, 3)  # create, clean, distilled; failed clean response not repeated
            self.assertNotIn('two', ''.join(t for _, t in fake.items))
            fake.items.append(('p', '真人后来写的文字'))
            with self.assertRaises(RuntimeError): p.append('2026-10-18', first)
            self.assertEqual(fake.writes, 3); state.close()

    def test_unknown_create_with_saved_response_and_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td); state = State(home / 'db'); fake = FakeLark(home); p = Lark(state, home, fake.command)
            items = [('title', '2026-10-18 录音双稿'), ('h1', '口语清稿'), ('p', '原话'), ('h1', '七删一改整理稿'), ('p', '原话')]
            fake.items = items
            state.put('doc:2026-10-18', {'create_pending': True, 'expected': expected(items), 'batch_ids': ['one']})
            with self.assertRaises(RuntimeError): p.ensure('2026-10-18', {'id': 'one'})
            p.recover('2026-10-18', 'fake-doc')
            self.assertEqual(fake.writes, 0); self.assertFalse(state.get('doc:2026-10-18')['create_pending']); state.close()

    def test_unapplied_append_requires_explicit_unchanged_base_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td); state = State(home / 'db'); fake = FakeLark(home); p = Lark(state, home, fake.command)
            first = {'id': 'one', 'clean': '第一句话。', 'distilled': '## 第一主题\n\n第一句话。'}
            p.append('2026-10-18', first)
            day = state.get('doc:2026-10-18'); base = day['expected']
            day.update(expected=base + [['p', '新原话']], pending={'base': base, 'done_key': 'half:test'})
            state.put('doc:2026-10-18', day)
            with self.assertRaises(RuntimeError): p.recover_append('2026-10-18')
            p.recover_append('2026-10-18', True)
            self.assertIsNone(state.get('doc:2026-10-18')['pending'])
            day.update(expected=base + [['p', '新原话']], pending={'base': base})
            state.put('doc:2026-10-18', day); fake.items.append(('p', '人工内容'))
            with self.assertRaises(RuntimeError): p.recover_append('2026-10-18', True)
            state.close()


class WorkerTests(unittest.TestCase):
    def setup_fixture(self, root, text='嗯，我可能下周再试，唔系今日决定。'):
        for n in range(5):
            s = source(n, text)
            atomic(root / 'sessions/test-session' / f'{n}.json', {'metadata': {
                'is_test': False, 'sha256': s.source_id, 'session_id': s.session, 'sequence': n,
                'capture_date': s.date, 'capture_started_at': f'2026-10-18T00:0{n}:00+00:00', 'duration_s': 60, 'original_name': f'{n}.m4a'}})
            atomic(root / 'asr' / s.source_id / 'receipt.json', {'source_id': s.source_id, 'is_test': False,
                'capture_date': s.date, 'status': 'done', 'segments': [{'text': text}] if text else []})
        return {'version': 1, 'enabled': True, 'home': str(root / 'private'), 'notes_outbox': str(root / 'notes'),
                'asr_outbox': str(root / 'asr'), 'receiver_sessions': str(root / 'sessions'), 'feishu': False}

    def test_cached_output_cold_restart_and_human_preserved(self):
        class Provider:
            calls = 0
            def edit(self, rows, context):
                self.calls += 1
                return {'clean': '原话' * 5, 'distilled': '## 安排\n\n原话' * 5, 'uncertain_terms': []}, {'model': 'fake'}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); config = self.setup_fixture(root); p = Provider(); w = Worker(config, p)
            w.tick(2e9); w.state.close(); w = Worker(config, p); w.tick(2e9)
            self.assertEqual(p.calls, 1)
            note = root / 'notes/口语清稿/2026-10-18_口语清稿.md'; note.write_text('人工修改')
            with self.assertRaises(RuntimeError): w.export('2026-10-18')
            self.assertEqual(note.read_text(), '人工修改'); w.state.close()

    def test_no_speech_makes_no_notes_or_calls(self):
        class Provider:
            def edit(self, *_): raise AssertionError('must not call for silence')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); w = Worker(self.setup_fixture(root, ''), Provider()); w.tick(2e9)
            self.assertEqual(list((root/'notes').rglob('*.md')),[]);self.assertEqual(json.loads((root/'notes/manifest.json').read_text())['files'],[]);w.state.close()

    def test_cutoff_retires_old_missing_asr_and_does_not_reexport_cached_old_batches(self):
        class Provider:
            seen=[]
            def edit(self,rows,context):
                self.seen.append([r.sequence for r in rows]);text='\n'.join(r.text for r in rows)
                return {'clean':text,'distilled':'## 本批内容\n\n'+text,'uncertain_terms':[]},{'model':'fake'}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config=self.setup_fixture(root);provider=Provider();provider.seen=[]
            for n in range(5):
                q=root/'asr'/source(n).source_id/'receipt.json';v=json.loads(q.read_text());v['segments']=[{'text':'过去信息' if n<3 else '新信息'}];atomic(q,v)
            worker=Worker(config,provider);worker.tick(2e9);worker.state.close()
            # Cleanup removes exported test files, but keeps private historical state for recovery.
            for q in (root/'notes').rglob('*.md'):q.unlink()
            (root/'notes/manifest.json').unlink()
            for n in range(3):(root/'asr'/source(n).source_id/'receipt.json').unlink()
            config['capture_since']='2026-10-18T00:03:00Z';worker=Worker(config,provider);worker.tick(2e9)
            self.assertEqual(provider.seen,[[0,1,2,3,4],[3,4]])
            note=(root/'notes/口语清稿/2026-10-18_口语清稿.md').read_text();self.assertNotIn('过去信息',note);self.assertIn('新信息',note);worker.state.close()
            # Advancing beyond every capture must not resurrect those saved outputs on a later restart.
            for q in (root/'notes').rglob('*.md'):q.unlink()
            config['capture_since']='2026-10-19T00:00:00Z';worker=Worker(config,provider);worker.tick(2e9)
            self.assertEqual(list((root/'notes').rglob('*.md')),[]);self.assertEqual(json.loads((root/'notes/manifest.json').read_text())['files'],[]);self.assertEqual(len(provider.seen),2);worker.state.close()

    def test_cutoff_checks_every_source_and_sparse_retirement_keeps_new_sources(self):
        class Provider:
            seen=[]
            def edit(self,rows,context):
                self.seen.append([r.sequence for r in rows]);text='\n'.join(r.text for r in rows)
                return {'clean':text,'distilled':'## 本批内容\n\n'+text,'uncertain_terms':[]},{'model':'fake'}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config=self.setup_fixture(root);provider=Provider();provider.seen=[]
            for n,minute in [(0,3),(1,1),(2,4),(3,5),(4,6)]:
                q=root/'sessions/test-session'/f'{n}.json';v=json.loads(q.read_text());v['metadata']['capture_started_at']=f'2026-10-18T00:0{minute}:00Z';atomic(q,v)
                q=root/'asr'/source(n).source_id/'receipt.json';v=json.loads(q.read_text());v['segments']=[{'text':'旧信息' if n==1 else '新信息'}];atomic(q,v)
            worker=Worker(config,provider);worker.tick(2e9);worker.state.close()
            for q in (root/'notes').rglob('*.md'):q.unlink()
            (root/'asr'/source(1).source_id/'receipt.json').unlink()
            config['capture_since']='2026-10-18T00:03:00Z';worker=Worker(config,provider)
            prior=worker.state.items('batch:')[0][1]
            self.assertFalse(worker._eligible(prior))  # First source is new, but a later one is old.
            missing_times=dict(prior,sources=[{k:v for k,v in x.items() if k!='captured'} for x in prior['sources']]);self.assertFalse(worker._eligible(missing_times))
            worker.tick(2e9);self.assertEqual(provider.seen,[[0,1,2,3,4],[0,2,3,4]])
            note=(root/'notes/口语清稿/2026-10-18_口语清稿.md').read_text();self.assertNotIn('旧信息',note);self.assertIn('新信息',note);worker.state.close()

    def test_manifest_never_advertises_old_and_new_mixed_body_before_export(self):
        from text_output.core import daily_markdown
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();config=self.setup_fixture(root);config['capture_since']='2026-10-18T00:00:00Z';worker=Worker(config,object())
            old=source(0,'旧内容',100);new=source(1,'新内容',2000000000)
            old_output={'clean':'旧内容','distilled':'## 旧主题\n\n旧内容'};new_output={'clean':'新内容','distilled':'## 新主题\n\n新内容'}
            for label,row,output in [('old',old,old_output),('new',new,new_output)]:worker.state.put('batch:'+label,{'id':label,'date':row.date,'session':row.session,'sequence':row.sequence,'captured':row.captured,'sources':[row.identity()],'output':output})
            for kind,directory in [('clean','口语清稿'),('distilled','七删一改整理稿')]:
                path=root/'notes'/directory/('2026-10-18_'+directory+'.md');body=daily_markdown('2026-10-18',kind,[old_output,new_output]);atomic(path,body);worker.state.put('note:'+str(path),hashlib.sha256(path.read_bytes()).hexdigest())
            worker.manifest();self.assertEqual(json.loads((root/'notes/manifest.json').read_text())['files'],[])
            worker.export('2026-10-18');manifest=json.loads((root/'notes/manifest.json').read_text());self.assertEqual(len(manifest['files']),2)
            for item in manifest['files']:
                path=root/'notes'/item['relative_path'];self.assertNotIn('旧内容',path.read_text());self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),item['sha256'])
            worker.state.close()

    def test_derived_asr_selected_with_model_proof_original_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.setup_fixture(root)
            original=root/'asr'/source(0).source_id/'receipt.json';before=original.read_bytes()
            value=json.loads(before);value.update(model='local-large-v3',original_name='0.m4a',capture_date_basis='watch_app_capture_utc',source_url=None,audio_duration_s=60,segments=[{'text':'Obsidian 记笔记。'},{'text':'我可能再试。'}])
            derived=root/'derived'/source(0).source_id/'receipt.json';atomic(derived,value)
            groups=discover(root/'asr',root/'sessions',derived_asr_root=root/'derived',derived_asr_model='local-large-v3')
            row=groups[('2026-10-18','test-session')][0]
            self.assertEqual(row.text,'Obsidian 记笔记。\n我可能再试。');self.assertEqual(row.asr_model,'local-large-v3')
            self.assertEqual(row.receipt_hash,hashlib.sha256(derived.read_bytes()).hexdigest());self.assertEqual(original.read_bytes(),before)
            for bad in [{'model':''},{'model':'turbo'},{'original_name':'other.m4a'},{'capture_date_basis':'receive_date_unconfirmed'},{'source_url':'https://example.invalid'},{'audio_duration_s':5}]:
                atomic(derived,dict(value,**bad))
                with self.assertRaises(ValueError):discover(root/'asr',root/'sessions',derived_asr_root=root/'derived',derived_asr_model='local-large-v3')

    def test_crash_after_cloud_publish_recovers_notes_without_new_input(self):
        class Provider:
            calls = 0
            def edit(self, rows, context):
                self.calls += 1
                return {'clean': '原话', 'distilled': '## 安排\n\n原话', 'uncertain_terms': []}, {'model': 'fake'}
        class Publisher:
            calls = 0
            def append(self, date, batch): self.calls += 1; return {'id': 'fake'}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); config = self.setup_fixture(root); config['feishu'] = True
            p = Provider(); cloud = Publisher(); w = Worker(config, p, cloud)
            w.tick(2e9)
            self.assertTrue(w.state.items('batch:')[0][1]['published'])
            # Published=True is durable, but the local artifacts are missing after a crash.
            for note in (root/'notes').rglob('*.md'):note.unlink()
            (root/'notes/manifest.json').unlink()
            w.state.close(); w = Worker(config, p, cloud); w.tick(2e9)
            self.assertTrue((root / 'notes/manifest.json').exists())
            self.assertEqual(p.calls, 1); self.assertEqual(cloud.calls, 1); w.state.close()

    def test_cloud_unknown_never_blocks_existing_local_export(self):
        class Provider:
            calls = 0
            def edit(self, rows, context):
                self.calls += 1
                return {'clean': '原话', 'distilled': '## 安排\n\n原话', 'uncertain_terms': []}, {'model': 'fake'}
        class Publisher:
            def append(self, *_): raise RuntimeError('simulated cloud unavailable')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config=self.setup_fixture(root);config['feishu']=True
            p=Provider();w=Worker(config,p,Publisher())
            self.assertTrue(w.tick(2e9)['sink_conflicts'])
            self.assertTrue((root/'notes/manifest.json').exists())
            # Simulate crash/loss of the last local result; cloud still unavailable on restart.
            for note in (root/'notes').rglob('*.md'):note.unlink()
            (root/'notes/manifest.json').unlink();w.state.close();w=Worker(config,p,Publisher())
            self.assertTrue(w.tick(2e9)['sink_conflicts'])
            self.assertTrue((root/'notes/manifest.json').exists());self.assertEqual(p.calls,1);w.state.close()

    def test_quota_denial_does_not_consume_three_provider_attempts(self):
        class Provider:
            def edit(self, *_): raise TokenBudgetExceeded('daily_text_token_budget_reached')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); w = Worker(self.setup_fixture(root), Provider())
            with self.assertRaises(TokenBudgetExceeded): w.tick(2e9)
            b = w.state.items('batch:')[0][1]
            self.assertEqual(b['attempts'], 0); self.assertEqual(b['last_error'], 'daily_text_token_budget_reached'); w.state.close()

    def test_capture_since_and_midnight_floor(self):
        class Provider:
            seen = []
            def edit(self, rows, _):
                self.seen.append([r.sequence for r in rows])
                return {'clean': '原话', 'distilled': '## 安排\n\n原话', 'uncertain_terms': []}, {'model': 'fake'}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); config = self.setup_fixture(root); config['capture_since'] = '2026-10-18T00:03:00Z'
            p = Provider(); w = Worker(config, p); w.tick(2e9)
            self.assertEqual(p.seen, [[3,4]]); w.state.close()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); config = self.setup_fixture(root)
            # The same recording spans midnight; first source on the next day has sequence 4.
            mpath = root / 'sessions/test-session/4.json'; m = json.loads(mpath.read_text()); m['metadata']['capture_date'] = '2026-10-19'; m['metadata']['capture_started_at'] = '2026-10-19T00:00:00Z'; atomic(mpath,m)
            rpath = root/'asr'/source(4).source_id/'receipt.json';r=json.loads(rpath.read_text());r['capture_date']='2026-10-19';atomic(rpath,r)
            p = Provider(); p.seen=[];w=Worker(config,p);w.tick(2e9)
            self.assertEqual(p.seen, [[0,1,2,3],[4]]);w.state.close()


if __name__ == '__main__': unittest.main()
