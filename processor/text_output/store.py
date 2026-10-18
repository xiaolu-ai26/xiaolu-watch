"""Crash-safe local state. Provider admission reserves tokens before network I/O."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

_root = Path(__file__).resolve().parents[2]
for _directory in (_root / 'receiver', _root / 'receiver_windows'):
    if _directory.is_dir():
        sys.path.insert(0, str(_directory))
from platform_store import replace, sync_directory


class TokenBudgetExceeded(RuntimeError):
    code = 'daily_text_token_budget_reached'


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    try:
        with temp.open('x', encoding='utf-8', newline='\n') as f:
            os.chmod(temp, 0o600); f.write(data); f.flush(); os.fsync(f.fileno())
        replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS admissions (id INTEGER PRIMARY KEY, day TEXT NOT NULL, reserved INTEGER NOT NULL, usage TEXT, at REAL NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS admission_settlements(admission INTEGER PRIMARY KEY, usage_hash TEXT NOT NULL, actual INTEGER NOT NULL, settled_at REAL NOT NULL)')
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def items(self, prefix):
        return [(k, json.loads(v)) for k, v in self.db.execute('SELECT key,value FROM kv WHERE key LIKE ? ORDER BY key', (prefix + '%',))]

    def admit(self, day, reservation, daily_limit):
        if type(reservation) is not int or reservation <= 0 or type(daily_limit) is not int or daily_limit < reservation:
            raise ValueError('invalid token budget')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self._settle_completed_locked()
            used = self.db.execute('SELECT coalesce(sum(coalesce(s.actual,a.reserved)),0) FROM admissions a LEFT JOIN admission_settlements s ON s.admission=a.id WHERE a.day=?', (day,)).fetchone()[0]
            if used + reservation > daily_limit:
                raise TokenBudgetExceeded('daily_text_token_budget_reached')
            cur = self.db.execute('INSERT INTO admissions(day,reserved,at) VALUES (?,?,?)', (day, reservation, time.time()))
            self.db.commit(); return cur.lastrowid
        except BaseException:
            self.db.rollback(); raise

    @staticmethod
    def confirmed_usage(usage):
        """Only fully received, matched, stop responses can release unused reserves.

        Cached input is included in prompt_tokens; reasoning is included in
        completion_tokens. Neither is added a second time or discounted here.
        """
        if not isinstance(usage,dict):return None
        metadata=usage.get('response_metadata',{})
        if not isinstance(metadata,dict) or metadata.get('model_matches_requested') is not True or metadata.get('finish_reason')!='stop':return None
        values=[usage.get(k) for k in ('prompt_tokens','completion_tokens','total_tokens')]
        if any(type(n) is not int or not 0<=n<=10**9 for n in values):return None
        prompt,completion,total=values
        if total!=prompt+completion or total==0:return None
        for key,ceiling in (('prompt_cache_hit_tokens',prompt),('prompt_cache_miss_tokens',prompt)):
            if key in usage and (type(usage[key]) is not int or not 0<=usage[key]<=ceiling):return None
        if ('prompt_cache_hit_tokens' in usage) != ('prompt_cache_miss_tokens' in usage):return None
        if 'prompt_cache_hit_tokens' in usage and usage['prompt_cache_hit_tokens']+usage['prompt_cache_miss_tokens']!=prompt:return None
        for field,key,ceiling in (('prompt_tokens_details','cached_tokens',prompt),('completion_tokens_details','reasoning_tokens',completion)):
            if field in usage:
                detail=usage[field]
                if not isinstance(detail,dict):return None
                if key in detail and (type(detail[key]) is not int or not 0<=detail[key]<=ceiling):return None
        detail=usage.get('prompt_tokens_details',{})
        if 'cached_tokens' in detail and 'prompt_cache_hit_tokens' in usage and detail['cached_tokens']!=usage['prompt_cache_hit_tokens']:return None
        return total

    def _settle_completed_locked(self):
        changed=0
        for admission,raw in self.db.execute('SELECT a.id,a.usage FROM admissions a LEFT JOIN admission_settlements s ON s.admission=a.id WHERE a.usage IS NOT NULL AND s.admission IS NULL').fetchall():
            try:usage=json.loads(raw)
            except (TypeError,ValueError):continue
            actual=self.confirmed_usage(usage)
            if actual is None:continue
            self.db.execute('INSERT INTO admission_settlements VALUES (?,?,?,?)',(admission,hashlib.sha256(raw.encode()).hexdigest(),actual,time.time()));changed+=1
        return changed

    def settle_completed(self):
        try:
            self.db.execute('BEGIN IMMEDIATE');changed=self._settle_completed_locked();self.db.commit();return changed
        except BaseException:self.db.rollback();raise

    def usage(self, admission, usage):
        raw=json.dumps(usage)
        try:
            self.db.execute('BEGIN IMMEDIATE')
            old=self.db.execute('SELECT usage FROM admissions WHERE id=?',(admission,)).fetchone()
            if old is None:raise ValueError('usage_without_admission')
            if old[0] is not None and json.loads(old[0])!=usage:raise ValueError('conflicting_usage_preserved')
            if old[0] is None:self.db.execute('UPDATE admissions SET usage=? WHERE id=?',(raw,admission))
            self._settle_completed_locked();self.db.commit()
        except BaseException:self.db.rollback();raise

    def budget(self, day):
        rows=list(self.db.execute('SELECT a.reserved,a.usage,s.actual FROM admissions a LEFT JOIN admission_settlements s ON s.admission=a.id WHERE a.day=?',(day,)))
        usages=[]
        for _,raw,_ in rows:
            try:value=json.loads(raw) if raw else {}
            except (TypeError,ValueError):value={}
            if isinstance(value,dict):usages.append(value)
        return {'day_utc':day,'admitted_requests':len(rows),'reserved_tokens':sum(r for r,_,_ in rows),
            'committed_tokens':sum(actual if actual is not None else reserved for reserved,_,actual in rows),
            'settled_requests':sum(actual is not None for _,_,actual in rows),
            'unsettled_reserved_tokens':sum(reserved for reserved,_,actual in rows if actual is None),
            'reported_prompt_tokens':sum(u.get('prompt_tokens',0) for u in usages if type(u.get('prompt_tokens',0)) is int),
            'reported_completion_tokens':sum(u.get('completion_tokens',0) for u in usages if type(u.get('completion_tokens',0)) is int)}

    def close(self):
        self.db.close()


@contextlib.contextmanager
def singleton(path):
    """OS advisory lock is released by process death; no stale PID guessing."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as f:
        if os.name == 'nt':
            import msvcrt
            f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
