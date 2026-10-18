import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
import json
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync_notes as sync


class DualSyncTests(unittest.TestCase):
    def test_rename_journal_recovers_owned_B_then_new_remote_C(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();home=root/'home';home.mkdir();vault=root/'vault';vault.mkdir();(vault/'口语清稿').mkdir()
            name='2026-10-18_口语清稿.md';rel='口语清稿/'+name;target=vault/rel
            a=hashlib.sha256(b'A').hexdigest();b=hashlib.sha256(b'B').hexdigest();c=hashlib.sha256(b'C').hexdigest();target.write_bytes(b'B')
            state=home/'state/mac-sync.json';state.parent.mkdir();state.write_text(json.dumps({'managed_dual_hashes':{rel:a},'pending_dual':{rel:{'old':a,'new':b}}}))
            (home/'settings.json').write_text(json.dumps({'mac_sync':{'enabled':True,'destination':str(vault),'ssh_host':'synthetic','remote_notes_dir':'old','remote_dual_text_dir':'new'}}))
            def run(argv,timeout):
                src=argv[-2];dst=Path(argv[-1])
                if src.endswith('manifest.json'):
                    data={'version':1,'files':[]}
                    if ':new/' in src:data.update(kind='dual-text',files=[{'relative_path':rel,'sha256':c,'bytes':1}])
                    dst.write_text(json.dumps(data))
                else:dst.write_bytes(b'C')
                return ''
            with patch.object(sync,'run',side_effect=run),patch.object(sys,'argv',['sync','--home',str(home)]):sync.main()
            self.assertEqual(target.read_bytes(),b'C');after=json.loads(state.read_text());self.assertEqual(after['managed_dual_hashes'][rel],c);self.assertEqual(after['pending_dual'],{})
    def test_manifests_have_separate_exact_scopes(self):
        with self.assertRaises(ValueError): sync.safe_relative('口语清稿/2026-10-18_口语清稿.md')
        with self.assertRaises(ValueError): sync.safe_relative('录音资料/raw.md', sync.DUAL_SCOPES)
        with self.assertRaises(ValueError): sync.safe_relative('口语清稿/other.md', sync.DUAL_SCOPES)
        self.assertEqual(str(sync.safe_relative('口语清稿/2026-10-18_口语清稿.md', sync.DUAL_SCOPES)), '口语清稿/2026-10-18_口语清稿.md')

    def test_first_later_batch_update_and_manual_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve(); vault = root/'vault';vault.mkdir();source=root/'source'
            source.write_text('首批正文'); first=hashlib.sha256(source.read_bytes()).hexdigest()
            with sync.AnchoredVault(vault) as anchor:
                fd=anchor.child('口语清稿');name='2026-10-18_口语清稿.md'
                self.assertEqual(sync.publish(anchor,fd,name,source,first),'copied')
                source.write_text('首批正文\n\n第二批正文');second=hashlib.sha256(source.read_bytes()).hexdigest()
                self.assertEqual(sync.publish(anchor,fd,name,source,second),'conflict')
                self.assertEqual(sync.publish(anchor,fd,name,source,second,first),'updated')
                target=vault/'口语清稿'/name
                target.write_text('人工改写');source.write_text('第三批正文');third=hashlib.sha256(source.read_bytes()).hexdigest()
                self.assertEqual(sync.publish(anchor,fd,name,source,third,second),'conflict')
                self.assertEqual(target.read_text(),'人工改写')

    def test_updated_target_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();vault=root/'vault';vault.mkdir();outside=root/'outside';outside.write_text('原样')
            source=root/'source';source.write_text('new');sha=hashlib.sha256(source.read_bytes()).hexdigest()
            with sync.AnchoredVault(vault) as anchor:
                fd=anchor.child('口语清稿');(vault/'口语清稿'/'a.md').symlink_to(outside)
                with self.assertRaises(OSError):sync.publish(anchor,fd,'a.md',source,sha,'old')
            self.assertEqual(outside.read_text(),'原样')


if __name__=='__main__':unittest.main()

class DeletionAckTests(unittest.TestCase):
    def test_offline_older_managed_note_deleted_b_preserved_and_acknowledged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();home=root/'home';home.mkdir();vault=root/'vault';vault.mkdir();directory=vault/'口语清稿';directory.mkdir()
            rel='口语清稿/2026-10-18_口语清稿.md';target=vault/rel;target.write_bytes(b'older-owned')
            other=directory/'2026-10-19_口语清稿.md';other.write_text('B preserved')
            old=hashlib.sha256(target.read_bytes()).hexdigest();remote_new=hashlib.sha256(b'newer-not-yet-pulled').hexdigest()
            state=home/'state/mac-sync.json';state.parent.mkdir();state.write_text(json.dumps({'managed_dual_hashes':{rel:old}}))
            (home/'settings.json').write_text(json.dumps({'mac_sync':{'enabled':True,'destination':str(vault),'ssh_host':'fixture','remote_notes_dir':'old','remote_dual_text_dir':'dual'}}))
            acknowledgments=[]
            def run(argv,timeout):
                if str(argv[-1]).endswith('consumer-ack.json') and str(argv[-1]).startswith('fixture:'):
                    acknowledgments.append(json.loads(Path(argv[-2]).read_text()));return ''
                data={'version':1,'files':[]}
                if ':dual/' in argv[-2]:data.update(kind='dual-text',generation=1,manifest_revision='fixture-revision',deleted=[{'relative_path':rel,'sha256':remote_new}])
                Path(argv[-1]).write_text(json.dumps(data));return ''
            with patch.object(sync,'run',side_effect=run),patch.object(sys,'argv',['sync','--home',str(home)]):sync.main()
            self.assertFalse(target.exists());self.assertEqual(other.read_text(),'B preserved')
            self.assertEqual(acknowledgments[0]['manifest_revision'],'fixture-revision');self.assertEqual(acknowledgments[0]['conflicts'],[])
            self.assertEqual(len(list((vault/'.watch-memory-trash').iterdir())),1)

class DeletedRenameRecoveryTests(unittest.TestCase):
    def test_renamed_b_with_managed_a_and_only_deleted_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();home=root/'home';home.mkdir();vault=root/'vault';vault.mkdir();(vault/'口语清稿').mkdir()
            rel='口语清稿/2026-10-18_口语清稿.md';target=vault/rel;target.write_bytes(b'B-after-rename')
            a=hashlib.sha256(b'A-before-update').hexdigest();b=hashlib.sha256(target.read_bytes()).hexdigest()
            state=home/'state/mac-sync.json';state.parent.mkdir();state.write_text(json.dumps({'managed_dual_hashes':{rel:a},'pending_dual':{rel:{'old':a,'new':b}}}))
            (home/'settings.json').write_text(json.dumps({'mac_sync':{'enabled':True,'destination':str(vault),'ssh_host':'fixture','remote_notes_dir':'old','remote_dual_text_dir':'dual'}}))
            acknowledgments=[]
            def run(argv,timeout):
                if str(argv[-1]).startswith('fixture:'):
                    acknowledgments.append(json.loads(Path(argv[-2]).read_text()));return ''
                data={'version':1,'files':[]}
                if ':dual/' in argv[-2]:data.update(kind='dual-text',generation=2,manifest_revision='delete-only',deleted=[{'relative_path':rel,'sha256':b}])
                Path(argv[-1]).write_text(json.dumps(data));return ''
            with patch.object(sync,'run',side_effect=run),patch.object(sys,'argv',['sync','--home',str(home)]):
                sync.main();sync.main()  # Cold/repeated run cannot get stuck after ownership recovery.
            self.assertFalse(target.exists());self.assertEqual(len(list((vault/'.watch-memory-trash').iterdir())),1)
            self.assertEqual(acknowledgments[-1]['conflicts'],[])
            saved=json.loads(state.read_text());self.assertEqual(saved['pending_dual'],{});self.assertEqual(saved['managed_dual_hashes'],{})
