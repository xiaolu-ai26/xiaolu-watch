#!/usr/bin/env python3
"""Today read projection; local body context never enters the summary provider."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from context_analysis import retirement
from context_analysis.health import analyze, locations
from context_analysis.reading import cards, iso, Summaries
from text_output.store import atomic, singleton
from text_worker import Worker


class Overview:
    def __init__(self, config, worker=None):
        self.config = config
        self.worker = worker or Worker(config)
        self.state = self.worker.state
        self.root = Path(config['reading_root'])
        self.summary = Summaries(self.state, self.worker.provider, config)

    def tick(self, now=None):
        now = time.time() if now is None else now
        snap = retirement.snapshot(self.config)
        ledger = retirement.store(self.config)
        timezone = self.config.get('timezone', 'Asia/Taipei')
        dates = {b['date'] for _,b in self.state.items('batch:') if b.get('output') and self.worker._eligible(b)}
        dates.add(dt.datetime.fromtimestamp(now, ZoneInfo(timezone)).date().isoformat())
        self.summary.requests = 0
        for date in sorted(dates):
            context = ledger.export_context(date) if ledger else {}
            day_cards = cards(self.worker._saved_batches(date), snap['group_ids'])
            for card in day_cards:
                digest = card.pop('version')
                version_key = 'card-version:' + card['id']
                old = self.state.get(version_key, {})
                revision = old.get('revision', 0) + (old.get('digest') != digest)
                self.state.put(version_key, {'digest': digest, 'revision': revision})
                card['version'] = revision
                # Hash identity used for summary cache; public card version stays numeric.
                summary_card = dict(card, version=digest)
                summary = self.summary.get([summary_card], 'card:' + card['id'], now, allow_request=self.summary.requests < 2)
                if summary['state'] == 'ready':
                    card['summary'] = summary['text']
                else:
                    card['summary'] = ''
            daily_inputs = [dict(card, clean=card['summary']) for card in day_cards]
            day_ready = all(card['summary'] for card in day_cards)
            overview = self.summary.get(daily_inputs, 'day:' + date, now, allow_request=day_ready and self.summary.requests < 2)
            if overview['state'] == 'empty':
                overview['state'] = 'no_data'
            flags = context.get('function_flags', {})
            body = analyze(context.get('health', []), date, timezone, now) if flags.get('health_enabled') else {
                'state': 'disabled', 'lines': ['健康背景尚未启用。'], 'sample_count': 0}
            local_day = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(), ZoneInfo(timezone))
            visits = locations(context.get('places', []), local_day.timestamp(), (local_day+dt.timedelta(days=1)).timestamp(), now) if flags.get('location_enabled') else []
            if visits:
                for visit in visits[-3:]:
                    a = dt.datetime.fromisoformat(visit['start_at']).astimezone(ZoneInfo(timezone)).strftime('%H:%M')
                    b = dt.datetime.fromisoformat(visit['end_at']).astimezone(ZoneInfo(timezone)).strftime('%H:%M')
                    body['lines'].append(f"{a}–{b}：{visit['label']}（已记录停留，不代表当前位置）。")
            coverage = []
            for _, batch in self.state.items('batch:'):
                if batch['date'] != date or not self.worker._eligible(batch):
                    continue
                for source in batch.get('sources', []):
                    if source.get('captured') is not None and source.get('duration') is not None:
                        coverage.append({'start_at': iso(source['captured']), 'end_at': iso(source['captured']+source['duration']), 'state': 'no_speech' if batch.get('no_speech') else 'recorded' if batch.get('output') else 'pending'})
            public = {'version': 3, 'retirement_generation': snap['generation'], 'date': date, 'timezone': timezone, 'generated_at': iso(now),
                'overview': {k: overview[k] for k in ('state','text')},
                'body': {k: body[k] for k in ('state','lines','sample_count')}, 'coverage': coverage, 'cards': []}
            sidecar = {'version': 1, 'cards': []}
            for card in day_cards:
                spans = card.pop('source_spans'); card.pop('source_ids', None)
                card['owner_label'] = 'self' if card['owner_label']=='疑似本人讲话' else 'other' if card['owner_label']=='包含未匹配本人的声音' else 'unknown'
                public['cards'].append(card)
                sidecar['cards'].append({'id': card['id'], 'version': card['version'], 'group_id': card['id'],
                    'generation': snap['generation'], 'source_spans': [dict(source_id=s['source_id'],start=s['start'],end=s['end'],
                        precision=s.get('time_precision','unknown'),overlap=False) for s in spans]})
            all_spans = [(card['id'], span) for card in sidecar['cards'] for span in card['source_spans']]
            for owner, span in all_spans:
                span['overlap'] = any(other != owner and span['source_id'] == candidate['source_id'] and span['start'] is not None and span['end'] is not None and candidate['start'] is not None and candidate['end'] is not None and max(span['start'], candidate['start']) < min(span['end'], candidate['end']) for other, candidate in all_spans)
            revision = self.state.get('reading-revision:'+date, 0) + 1
            public['revision'] = sidecar['revision'] = revision
            with retirement.guard(self.config, snap['generation']):
                atomic(self.root / (date+'.sources.json'), sidecar)
                atomic(self.root / (date+'.json'), public)
                self.state.put('reading-revision:'+date, revision)
        if ledger:
            with retirement.guard(self.config, snap['generation']):
                for key, value in self.state.items('overview:'):
                    if 'input_group_ids' not in value or set(value['input_group_ids']) & set(snap['group_ids']):
                        if not value.get('retired'):
                            self.state.put(key, {'retired': True, 'attempts': 3})
                for transaction in snap.get('transactions', []):
                    identifier = transaction.get('transaction_id') or transaction.get('deletion_id') or transaction.get('id')
                    if identifier:
                        ledger.report_derivative_stage(identifier, 'summary', 'done', 'retired_inputs_and_caches_removed')
                        ledger.report_derivative_stage(identifier, 'projection', 'done', 'filtered_projection_committed')
        return {'days': len(dates), 'generation': snap['generation']}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    config=json.loads(args.config.read_text(encoding='utf-8'))
    with singleton(Path(config['home'])/'overview.lock'):
        worker=Overview(config)
        while True:
            try:
                result=worker.tick()
                atomic(Path(config['home'])/'overview-status.json',dict(status='ok',checked_at=time.time(),**result))
            except Exception as e:
                atomic(Path(config['home'])/'overview-status.json',dict(status='retry_or_review',checked_at=time.time(),error_type=type(e).__name__))
                if args.once: raise
            if args.once: break
            time.sleep(120)

if __name__=='__main__':main()
