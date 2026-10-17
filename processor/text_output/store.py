"""Crash-safe local state. Provider admission reserves tokens before network I/O."""
import contextlib
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
            used = self.db.execute('SELECT coalesce(sum(reserved),0) FROM admissions WHERE day=?', (day,)).fetchone()[0]
            if used + reservation > daily_limit:
                raise TokenBudgetExceeded('daily_text_token_budget_reached')
            cur = self.db.execute('INSERT INTO admissions(day,reserved,at) VALUES (?,?,?)', (day, reservation, time.time()))
            self.db.commit(); return cur.lastrowid
        except BaseException:
            self.db.rollback(); raise

    def usage(self, admission, usage):
        # Reservation is deliberately never refunded (including timeout/crash/partial response).
        with self.db:
            self.db.execute('UPDATE admissions SET usage=? WHERE id=?', (json.dumps(usage), admission))

    def budget(self, day):
        rows = list(self.db.execute('SELECT reserved,usage FROM admissions WHERE day=?', (day,)))
        usages = [json.loads(u) for _, u in rows if u]
        return {'day_utc': day, 'admitted_requests': len(rows), 'reserved_tokens': sum(r for r, _ in rows),
                'reported_prompt_tokens': sum(u.get('prompt_tokens', 0) for u in usages),
                'reported_completion_tokens': sum(u.get('completion_tokens', 0) for u in usages)}

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
