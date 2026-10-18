"""Shared receiver-owned deletion ledger adapter, fail closed when configured."""
import sys
from pathlib import Path


def store(config):
    root = config.get('context_root')
    if not root:
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'receiver'))
    from context_store import ContextStore
    return ContextStore(Path(root))


def snapshot(config):
    ledger = store(config)
    return ledger.retirement_snapshot() if ledger else {'generation': 0, 'group_ids': [], 'source_ids': [], 'transactions': []}


def check(config, generation):
    if snapshot(config)['generation'] != generation:
        raise RuntimeError('deletion_generation_changed')


import contextlib
import functools
import threading
_LOCAL = threading.local()


@contextlib.contextmanager
def guard(config, expected=None):
    """Reentrant within this worker; outer scope owns the receiver's process lock."""
    root = str(Path(config['context_root']).resolve()) if config.get('context_root') else None
    active = getattr(_LOCAL, 'guard', None)
    if active is not None:
        if active[0] != root:
            raise RuntimeError('nested_different_retirement_store')
        if expected is not None and active[1]['generation'] != expected:
            raise RuntimeError('deletion_generation_changed')
        yield active[1]
        return
    ledger = store(config)
    manager = ledger.generation_guard(expected, timeout=30) if ledger else contextlib.nullcontext(snapshot(config))
    with manager as value:
        if expected is not None and value['generation'] != expected:
            raise RuntimeError('deletion_generation_changed')
        _LOCAL.guard = (root, value)
        try:
            yield value
        finally:
            _LOCAL.guard = None


def guarded(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        with guard(self.config):
            return function(self, *args, **kwargs)
    return wrapped


@contextlib.contextmanager
def egress(config, expected, group_ids=(), source_ids=()):
    with guard(config, expected) as value:
        if set(group_ids) & set(value['group_ids']) or set(source_ids) & set(value.get('retired_raw_source_ids', [])):
            raise RuntimeError('retired_input_egress_denied')
        yield


def request(provider, callback, config, expected, group_ids=(), source_ids=()):
    factory = functools.partial(egress, config, expected, group_ids, source_ids)
    if hasattr(provider, 'with_egress_guard'):
        # Production provider binds this to HTTPSConnection.request only; it does
        # not hold the lock while waiting for inference/response bytes.
        with provider.with_egress_guard(factory):
            return callback()
    # Dependency-injected deterministic providers have no network transport.
    with factory():
        return callback()
