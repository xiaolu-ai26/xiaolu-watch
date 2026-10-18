"""Offline replay of a reviewed, fully received provider result; no new API call."""
import copy
from text_output.core import digest
from text_output.editorial import retain_literal_repetitions,READING_POLICY_VERSION
from text_output.conversation import validate_groups,utterances
from text_output.store import State
from context_analysis import retirement
from manual_pending import ManualProvider,apply_pending,unit_hash


def replay(worker,admission,binding,rows,previous,context):
    if binding.get('approved_for_apply') is not True:raise ValueError('replay_review_required')
    retirement.check(worker.config,binding['generation'])
    rejected=worker.state.get('rejected:'+str(admission))
    usage_row=worker.state.db.execute('SELECT usage FROM admissions WHERE id=?',(admission,)).fetchone()
    import json
    usage=json.loads(usage_row[0]) if usage_row and usage_row[0] else None
    if State.confirmed_usage(usage) is None:raise ValueError('replay_requires_confirmed_stop_response')
    if not rejected or digest(rejected)!=binding['rejected_hash'] or rejected.get('sources')!=[row.identity() for row in rows]:raise ValueError('replay_sources_or_response_changed')
    if digest(previous)!=binding['previous_hash'] or digest(context)!=binding['context_hash']:raise ValueError('replay_context_changed')
    earlier=[b for b in worker._saved_batches(rows[0].date) if b['id']!=binding['batch_id'] and b.get('conversation_groups') and b['captured']<=rows[0].captured]
    current_previous=earlier[-1]['conversation_groups'][-1] if earlier else None
    current_context=earlier[-1]['raw_tail'] if earlier else ''
    if digest(current_previous)!=binding['previous_hash'] or digest(current_context)!=binding['context_hash']:raise ValueError('replay_live_context_changed')

    units=utterances(rows);value,actions=retain_literal_repetitions(rejected['model_output'],units)
    if not actions:raise ValueError('replay_no_proven_conservative_normalization')
    validate_groups(value,units,previous,context,require_reading=True)
    candidate={'approved_for_apply':True,'provenance':'codex_reviewed_manual','policy':READING_POLICY_VERSION,
        'generation':binding['generation'],'batch_id':binding['batch_id'],'source_ids':[row.source_id for row in rows],
        'units_hash':unit_hash(units),'previous_group_id':previous['id'] if previous else None,'reviewed_at':binding['reviewed_at'],'value':value}
    class VerifiedReplay(ManualProvider):
        def edit_conversations(self,current_rows,current_units,current_previous,current_context):
            if digest(current_previous)!=binding['previous_hash'] or digest(current_context)!=binding['context_hash']:
                raise ValueError('replay_context_changed')
            result,metadata=super().edit_conversations(current_rows,current_units,current_previous,current_context)
            metadata.update(provider='api.deepseek.com',model='deepseek-flash',admission_id=admission,
                response_reused=True,api_request=False,usage={k:v for k,v in usage.items() if k!='response_metadata'},
                requested_effort=usage['response_metadata'].get('requested_effort'),effective_effort=usage['response_metadata'].get('effective_effort'),
                original_rejected_hash=binding['rejected_hash'],reading_normalization_actions=copy.deepcopy(actions))
            return result,metadata
    before=copy.deepcopy(rejected)
    result=apply_pending(worker,candidate,verified_provider=VerifiedReplay(candidate))
    if worker.state.get('rejected:'+str(admission))!=before:raise RuntimeError('replay_original_response_changed')
    return dict(result,reused_admission=admission,normalization_actions=actions)
