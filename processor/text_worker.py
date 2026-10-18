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
from context_analysis import retirement
from context_analysis.reading import filter_batch


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
        deleted = retirement.snapshot(self.config)['group_ids']
        batches = [filter_batch(v, deleted) for _, v in self.state.items('batch:') if v['date'] == date and v.get('output') and self._eligible(v)]
        return sorted([b for b in batches if b['output']['clean'] or b['output']['distilled']], key=lambda x: (x['captured'], x['session'], x['sequence']))

    def _eligible(self, batch):
        if self.capture_floor is None:
            return True
        sources = batch.get('sources')
        # Legacy missing per-source times are retired, never guessed from a batch's first time.
        return bool(sources) and all(type(s.get('captured')) in (int, float) and math.isfinite(s['captured'])
                                     and s['captured'] >= self.capture_floor for s in sources)

    @retirement.guarded
    def export(self, date):
        generation = retirement.snapshot(self.config)['generation']
        batches = self._saved_batches(date)
        if not batches:
            self._retire_empty_notes(date)
            self.manifest()
            return
        for kind, directory in [('clean', NOTE_SCOPES[0]), ('distilled', NOTE_SCOPES[1])]:
            body = daily_markdown(date, kind, [b['output'] for b in batches])
            target = self.notes / directory / (date + '_' + directory + '.md')
            previous = self.state.get('note:' + str(target))
            expected_hash = hashlib.sha256(body.encode()).hexdigest()
            if target.exists() and (target.is_symlink() or hash_file(target) not in (previous, expected_hash)):
                raise RuntimeError('text_outbox_human_edit_preserved')
            retirement.check(self.config, generation)
            atomic(target, body)
            self.state.put('note:' + str(target), hash_file(target))
            relative = directory + '/' + target.name
            self.state.put('deleted_notes', [x for x in self.state.get('deleted_notes', []) if x['relative_path'] != relative])
        self.manifest()

    def _retire_empty_notes(self, date):
        deleted = self.state.get('deleted_notes', [])
        for directory in NOTE_SCOPES:
            target = self.notes / directory / (date + '_' + directory + '.md')
            prior = self.state.get('note:' + str(target))
            if not prior:
                continue
            if target.exists():
                if target.is_symlink() or hash_file(target) != prior:
                    raise RuntimeError('deleted_note_human_edit_preserved')
                trash = self.home / 'deleted-notes' / directory / target.name
                trash.parent.mkdir(parents=True, exist_ok=True)
                target.replace(trash)
            item = {'relative_path': directory + '/' + target.name, 'sha256': prior}
            if item not in deleted:
                deleted.append(item)
        self.state.put('deleted_notes', deleted)

    @retirement.guarded
    def reconcile_deletions(self):
        snap = retirement.snapshot(self.config)
        if not snap['group_ids']:
            return
        dates = sorted({b['date'] for _, b in self.state.items('batch:') if b.get('output') and self._eligible(b)})
        for key, batch in self.state.items('batch:'):
            if batch.get('output') and self._eligible(batch):
                for group in batch.get('conversation_groups', []):
                    if group['id'] in snap['group_ids']:
                        date_key = 'retired-group-dates:'+group['id']
                        self.state.put(date_key, sorted(set(self.state.get(date_key, [])) | {batch['date']}))
                filtered = filter_batch(batch, snap['group_ids'])
                if filtered != batch:
                    self.state.put(key, filtered)
        # Invalidate summaries that might contain retired content; never send them as future context.
        for key, value in self.state.items('overview:')+self.state.items('day-reading:')+self.state.items('manual-day-ready:'):
            if 'input_group_ids' not in value or any(group_id in value['input_group_ids'] for group_id in snap['group_ids']):
                self.state.put(key, {'retired': True, 'attempts': 3})
        local_failures = []; cloud_failures = []
        for date in dates:
            try:
                self.export(date)
                self.state.put('local-delete-conflict:'+date, None)
            except Exception as error:
                local_failures.append(date)
                self.state.put('local-delete-conflict:'+date, {'generation': snap['generation'], 'reason': type(error).__name__})
            if self.publisher and self.state.get('cloud-retired:'+date) != snap['generation']:
                try:
                    retirement.check(self.config, snap['generation'])
                    self.publisher.remove_retired_blocks(date, [b['output'] for b in self._saved_batches(date) if b.get('published')], snap['group_ids'])
                    self.state.put('cloud-retired:'+date, snap['generation'])
                    self.state.put('cloud-delete-conflict:'+date, None)
                except Exception as error:
                    cloud_failures.append(date)
                    self.state.put('cloud-delete-conflict:'+date, {'generation': snap['generation'], 'reason': type(error).__name__})
        ledger = retirement.store(self.config)
        if ledger:
            ack_path = self.notes / 'consumer-ack.json'
            ack = json.loads(ack_path.read_text(encoding='utf-8')) if ack_path.is_file() and not ack_path.is_symlink() else {}
            manifest = json.loads((self.notes / 'manifest.json').read_text(encoding='utf-8')) if (self.notes / 'manifest.json').exists() else {}
            notes_done = (not local_failures and ack.get('generation') == snap['generation'] and ack.get('manifest_revision') == manifest.get('manifest_revision') and ack.get('files') == manifest.get('files') and ack.get('deleted') == manifest.get('deleted') and not ack.get('conflicts'))
            for transaction in snap.get('transactions', []):
                transaction_id = transaction.get('transaction_id') or transaction.get('deletion_id') or transaction.get('id')
                if transaction_id:
                    ledger.report_derivative_stage(transaction_id, 'processed_text', 'done', 'managed_outputs_reconciled')
                    affected_dates = {date for group in transaction.get('group_ids', []) for date in self.state.get('retired-group-dates:'+group, [])}
                    affected_conflicts = [date for date in cloud_failures if date in affected_dates]
                    ledger.report_derivative_stage(transaction_id, 'cloud', 'retryable_failed' if affected_conflicts else 'done', {'conflicting_dates': affected_conflicts})
                    ledger.report_derivative_stage(transaction_id, 'notes', 'done' if notes_done else 'retryable_failed' if ack.get('conflicts') or local_failures else 'pending', 'consumer_ack_verified' if notes_done else 'consumer_conflict_preserved' if ack.get('conflicts') or local_failures else 'waiting_for_consumer_ack')

    @retirement.guarded
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
        value = {'version': 1, 'kind': 'dual-text', 'files': files,
                 'deleted': self.state.get('deleted_notes', []), 'generation': retirement.snapshot(self.config)['generation']}
        if {x['relative_path'] for x in files} & {x['relative_path'] for x in value['deleted']}:
            raise RuntimeError('deleted_note_still_active')
        value['manifest_revision'] = digest(value)
        value['generated_at'] = time.time()
        atomic(self.notes / 'manifest.json', value)

    @retirement.guarded
    def _publish(self, batch):
        if not batch.get('output') or not self._eligible(batch):
            return
        snap = retirement.snapshot(self.config)
        batch = filter_batch(batch, snap['group_ids'])
        if not batch['output']['clean'] and not batch['output']['distilled']:
            return
        # Sink conflicts are durable per-date status; neither blocks fresh ASR text.
        try:
            self.export(batch['date'])
            self.state.put('sink-local:'+batch['date'], None)
        except Exception as error:
            self.state.put('sink-local:'+batch['date'], {'reason': type(error).__name__})
        if self.publisher and not batch.get('published'):
            if self.state.get('cloud-delete-conflict:'+batch['date']):
                return
            output = dict(batch['output'], id=batch['id'], _groups=batch.get('conversation_groups', []))
            retirement.check(self.config, snap['generation'])
            try:
                day = self.publisher.append(batch['date'], output)
            except Exception as error:
                self.state.put('sink-cloud:'+batch['date'], {'reason': type(error).__name__})
                return
            batch.update(published=True, document_id=day['id'], document_url=day.get('url', ''))
            self.state.put('batch:' + batch['id'], batch)
            self.state.put('sink-cloud:'+batch['date'], None)

    @retirement.guarded
    def _record_error(self, key, error):
        # Never write a captured batch object after releasing a publication guard.
        # Deletion may have scrubbed it in the meantime. Merge fixed error metadata
        # into the CURRENT record, preserving all current retirement edits.
        current = self.state.get(key)
        if current is None:
            return
        current['last_error'] = getattr(error, 'code', type(error).__name__)
        if isinstance(error, TokenBudgetExceeded):
            current['attempts'] = max(0, current.get('attempts', 0)-1)
            tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
            current['retry_after'] = dt.datetime.combine(tomorrow, dt.time(), dt.timezone.utc).timestamp()+60
        self.state.put(key, current)

    def tick(self, clock=None, reviewed_scope=None):
        now = time.time() if clock is None else clock
        since = self.capture_floor
        if reviewed_scope is None:
            self.reconcile_deletions()
        generation = retirement.snapshot(self.config)['generation']
        if reviewed_scope is not None:
            if generation != reviewed_scope['expected_generation']:
                raise RuntimeError('reviewed_batch_generation_changed')
            generation = reviewed_scope['expected_generation']
        groups = discover(self.config['asr_outbox'], self.config['receiver_sessions'], capture_since=since,
                          derived_asr_root=self.config.get('derived_asr_outbox'), derived_asr_model=self.config.get('derived_asr_model'),
                          retired_source_ids=retirement.snapshot(self.config).get('retired_raw_source_ids', []))
        if reviewed_scope is None:
            self.manifest()  # Even with no new audio, retire stale manifest entries without deleting files.
            # First finish generated but unpublished artifacts. Never call the provider again.
            # Local artifacts are independently recoverable even after cloud published=True.
            for date in sorted({b['date'] for _, b in self.state.items('batch:') if b.get('output') and self._eligible(b)}):
                try:
                    self.export(date)
                except Exception as error:
                    self.state.put('sink-local:'+date, {'reason': type(error).__name__})
            for _, batch in self.state.items('batch:'):
                if batch.get('output') and self._eligible(batch) and (not batch.get('published') or not self.publisher):
                    self._publish(batch)
        completed = 0; speaker_waiting = 0; unknown_units = 0; speaker_reasons = {}
        for (date, session), slots in sorted(groups.items()):
            fixed_rows = None
            if reviewed_scope is not None:
                target = self.state.get('batch:' + reviewed_scope['batch_id'])
                if target is None or (date, session) != (target['date'], target['session']):
                    continue
                fixed_rows = [slots.get(seq) for seq in target['sequences']]
                if (any(row is None for row in fixed_rows) or
                    [row.source_id for row in fixed_rows] != reviewed_scope['source_ids'] or
                    digest({'source_batch': batch_identity(fixed_rows), 'schema': GROUP_SCHEMA}) != reviewed_scope['batch_id']):
                    raise ValueError('reviewed_batch_scope_changed')
            cursor_key = 'consumed:' + date + ':' + session
            consumed = set(self.state.get(cursor_key, [])) if since is None else set()
            consumed.update(seq for seq, item in slots.items() if item is not None and item.asr_model == 'source_retired')
            floor = 0
            if slots and min(slots) > 0 and any(min(slots)-1 in other for (d,s),other in groups.items() if s==session and d!=date):
                floor = min(slots)  # A committed previous-day predecessor proves the midnight boundary.
            # Recover source consumption after crash between immutable result commit and cursor commit.
            for _, prior in self.state.items('batch:'):
                if prior['date'] == date and prior['session'] == session and self._eligible(prior) and (prior.get('output') or prior.get('no_speech')):
                    consumed.update(prior['sequences'])
            if since is not None:
                consumed.update(seq for seq, item in slots.items() if item is not None and item.captured < since)
            if reviewed_scope is None:
                self.state.put(cursor_key, sorted(consumed))
            window_args = {'max_seconds': int(self.config.get('batch_seconds', 300)),
                           'max_chars': int(self.config.get('batch_chars', 3500)),
                           'tail_seconds': int(self.config.get('tail_seconds', 180)), 'floor': floor}
            if fixed_rows is not None:
                rows = fixed_rows
            elif self.conversation_mode:
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
            waiting_budget = batch.get('last_error') == 'daily_text_token_budget_reached'
            budget_available = False
            if waiting_budget:
                budget = self.state.budget(dt.datetime.now(dt.timezone.utc).date().isoformat())
                budget_available = budget['committed_tokens'] < int(self.config.get('provider',{}).get('daily_token_reservation_limit',0))
            if batch.get('attempts', 0) >= 3 or (batch.get('retry_after', 0) > now and not (waiting_budget and budget_available)):
                continue
            if not any(r.text for r in rows):
                with retirement.guard(self.config, generation):
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
            with retirement.guard(self.config, generation):
                batch.update(attempts=batch['attempts'] + 1, retry_after=now + 600)
                self.state.put(key, batch)
            try:
                if self.conversation_mode:
                    grouped, provenance = retirement.request(self.provider, lambda: self.provider.edit_conversations(rows, units, conversation_previous, context), self.config, generation,
                        group_ids=[conversation_previous['id']] if conversation_previous else [], source_ids=[row.source_id for row in rows])
                    output, group_sidecar = materialize(grouped, units, identity, conversation_previous)
                    batch.update(conversation_schema=GROUP_SCHEMA, conversation_groups=group_sidecar, speaker=speaker_state)
                else:
                    output, provenance = retirement.request(self.provider, lambda: self.provider.edit(rows, context), self.config, generation, source_ids=[row.source_id for row in rows])
                with retirement.guard(self.config, generation):
                    batch.update(output=output, provider=provenance, raw_tail=''.join(r.text for r in rows)[-600:], generated_at=time.time(), last_error=None)
                    self.state.put(key, batch)
                    self.state.put(cursor_key, sorted(consumed | {r.sequence for r in rows}))
                    self._publish(batch)
                completed += 1
            except Exception as e:
                self._record_error(key, e)
                raise
            if completed >= int(self.config.get('max_batches_per_tick', 2)):
                break
        blocked = [{'batch': b['id'], 'reason': b.get('last_error'), 'attempts': b.get('attempts'),
                    'retry_after': b.get('retry_after')} for _, b in self.state.items('batch:')
                   if not b.get('output') and b.get('last_error') and self._eligible(b)]
        return {'new_batches': completed, 'days': len({k[0] for k in groups}), 'blocked_batches': blocked,
                'speaker_waiting_windows': speaker_waiting, 'speaker_unknown_units': unknown_units,
                'speaker_reasons': speaker_reasons, 'conversation_mode': self.conversation_mode,
                'sink_conflicts': [key for prefix in ('sink-', 'cloud-delete-conflict:', 'local-delete-conflict:') for key,value in self.state.items(prefix) if value]}


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
                    atomic(home / 'status.json', {'checked_at': time.time(), 'status': 'blocked' if result['blocked_batches'] or result['sink_conflicts'] else 'ok', **result,
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
