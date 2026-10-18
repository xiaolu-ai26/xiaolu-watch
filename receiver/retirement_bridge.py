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
