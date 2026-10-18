#!/usr/bin/env python3
"""Prepare, then explicitly apply a reviewed day revision. Never reruns ASR.

Candidates live outside notes/projections. Paid admissions use the EXISTING text
state. Generation/source hashes bind the candidate; raw audio is never modified.
"""
import argparse,copy,datetime as dt,hashlib,json,sqlite3,time
from pathlib import Path
from text_worker import Worker
from text_output.core import discover,digest
from text_output.conversation import utterances,materialize
from text_output.editorial import READING_POLICY_VERSION
from text_output.store import atomic,singleton,TokenBudgetExceeded
from context_analysis import retirement


def content_version(batch):
    return digest({k:batch.get(k) for k in ('sources','output','conversation_groups')})


def load_entries(worker,batches,snap):
    rows=discover(worker.config['asr_outbox'],worker.config['receiver_sessions'],capture_since=worker.capture_floor,
        derived_asr_root=worker.config.get('derived_asr_outbox'),derived_asr_model=worker.config.get('derived_asr_model'),
        retired_source_ids=snap.get('retired_raw_source_ids',[]))
    unit_lookup={}
    for slots in rows.values():
        for row in slots.values():
            if row is not None and row.asr_model not in ('source_retired','capture_excluded'):
                for unit in utterances([row]):unit_lookup[(unit['source_id'],unit['asr_segment_index'],unit['sentence_index'])]=unit
    entries=[]
    for batch in batches:
        for index,group in enumerate(batch.get('conversation_groups',[])):
            if group['id'] in snap['group_ids']:continue
            units=[]
            for span in group['source_spans']:
                key=(span['source_id'],span.get('asr_segment_index'),span.get('sentence_index'))
                if key in unit_lookup and span.get('source_receipt_hash')==unit_lookup[key].get('source_receipt_hash'):
                    units.append(dict(unit_lookup[key],owner=span.get('owner','unknown')))
            if len(units)!=len(group['source_spans']):
                # Retained B may refer to a retired shared source: use B's own text,
                # never read deleted A from the source's private trash.
                if not group['source_spans']:raise ValueError('missing_group_source_evidence')
                units=[dict(group['source_spans'][0],i=0,text=group['clean_fragment'],owner='unknown',gap_before='unknown')]
                basis='previous_clean_fragment'
            else:basis='original_asr_units'
            entries.append({'batch_id':batch['id'],'group_index':index,'id':group['id'],'title':group['title'],'units':units,'basis':basis})
    return entries


def prepare(worker,date,directory):
    directory=Path(directory).resolve()
    if directory==worker.notes or directory.is_relative_to(worker.notes):raise ValueError('candidate_must_not_be_reader_output')
    directory.mkdir(parents=True,exist_ok=True)
    snap=retirement.snapshot(worker.config)
    batches=worker._saved_batches(date)
    if not batches:raise ValueError('no_eligible_conversation_batches')
    entries=load_entries(worker,batches,snap)
    candidate_file=directory/'candidate.json'
    base_versions={b['id']:content_version(b) for b in batches}
    candidate=json.loads(candidate_file.read_text()) if candidate_file.exists() else {'version':1,'date':date,'policy':READING_POLICY_VERSION,'generation':snap['generation'],'base_versions':base_versions,'results':{},'attempts':{}}
    if candidate['base_versions']!=base_versions or candidate['generation']!=snap['generation'] or candidate['policy']!=READING_POLICY_VERSION:raise ValueError('candidate_source_changed')
    atomic(directory/'before-batches.json',batches)
    backup=directory/'before-state.sqlite3'
    if not backup.exists():
        destination=sqlite3.connect(backup);worker.state.db.backup(destination);destination.close()
    chunks=[];chunk=[];chars=0
    for entry in entries:
        size=sum(len(u['text']) for u in entry['units'])
        if chunk and (len(chunk)>=16 or chars+size>6000):chunks.append(chunk);chunk=[];chars=0
        chunk.append(entry);chars+=size
    if chunk:chunks.append(chunk)
    for chunk in chunks:
        identity=digest([(e['batch_id'],e['group_index'],e['units']) for e in chunk])
        if identity in candidate['results']:continue
        if candidate['attempts'].get(identity,0)>=3:raise RuntimeError('candidate_attempt_limit')
        candidate['attempts'][identity]=candidate['attempts'].get(identity,0)+1;atomic(candidate_file,candidate)
        try:
            value,provenance,units=retirement.request(worker.provider,lambda:worker.provider.refine_existing(chunk),worker.config,snap['generation'],group_ids=[e['id'] for e in chunk])
        except TokenBudgetExceeded:
            candidate['attempts'][identity]-=1
            atomic(candidate_file,candidate)
            raise
        with retirement.guard(worker.config,snap['generation']):
            candidate['results'][identity]={'entries':chunk,'value':value,'provider':provenance,'units':units};atomic(candidate_file,candidate)
    return finish_candidate(candidate,batches,candidate_file)


