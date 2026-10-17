"""Yield to the existing ASR worker; no process termination or global GPU changes."""
import contextlib
import json
import os
from pathlib import Path
import time
from .runtime import SOURCE_ID

def asr_ready_first(config):
    inbox=config.get('asr_receiver_inbox')
    if not inbox:return False
    root=Path(config['asr_outbox'])
    for marker in Path(inbox).glob('*.ready'):
        audio=marker.with_suffix('');source_id=audio.stem
        if not SOURCE_ID.fullmatch(source_id) or not audio.is_file():continue
        # ASR scan requires 60s stability. Yield five seconds before that edge.
        if time.time()-audio.stat().st_mtime < 55:continue
        receipt=root/source_id/'receipt.json'
        try:
            done=json.loads(receipt.read_text(encoding='utf-8')).get('status')=='done'
        except (FileNotFoundError,ValueError):done=False
        if not done:return True
    return False

@contextlib.contextmanager
def gpu_slot(path):
    if not path:
        yield True;return
    path=Path(path)
    if not path.is_absolute() or path.is_symlink():raise ValueError('Invalid ASR GPU lock path')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0,os.SEEK_END)
        if handle.tell()==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:
                yield False;return
        else:
            import fcntl
            try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                yield False;return
        try:yield True
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
