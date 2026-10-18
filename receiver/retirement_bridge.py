"""Local pipeline boundary: a retired original must never be copied or published."""
import contextlib
import functools
import os
import re

@contextlib.contextmanager
def source_guard(source_id):
    root=os.environ.get('WATCH_MEMORY_CONTEXT_ROOT')
    if not root:yield False;return
    from context_store import StoreError
    if not isinstance(source_id,str) or not re.fullmatch('[0-9a-f]{64}',source_id):raise StoreError('invalid_source_id')
    from context_store import ContextStore,StoreError
    with ContextStore(root).generation_guard(timeout=60) as snapshot:
        yield source_id in snapshot['retired_raw_source_ids']

def protected_pull(function):
    @functools.wraps(function)
    def guarded(source_id,*args,**kwargs):
        with source_guard(source_id) as retired:
            if retired:
                from context_store import StoreError
                raise StoreError('retired_source_copy_denied',410)
            return function(source_id,*args,**kwargs)
    return guarded

class SourceLease:
    """Commit lease with an explicit unlocked ASR queue interval.

    The ASR worker guards each source itself. Reacquiring here rechecks retirement
    before copying results or publishing a job ACK; no stale snapshot is reused.
    """
    def __init__(self,source_id):self.source_id=source_id;self.manager=None;self.retired=False
    def acquire(self):
        manager=source_guard(self.source_id)
        self.retired=manager.__enter__();self.manager=manager
    def close(self):
        if self.manager is not None:
            manager=self.manager;self.manager=None;manager.__exit__(None,None,None)
    def run_unlocked(self,callback):
        self.close()
        try:return callback()
        finally:self.acquire()
    def __enter__(self):self.acquire();return self
    def __exit__(self,*args):self.close()

def source_lease(source_id):return SourceLease(source_id)