def finish_candidate(candidate,batches,candidate_file,*,save=True):
    revised={batch['id']:copy.deepcopy(batch) for batch in batches}
    for result in candidate['results'].values():
        _,metadata=materialize(result['value'],result['units'],'candidate')
        for entry,new in zip(result['entries'],metadata):
            batch=revised[entry['batch_id']];old=batch['conversation_groups'][entry['group_index']]
            # Preserve identity, exact audio/deletion boundaries and continuation metadata.
            changed=dict(old,clean_fragment=new['clean_fragment'],distilled_fragment=new['distilled_fragment'],
                clean_rendered=new['clean_rendered'],distilled_rendered=new['distilled_rendered'],reading=new['reading'],
                reading_version=READING_POLICY_VERSION,summary_input=new['summary_input'],reading_units=new['reading_units'],editorial_basis=entry['basis'],editorial_provenance=result['provider'])
            batch['conversation_groups'][entry['group_index']]=changed
    for batch in revised.values():
        terms=batch['output'].get('uncertain_terms',[])
        batch['output']={kind:'\n\n'.join(g[kind+'_rendered'] for g in batch['conversation_groups'] if g[kind+'_rendered']) for kind in ('clean','distilled')}
        batch['output']['uncertain_terms']=terms
        batch['editorial_revision']=READING_POLICY_VERSION
    candidate['revised_batches']=list(revised.values());candidate['ready_for_review']=True
    if save:atomic(candidate_file,candidate)
    return candidate


def manual_template(worker,directory):
    directory=Path(directory);candidate=json.loads((directory/'candidate.json').read_text());batches=json.loads((directory/'before-batches.json').read_text())
    if {batch['id']:content_version(batch) for batch in batches}!=candidate['base_versions']:raise ValueError('manual_snapshot_hash_changed')
    snap=retirement.snapshot(worker.config)
    if snap['generation']!=candidate['generation']:raise ValueError('candidate_generation_changed')
    entries=load_entries(worker,batches,snap)
    suggestions={}
    for result in candidate['results'].values():
        for entry,group in zip(result['entries'],result['value']['groups']):
            value=copy.deepcopy(group);positions={old:new for new,old in enumerate(value['indices'])}
            value['indices']=list(range(len(entry['units'])))
            value['reading']['retained_indices']=[positions[i] for i in value['reading']['retained_indices']]
            value['reading']['excluded']=[dict(item,index=positions[item['index']]) for item in value['reading']['excluded']]
            suggestions[entry['batch_id']+':'+str(entry['group_index'])]={'value':value,'upstream_provider':result['provider']}
    template={'version':1,'generation':candidate['generation'],'policy':candidate['policy'],'entries':[]}
    old={batch['id']:batch for batch in batches}
    for entry in entries:
        key=entry['batch_id']+':'+str(entry['group_index'])
        original=old[entry['batch_id']]['conversation_groups'][entry['group_index']]
        template['entries'].append(dict(entry,key=key,units=[dict(unit,i=i) for i,unit in enumerate(entry['units'])],
            original_clean=original['clean_fragment'],original_distilled=original.get('distilled_fragment'),suggestion=suggestions.get(key)))
    atomic(directory/'manual-template.json',template)
    return template


def import_manual(worker,directory,path):
    from text_output.conversation import validate_groups
    from context_analysis.reading import validate_summary
    directory=Path(directory);candidate=json.loads((directory/'candidate.json').read_text());batches=json.loads((directory/'before-batches.json').read_text())
    template=manual_template(worker,directory);review=json.loads(Path(path).read_text())
    if review.get('provenance')!='codex_reviewed_manual' or review.get('generation')!=candidate['generation'] or review.get('policy')!=candidate['policy']:
        raise ValueError('manual_review_contract_invalid')
    expected={entry['key']:entry for entry in template['entries']}
    if not isinstance(review.get('groups'),dict) or set(review['groups'])!=set(expected):raise ValueError('manual_review_coverage_incomplete')
    original_results=candidate.get('upstream_provider_results',candidate['results'])
    candidate['upstream_provider_results']=original_results
    candidate['results']={}
    for key,entry in expected.items():
        value=review['groups'][key]
        validate_groups({'groups':[value],'uncertain_terms':[]},entry['units'],require_reading=True)
        if value['title']!=entry['title'] or value['indices']!=list(range(len(entry['units']))) or value['continue_previous']:
            raise ValueError('manual_review_boundaries_changed')
        upstream=(entry.get('suggestion') or {}).get('upstream_provider')
        candidate['results']['manual:'+key]={'entries':[entry],'value':{'groups':[value],'uncertain_terms':[]},'units':entry['units'],
            'provider':{'provider':'codex_reviewed_manual','api_request':False,'admission_id':None,'upstream_provider':upstream}}
    candidate['approved_for_apply']=False
    candidate['manual_review_complete']=False
    candidate.pop('manual_overview',None)
    candidate=finish_candidate(candidate,batches,directory/'candidate.json',save=False)
    if review.get('daily_overview'):
        from context_analysis.reading import cards
        all_cards=cards(candidate['revised_batches'])
        kept=[card for card in all_cards if card['reading_role']!='background']
        supplied=review['daily_overview']
        validate_summary(supplied,{card['id'] for card in kept},'\n'.join(card['summary_input'] for card in kept))
        candidate['manual_overview']={'policy':READING_POLICY_VERSION,'output':supplied,'content_versions':{card['id']:card['version'] for card in kept},
            'input_group_ids':[card['id'] for card in kept],'provider':'codex_reviewed_manual','reviewed_at':time.time()}
    candidate['manual_review_complete']=True
    candidate['approved_for_apply']=False
    with retirement.guard(worker.config,candidate['generation']):atomic(directory/'candidate.json',candidate)
    return candidate


