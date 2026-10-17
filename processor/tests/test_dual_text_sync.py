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
