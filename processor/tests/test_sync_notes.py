import hashlib,tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sync_notes as s
class ScopeChecks(unittest.TestCase):
 def test_bad_paths(self):
  for p in ['录音资料/../x.md','录音资料/a\\b.md','录音资料/C:x.md','录音资料/a\n.md','/录音资料/x.md']:
   with self.assertRaises(ValueError):s.safe_relative(p)
 def test_parent_symlink(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td).resolve();(r/'outside').mkdir();(r/'alias').symlink_to(r/'outside',target_is_directory=True)
   with self.assertRaises(OSError):
    with s.AnchoredVault(r/'alias'):pass
 def test_child_symlink_and_no_overwrite(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td).resolve();(r/'vault').mkdir();(r/'outside').mkdir();(r/'vault'/'录音资料').symlink_to(r/'outside',target_is_directory=True)
   with s.AnchoredVault(r/'vault') as a:
    with self.assertRaises(OSError):a.child('录音资料')
   (r/'vault'/'录音资料').unlink();src=r/'source';src.write_bytes(b'generated');digest=hashlib.sha256(src.read_bytes()).hexdigest()
   with s.AnchoredVault(r/'vault') as a:
    fd=a.child('录音资料');self.assertEqual(s.publish(a,fd,'a.md',src,digest),'copied');(r/'vault'/'录音资料'/'a.md').write_bytes(b'human');self.assertEqual(s.publish(a,fd,'a.md',src,digest),'conflict');self.assertEqual((r/'vault'/'录音资料'/'a.md').read_bytes(),b'human')
 def test_parent_replaced_after_anchor(self):
  with tempfile.TemporaryDirectory() as td:
   r=Path(td).resolve();(r/'vault').mkdir();(r/'outside').mkdir()
   with s.AnchoredVault(r/'vault') as a:
    (r/'vault').rename(r/'old');(r/'vault').symlink_to(r/'outside',target_is_directory=True)
    with self.assertRaises(RuntimeError):a.child('录音资料')
 def test_constant_fd_for_many_items(self):
  import resource,os
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve()/'vault';root.mkdir()
   with s.AnchoredVault(root) as a:
    first=a.child('录音资料');second=a.child('飞书修订快照');count=len(a.fds)
    limits=resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
     resource.setrlimit(resource.RLIMIT_NOFILE,(min(128,limits[0]),limits[1]))
     for _ in range(2048):
      self.assertEqual(a.child('录音资料'),first);self.assertEqual(a.child('飞书修订快照'),second)
    finally:resource.setrlimit(resource.RLIMIT_NOFILE,limits)
    self.assertEqual(len(a.fds),count);self.assertEqual(len(a.children),2)
if __name__=='__main__':unittest.main()