def apply(worker,directory):
    directory=Path(directory);candidate=json.loads((directory/'candidate.json').read_text())
    if candidate.get('approved_for_apply') is not True:raise ValueError('candidate_quality_review_required')
    generation=candidate['generation'];date=candidate['date']
    replacements={batch['id']:batch for batch in candidate['revised_batches']}
    identity=digest({'date':date,'generation':generation,'base_versions':candidate['base_versions'],'revised':candidate['revised_batches']})
    journal_file=directory/'apply-intent.json'
    journal=json.loads(journal_file.read_text()) if journal_file.exists() else {'identity':identity,'generation':generation,'cloud_completed':False,'local_completed':False}
    if journal['identity']!=identity:raise ValueError('different_revision_apply_intent')
    with retirement.guard(worker.config,generation):
        current=worker._saved_batches(date);by_id={batch['id']:batch for batch in current}
        for identifier,old_version in candidate['base_versions'].items():
            if identifier not in by_id or content_version(by_id[identifier]) not in (old_version,content_version(replacements[identifier])):
                raise ValueError('candidate_source_changed')
        # Sink state belongs to the live record. A batch may have been published
        # during candidate generation; never replace published/URLs with stale data.
        final=[]
        for batch in current:
            updated=copy.deepcopy(batch)
            if batch['id'] in replacements:
                desired=replacements[batch['id']]
                updated.update(output=desired['output'],conversation_groups=desired['conversation_groups'],editorial_revision=READING_POLICY_VERSION)
            final.append(updated)
        atomic(journal_file,journal)
    if worker.publisher:
        worker.publisher.revise_managed_day(date,[b for b in current if b.get('published')],[b for b in final if b.get('published')],directory,
            guard_factory=lambda:retirement.guard(worker.config,generation))
    journal['cloud_completed']=True;atomic(journal_file,journal)
    with retirement.guard(worker.config,generation):
        changes=[]
        for batch in final:
            if batch['id'] not in replacements:continue
            live=worker.state.get('batch:'+batch['id']);version=content_version(live)
            new_version=content_version(replacements[batch['id']])
            if version not in (candidate['base_versions'][batch['id']],new_version):raise ValueError('candidate_source_changed')
            if version==new_version:continue
            live.update(output=batch['output'],conversation_groups=batch['conversation_groups'],editorial_revision=READING_POLICY_VERSION)
            changes.append(('batch:'+batch['id'],json.dumps(live,ensure_ascii=False)))
        # One SQLite transaction avoids partial local versions. Strict old/new
        # acceptance above also recovers a pre-existing partial migration safely.
        with worker.state.db:
            worker.state.db.executemany('INSERT OR REPLACE INTO kv VALUES (?,?)',changes)
        worker.export(date)
        if candidate.get('manual_overview'):
            worker.state.put('manual-day-ready:'+date,candidate['manual_overview'])
    journal['local_completed']=True;atomic(journal_file,journal)
    atomic(directory/'applied.json',{'applied_at':dt.datetime.now(dt.timezone.utc).isoformat(),'generation':generation,'batches':len(replacements)})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--date',required=True);parser.add_argument('--candidate-dir',type=Path,required=True);parser.add_argument('--apply-reviewed',action='store_true');parser.add_argument('--manual-template',action='store_true');parser.add_argument('--import-manual',type=Path);args=parser.parse_args()
    dt.date.fromisoformat(args.date);config=json.loads(args.config.read_text());worker=Worker(config)
    try:
        if args.manual_template:manual_template(worker,args.candidate_dir)
        elif args.import_manual:import_manual(worker,args.candidate_dir,args.import_manual)
        elif args.apply_reviewed:
            with singleton(worker.home/'worker.lock'):apply(worker,args.candidate_dir)
        else:prepare(worker,args.date,args.candidate_dir)
    finally:worker.state.close()

if __name__=='__main__':main()
