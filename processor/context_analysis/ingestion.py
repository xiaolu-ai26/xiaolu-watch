"""Private queue inventory projected as counts and audio-event coverage only."""
import datetime as dt
import json
import math
from pathlib import Path
from text_output.core import discover,digest,batch_identity
from text_output.conversation import GROUP_SCHEMA
from .reading import iso


def progress(config,state,date,capture_floor,retired=()):
    groups=discover(config['asr_outbox'],config['receiver_sessions'],capture_since=capture_floor,
        derived_asr_root=config.get('derived_asr_outbox'),derived_asr_model=config.get('derived_asr_model'),retired_source_ids=retired)
    ready={r.source_id:r for (day,_),slots in groups.items() if day==date for r in slots.values()
           if r is not None and r.asr_model not in ('source_retired','capture_excluded')}
    records={}
    for path in sorted(Path(config['receiver_sessions']).glob('*/*.json')):
        # discover validates these same committed metadata paths before projection.
        m=json.loads(path.read_text(encoding='utf-8'))['metadata']
        if m.get('is_test') is not False or m.get('capture_date')!=date or m['sha256'] in retired:continue
        start=dt.datetime.fromisoformat(m['capture_started_at'].replace('Z','+00:00')).timestamp();duration=m['duration_s']
        if capture_floor is not None and start<capture_floor:continue
        if type(duration) not in (int,float) or not math.isfinite(duration) or not 0<=duration<=86400:raise ValueError('invalid progress duration')
        records[m['sha256']]={'start':start,'end':start+duration}
    batches=[b for _,b in state.items('batch:') if b['date']==date]
    done={s['source_id'] for b in batches if b.get('output') or b.get('no_speech') for s in b.get('sources',[])}
    queued={sid for sid,row in ready.items() if sid in records and sid not in done and row.text.strip()}
    pending_asr=set(records)-set(ready)
    frontier=None
    for sid,record in sorted(records.items(),key=lambda x:(x[1]['start'],x[0])):
        if sid not in done and (sid not in ready or ready[sid].text.strip()):break
        frontier=record['end'] if frontier is None else max(frontier,record['end'])
    def current_batch(batch):
        sources=batch.get('sources',[])
        if not sources or any(s['source_id'] not in ready or s['source_id'] in done for s in sources):return False
        rows=[ready[s['source_id']] for s in sources]
        identity=batch_identity(rows)
        if config.get('conversation_mode',False):identity=digest({'source_batch':identity,'schema':GROUP_SCHEMA})
        return batch.get('id')==identity
    budget_wait=any(b.get('last_error')=='daily_text_token_budget_reached' and not b.get('output') and not b.get('superseded_by')
                    and queued.intersection(s['source_id'] for s in b.get('sources',[])) and current_batch(b) for b in batches)
    status='empty' if not records else 'budget_wait' if queued and budget_wait else 'processing' if queued or pending_asr else 'up_to_date'
    return {'state':status,'latest_recorded_at':iso(max((r['end'] for r in records.values()),default=None)),
        'latest_transcribed_at':iso(max((records[sid]['end'] for sid in ready if sid in records),default=None)),
        'latest_organized_at':iso(frontier),'received_count':len(records),'transcribed_count':len(set(ready)&set(records)),
        'queued_text_count':len(queued),'queued_asr_count':len(pending_asr)}
