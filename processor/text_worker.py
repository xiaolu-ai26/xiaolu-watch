#!/usr/bin/env python3
"""Independent opt-in text worker: local ASR -> clean/distilled -> daily outputs.

No microphone/audio inference, no changes to the ASR pipeline or its legacy outbox.
Configuration (including the explicit paid provider) is private and required.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import time

from text_output.core import NOTE_SCOPES, batch_identity, daily_markdown, discover, next_batch, digest
from text_output.conversation import GROUP_SCHEMA, choose_window, utterances, materialize
from text_output.speaker_labels import attach as attach_speaker_labels
from text_output.lark_output import Lark
from text_output.provider import DeepSeek
from text_output.store import State, TokenBudgetExceeded, atomic, singleton


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Worker:
    def __init__(self, config, provider=None, publisher=None):
        self.config = config
        if config.get('version') != 1 or config.get('enabled') is not True:
            raise ValueError('explicit enabled text configuration required')
        self.home = Path(config['home']).resolve()
        self.notes = Path(config['notes_outbox']).resolve()
        self.capture_floor = None
        if config.get('capture_since'):
            stamp = dt.datetime.fromisoformat(config['capture_since'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('capture cutoff must be timezone aware')
            self.capture_floor = stamp.timestamp()
        if self.home == self.notes:
            raise ValueError('state and reader-facing output must be separate')
        self.home.mkdir(parents=True, exist_ok=True)
        self.state = State(self.home / 'state.sqlite3')
        self.conversation_mode = config.get('conversation_mode', False)
        if type(self.conversation_mode) is not bool:
            raise ValueError('conversation_mode must be a boolean')
        self.provider = provider or DeepSeek(config['provider'], self.state)
        self.publisher = publisher or (Lark(self.state, self.home) if config.get('feishu') is True else None)

    def _saved_batches(self, date):
        batches = [v for _, v in self.state.items('batch:') if v['date'] == date and v.get('output') and self._eligible(v)]
        return sorted(batches, key=lambda x: (x['captured'], x['session'], x['sequence']))

    def _eligible(self, batch):
        if self.capture_floor is None:
            return True
        sources = batch.get('sources')
        # Legacy missing per-source times are retired, never guessed from a batch's first time.
        return bool(sources) and all(type(s.get('captured')) in (int, float) and math.isfinite(s['captured'])
                                     and s['captured'] >= self.capture_floor for s in sources)

    def export(self, date):
        batches = self._saved_batches(date)
        if not batches:
            return
        for kind, directory in [('clean', NOTE_SCOPES[0]), ('distilled', NOTE_SCOPES[1])]:
            body = daily_markdown(date, kind, [b['output'] for b in batches])
            target = self.notes / directory / (date + '_' + directory + '.md')
            previous = self.state.get('note:' + str(target))
            expected_hash = hashlib.sha256(body.encode()).hexdigest()
            if target.exists() and (target.is_symlink() or hash_file(target) not in (previous, expected_hash)):
                raise RuntimeError('text_outbox_human_edit_preserved')
            atomic(target, body)
            self.state.put('note:' + str(target), hash_file(target))
        self.manifest()

    def manifest(self):
        files = []
        eligible_dates = {b['date'] for _, b in self.state.items('batch:') if b.get('output') and self._eligible(b)}
        for directory in NOTE_SCOPES:
            for path in sorted((self.notes / directory).glob('*.md')):
                date = next((d for d in eligible_dates if path.name == d + '_' + directory + '.md'), None)
                if date is None:
                    continue
                if path.is_symlink():
                    raise ValueError('no output symlinks')
                kind = 'clean' if directory == NOTE_SCOPES[0] else 'distilled'
                body = daily_markdown(date, kind, [b['output'] for b in self._saved_batches(date)])
                expected = hashlib.sha256(body.encode()).hexdigest()
                if hash_file(path) != expected:
                    continue  # Never advertise an older same-day mixed body before export replaces it.
                files.append({'relative_path': directory + '/' + path.name, 'sha256': expected, 'bytes': path.stat().st_size})
        atomic(self.notes / 'manifest.json', {'version': 1, 'kind': 'dual-text', 'generated_at': time.time(), 'files': files})

    def _publish(self, batch):
        if not batch.get('output') or not self._eligible(batch):
            return
        # Independent sink: a cloud failure must never block local durable notes.
        self.export(batch['date'])
        if self.publisher and not batch.get('published'):
            output = dict(batch['output'], id=batch['id'])
            day = self.publisher.append(batch['date'], output)
            batch.update(published=True, document_id=day['id'], document_url=day.get('url', ''))
            self.state.put('batch:' + batch['id'], batch)

    def tick(self, clock=None):
        now = time.time() if clock is None else clock
        since = self.capture_floor
        groups = discover(self.config['asr_outbox'], self.config['receiver_sessions'], capture_since=since,
                          derived_asr_root=self.config.get('derived_asr_outbox'), derived_asr_model=self.config.get('derived_asr_model'))
        self.manifest()  # Even with no new audio, retire stale manifest entries without deleting files.
        # First finish generated but unpublished artifacts. Never call the provider again.
        # Local artifacts are independently recoverable even after cloud published=True.
        for date in sorted({b['date'] for _, b in self.state.items('batch:') if b.get('output') and self._eligible(b)}):
            self.export(date)
        for _, batch in self.state.items('batch:'):
            if batch.get('output') and self._eligible(batch) and (not batch.get('published') or not self.publisher):
                self._publish(batch)
        completed = 0; speaker_waiting = 0; unknown_units = 0; speaker_reasons = {}
        for (date, session), slots in sorted(groups.items()):
            cursor_key = 'consumed:' + date + ':' + session
            consumed = set(self.state.get(cursor_key, [])) if since is None else set()
            floor = 0
            if slots and min(slots) > 0 and any(min(slots)-1 in other for (d,s),other in groups.items() if s==session and d!=date):
                floor = min(slots)  # A committed previous-day predecessor proves the midnight boundary.
            # Recover source consumption after crash between immutable result commit and cursor commit.
            for _, prior in self.state.items('batch:'):
                if prior['date'] == date and prior['session'] == session and self._eligible(prior) and (prior.get('output') or prior.get('no_speech')):
                    consumed.update(prior['sequences'])
            if since is not None:
                consumed.update(seq for seq, item in slots.items() if item is not None and item.captured < since)
            self.state.put(cursor_key, sorted(consumed))
            window_args = {'max_seconds': int(self.config.get('batch_seconds', 300)),
                           'max_chars': int(self.config.get('batch_chars', 3500)),
                           'tail_seconds': int(self.config.get('tail_seconds', 180)), 'floor': floor}
            if self.conversation_mode:
                rows = choose_window(slots, consumed, now, pause_seconds=self.config.get('conversation_pause_seconds', 12), **window_args)
            else:
                rows = next_batch(slots, consumed, now, **window_args)
            if not rows:
                continue
            identity = batch_identity(rows)
            if self.conversation_mode:
                identity = digest({'source_batch': identity, 'schema': GROUP_SCHEMA})
            key = 'batch:' + identity
            batch = self.state.get(key, {'id': identity, 'date': date, 'session': session,
                    'sequence': rows[0].sequence, 'sequences': [r.sequence for r in rows], 'captured': rows[0].captured,
                    'sources': [r.identity() for r in rows], 'attempts': 0})
            if batch.get('attempts', 0) >= 3 or batch.get('retry_after', 0) > now:
                continue
            if not any(r.text for r in rows):
                batch['no_speech'] = True
                self.state.put(key, batch)
                self.state.put(cursor_key, sorted(consumed | {r.sequence for r in rows}))
                continue
            previous = [b for b in self._saved_batches(date) if b['session'] == session and b['sequence'] < rows[0].sequence]
            context = previous[-1]['raw_tail'] if previous else ''
            units = []; conversation_previous = None; speaker_state = None
            if self.conversation_mode:
                units = utterances(rows)
                if len(units) > 400:
                    raise ValueError('conversation utterance window too large')
                speaker_state = attach_speaker_labels(units, rows, self.config.get('speaker', {}), self.state, now)
                unknown_units += speaker_state['unknown_units']
                reasons = list(speaker_state.get('reasons', {}).values()) or [speaker_state.get('reason', 'speaker_unknown')]
                for reason in reasons:
                    speaker_reasons[reason] = speaker_reasons.get(reason, 0) + 1
                if speaker_state['waiting']:
                    speaker_waiting += 1
                    continue
                earlier = [b for b in self._saved_batches(date) if b.get('conversation_groups') and b['captured'] <= rows[0].captured]
                if earlier:
                    prior = earlier[-1]
                    conversation_previous = prior['conversation_groups'][-1]
                    context = prior['raw_tail']
            # Persist the attempt before network I/O; crashes count toward the retry ceiling too.
            batch.update(attempts=batch['attempts'] + 1, retry_after=now + 600)
            self.state.put(key, batch)
            try:
                if self.conversation_mode:
                    grouped, provenance = self.provider.edit_conversations(rows, units, conversation_previous, context)
                    output, group_sidecar = materialize(grouped, units, identity, conversation_previous)
                    batch.update(conversation_schema=GROUP_SCHEMA, conversation_groups=group_sidecar, speaker=speaker_state)
                else:
                    output, provenance = self.provider.edit(rows, context)
                batch.update(output=output, provider=provenance, raw_tail=''.join(r.text for r in rows)[-600:], generated_at=time.time(), last_error=None)
                self.state.put(key, batch)
                self.state.put(cursor_key, sorted(consumed | {r.sequence for r in rows}))
                self._publish(batch)
                completed += 1
            except Exception as e:
                # Never persist provider error bodies or arbitrary transcript-bearing exception strings.
                batch['last_error'] = getattr(e, 'code', type(e).__name__)
                if isinstance(e, TokenBudgetExceeded):
                    batch['attempts'] -= 1  # Admission denied: no provider request was made.
                    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
                    batch['retry_after'] = dt.datetime.combine(tomorrow, dt.time(), dt.timezone.utc).timestamp() + 60
                self.state.put(key, batch)
                raise
            if completed >= int(self.config.get('max_batches_per_tick', 2)):
                break
        blocked = [{'batch': b['id'], 'reason': b.get('last_error'), 'attempts': b.get('attempts'),
                    'retry_after': b.get('retry_after')} for _, b in self.state.items('batch:')
                   if not b.get('output') and b.get('last_error') and self._eligible(b)]
        return {'new_batches': completed, 'days': len({k[0] for k in groups}), 'blocked_batches': blocked,
                'speaker_waiting_windows': speaker_waiting, 'speaker_unknown_units': unknown_units,
                'speaker_reasons': speaker_reasons, 'conversation_mode': self.conversation_mode}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--recover-doc', nargs=2, metavar=('DATE', 'DOCUMENT_ID'))
    parser.add_argument('--recover-append', metavar='DATE')
    parser.add_argument('--retry-unapplied', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    home = Path(config['home'])
    with singleton(home / 'worker.lock'):
        worker = Worker(config)
        try:
            if args.recover_doc:
                worker.publisher.recover(*args.recover_doc)
                return
            if args.recover_append:
                worker.publisher.recover_append(args.recover_append, args.retry_unapplied)
                return
            while True:
                try:
                    result = worker.tick()
                    atomic(home / 'status.json', {'checked_at': time.time(), 'status': 'blocked' if result['blocked_batches'] else 'ok', **result,
                        'provider_budget': worker.state.budget(dt.datetime.now(dt.timezone.utc).date().isoformat())})
                except Exception as e:
                    atomic(home / 'status.json', {'checked_at': time.time(), 'status': 'retry_or_operator_review', 'error_type': type(e).__name__,
                        'reason': getattr(e, 'code', type(e).__name__),
                        'provider_budget': worker.state.budget(dt.datetime.now(dt.timezone.utc).date().isoformat())})
                    if args.once:
                        raise SystemExit(1)
                if args.once:
                    break
                time.sleep(60)
        finally:
            worker.state.close()


if __name__ == '__main__':
    main()
