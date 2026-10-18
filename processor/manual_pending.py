"""Import a finite, explicitly reviewed manual result via the normal text worker.

No model client is created by this provider. It binds the exact original ASR
units/sources and previous group, and never admits tokens or expands its scope.
The caller must hold the text worker's OS singleton lock.
"""
import copy
import time
from text_output.core import digest,discover,batch_identity
from text_output.conversation import validate_groups,utterances,materialize,GROUP_SCHEMA
from context_analysis import retirement
from context_analysis.reading import cards,validate_summary
from text_output.editorial import READING_POLICY_VERSION


def unit_hash(units):
    keys=('i','text','source_id','source_receipt_hash','asr_segment_index','sentence_index','start','end','absolute_start','absolute_end','time_precision')
    return digest([{k:u.get(k) for k in keys} for u in units])


def source_ids(batch):
    return [s['source_id'] for s in batch['sources']]


class ManualProvider:
    def __init__(self,candidate):
        self.candidate=candidate;self.calls=0

    def edit_conversations(self,rows,units,previous,context):
        c=self.candidate
        if self.calls or [r.source_id for r in rows]!=c['source_ids'] or unit_hash(units)!=c['units_hash']:
            raise ValueError('manual_pending_input_changed')
        if (previous or {}).get('id')!=c.get('previous_group_id'):
            raise ValueError('manual_pending_previous_changed')
        validate_groups(c['value'],units,previous,require_reading=True)
        self.calls+=1
        return copy.deepcopy(c['value']),{'provider':'codex_reviewed_manual','api_request':False,'admission_id':None,'input_units_hash':c['units_hash'],'manual_value_hash':digest(c['value']),'reviewed_at':c['reviewed_at']}


def apply_pending(worker,candidate,verified_provider=None):
    c=candidate
    if c.get('approved_for_apply') is not True or c.get('provenance')!='codex_reviewed_manual' or c.get('policy')!=READING_POLICY_VERSION:
        raise ValueError('manual_pending_review_required')
    if not c.get('source_ids') or len(c['source_ids'])!=len(set(c['source_ids'])):
        raise ValueError('manual_pending_source_scope_invalid')
    key='batch:'+c['batch_id'];original_provider=worker.provider;original_limit=worker.config.get('max_batches_per_tick',2)
    before=list(worker.state.db.execute('select id,day,reserved,usage,at from admissions order by id'))
    with retirement.guard(worker.config,c['generation']) as snap:
        if set(c['source_ids']) & set(snap.get('retired_raw_source_ids',[])) or c.get('previous_group_id') in snap.get('group_ids',[]):
            raise ValueError('manual_pending_retired')
        batch=worker.state.get(key)
        if batch is None:
            frozen=c.get('pending_batch')
            if not isinstance(frozen,dict) or not frozen.get('sequences') or frozen['sequences']!=sorted(set(frozen['sequences'])):
                raise ValueError('manual_pending_frozen_batch_required')
            batch={k:copy.deepcopy(frozen[k]) for k in ('date','session','sequence','sequences','captured','sources')}
            batch.update(id=c['batch_id'],attempts=0)
        if source_ids(batch)!=c['source_ids'] or not worker._eligible(batch):raise ValueError('manual_pending_batch_changed')
        overlap=[b for _,b in worker.state.items('batch:') if b.get('output') and set(source_ids(b)) & set(c['source_ids'])]
        if overlap and (len(overlap)!=1 or overlap[0]['id']!=c['batch_id']):raise ValueError('manual_pending_already_consumed_elsewhere')
        if batch.get('output'):
            if (batch.get('provider',{}).get('input_units_hash')!=c['units_hash'] or batch.get('provider',{}).get('manual_value_hash')!=digest(c['value'])):raise ValueError('manual_pending_existing_result_changed')
        # All validation happens without touching attempts, notes, manifests or cloud.
        discovered=discover(worker.config['asr_outbox'],worker.config['receiver_sessions'],capture_since=worker.capture_floor,
            derived_asr_root=worker.config.get('derived_asr_outbox'),derived_asr_model=worker.config.get('derived_asr_model'),
            retired_source_ids=snap.get('retired_raw_source_ids',[]))
        slots=discovered.get((batch['date'],batch['session']),{})
        rows=[slots.get(seq) for seq in batch['sequences']]
        if any(row is None for row in rows) or [r.source_id for r in rows]!=c['source_ids']:
            raise ValueError('manual_pending_missing_frozen_sources')
        if (batch['sources']!=[row.identity() for row in rows] or batch['captured']!=rows[0].captured or
            batch['sequence']!=rows[0].sequence or batch['sequences']!=[row.sequence for row in rows] or
            any((row.date,row.session)!=(batch['date'],batch['session']) for row in rows)):
            raise ValueError('manual_pending_frozen_metadata_changed')
        if digest({'source_batch':batch_identity(rows),'schema':GROUP_SCHEMA})!=c['batch_id']:
            raise ValueError('manual_pending_source_identity_changed')
        units=utterances(rows)
        earlier=[b for b in worker._saved_batches(batch['date']) if b['id']!=batch['id'] and b.get('conversation_groups') and b['captured']<=rows[0].captured]
        previous=earlier[-1]['conversation_groups'][-1] if earlier else None
        ManualProvider(c).edit_conversations(rows,units,previous,'')
        output,metadata=materialize(c['value'],units,c['batch_id'],previous)
        prospective=[b for b in worker._saved_batches(batch['date']) if b['id']!=batch['id']]+[dict(batch,output=output,conversation_groups=metadata)]
        if c.get('daily_overview'):
            kept=[g for g in cards(prospective) if g['reading_role']!='background']
            validate_summary(c['daily_overview'],{g['id'] for g in kept},'\n'.join(g['summary_input'] for g in kept))
        if not batch.get('output'):
            # Clear only this reviewed batch's budget wait after successful preflight.
            batch.update(retry_after=0,attempts=0);worker.state.put(key,batch)
    provider=verified_provider or ManualProvider(c)
    try:
        worker.provider=provider;worker.config['max_batches_per_tick']=1
        if batch.get('output'):
            with retirement.guard(worker.config,c['generation']):worker._publish(batch)
        else:worker.tick(reviewed_scope={'batch_id':c['batch_id'],'source_ids':c['source_ids'],'expected_generation':c['generation']})
    finally:
        worker.provider=original_provider;worker.config['max_batches_per_tick']=original_limit
    with retirement.guard(worker.config,c['generation']):
        saved=worker.state.get(key)
        if not saved.get('output') or saved.get('provider',{}).get('input_units_hash')!=c['units_hash']:
            raise ValueError('manual_pending_not_committed')
        # Old schema identities for these same inputs are only diagnostic pending
        # records; keep their provenance but stop reporting them as unhandled work.
        for other_key,other in worker.state.items('batch:'):
            if other_key!=key and not other.get('output') and source_ids(other)==c['source_ids']:
                other.update(last_error=None,retry_after=0,superseded_by=c['batch_id'],superseded_reason='same_sources_processed_by_reviewed_manual_result')
                worker.state.put(other_key,other)
        if c.get('daily_overview'):
            current=cards(worker._saved_batches(saved['date']));kept=[g for g in current if g['reading_role']!='background']
            validate_summary(c['daily_overview'],{g['id'] for g in kept},'\n'.join(g['summary_input'] for g in kept))
            worker.state.put('manual-day-ready:'+saved['date'],{'policy':READING_POLICY_VERSION,'output':c['daily_overview'],
                'content_versions':{g['id']:g['version'] for g in kept},'input_group_ids':[g['id'] for g in kept],
                'provider':'codex_reviewed_manual','reviewed_at':time.time()})
    after=list(worker.state.db.execute('select id,day,reserved,usage,at from admissions order by id'))
    if before!=after:raise RuntimeError('manual_pending_unexpected_admission_change')
    return {'batch_id':c['batch_id'],'sources':len(c['source_ids']),'provider_calls':provider.calls,'model_api_calls':0,'published':saved.get('published',False)}
