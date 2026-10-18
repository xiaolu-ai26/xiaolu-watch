"""Generation-bound card deletion plans. Paths come only from trusted configuration."""
import datetime as dt
import hashlib
import hmac
import json
import re
import time
import uuid
from pathlib import Path
from context_store import StoreError, canonical, digest, identifier, now

CARD=re.compile(r'[A-Za-z0-9_-]{1,128}')
SHA=re.compile(r'[0-9a-f]{64}')

def bounded_json(path,limit=8*1024*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:raise StoreError('projection_unavailable',503)
    data=path.read_bytes()
    if len(data)>limit:raise StoreError('projection_unavailable',503)
    try:return json.loads(data.decode('utf-8'))
    except (ValueError,UnicodeError):raise StoreError('projection_unavailable',503)

def intervals(spans):
    result=[]
    for span in spans:
        if span.get('precision')!='asr_word_span' or span.get('overlap') is not False:return None
        a,b=span.get('start'),span.get('end')
        if type(a) not in (int,float) or type(b) not in (int,float) or not 0<=a<b<=600:return None
        if not __import__('math').isfinite(a+b):return None
        result.append([a,b])
    return result

class Deletions:
    def __init__(self,store,reading_root):self.store=store;self.reading_root=Path(reading_root)
    def snapshot(self,card_id):
        if not isinstance(card_id,str) or not CARD.fullmatch(card_id):raise StoreError('invalid_card_id')
        public=[];private=[]
        for path in sorted(self.reading_root.glob('????-??-??.json')):
            day=bounded_json(path);side=bounded_json(path.with_name(path.stem+'.sources.json'))
            if day.get('revision')!=side.get('revision'):raise StoreError('projection_updating',409)
            public.extend(day.get('cards',[]));private.extend(side.get('cards',[]))
        targets=[c for c in public if c.get('id')==card_id];sources=[c for c in private if c.get('id')==card_id]
        if len(targets)!=1 or len(sources)!=1:raise StoreError('card_not_found',404)
        target=targets[0];source=sources[0]
        if target.get('version')!=source.get('version'):raise StoreError('projection_updating',409)
        spans=source.get('source_spans',[])
        if not isinstance(spans,list) or any(not isinstance(s,dict) or not SHA.fullmatch(str(s.get('source_id',''))) for s in spans):raise StoreError('invalid_source_projection',503)
        ids=sorted(set(s['source_id'] for s in spans))
        relevant=[c for c in private if any(s.get('source_id') in ids for s in c.get('source_spans',[]))]
        safe=True;assets=[]
        for sid in ids:
            chosen=[s for s in spans if s['source_id']==sid]
            others=[s for c in relevant if c['id']!=card_id for s in c.get('source_spans',[]) if s['source_id']==sid]
            selected=intervals(chosen);protected=intervals(others)
            if selected is None or protected is None or any(max(a,c)<min(b,d) for a,b in selected for c,d in protected):safe=False
            assets.append({'source_id':sid,'delete_intervals':selected or [],'protected_intervals':protected or [],'boundaries_verified':selected is not None and protected is not None})
        # Bind complete relevant cards and current asset revisions, not unrelated daily text changes.
        mappings={sid:self.store.retirement_for(sid) for sid in ids}
        if any(value and value['state']!='retired' for value in mappings.values()):raise StoreError('source_deletion_busy',409)
        binding={'card':source,'related':sorted(relevant,key=lambda c:c['id']),'assets':mappings}
        return {'target':target,'private':source,'binding_hash':digest(binding),'sources':assets,'related_cards':sorted(set(c['id'] for c in relevant if c['id']!=card_id)), 'audio_status':'no_audio' if not ids else 'deletable' if safe else 'shared_audio_pending'}
    def plan(self,principal,card_id,body):
        if not isinstance(body,dict) or set(body)!={'request_id','expected_card_version'} or type(body['expected_card_version']) is not int or body['expected_card_version']<1:raise StoreError('invalid_plan')
        with self.store.generation_guard() as guard:
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE');prior=self.store.idempotent(c,principal,'plan:'+card_id,body.get('request_id'),body)
                if prior:return prior
                if card_id in guard['group_ids']:raise StoreError('card_retired',410)
                snapshot=self.snapshot(card_id)
                if snapshot['target']['version']!=body['expected_card_version']:raise StoreError('card_version_changed',409)
                plan_id=str(uuid.uuid4());expiry=time.time()+600
                token=hmac.new(self.store.master,b'phone-delete-plan-v1\0'+principal.encode()+plan_id.encode()+snapshot['binding_hash'].encode(),hashlib.sha256).hexdigest()
                record={'principal':principal,'generation':guard['generation'],'snapshot':snapshot,'token_hash':digest(token.encode()),'card_id':card_id,'card_version':body['expected_card_version']}
                response={'version':3,'plan_id':plan_id,'plan_revision':1,'card_id':card_id,'card_version':body['expected_card_version'],'expires_at':dt.datetime.fromtimestamp(expiry,dt.timezone.utc).isoformat(),'confirmation_token':token,'affected_cards':[{'id':card_id,'impact':'delete'}]+[{'id':v,'impact':'retain'} for v in snapshot['related_cards']],'audio_status':snapshot['audio_status'],'warnings':['Shared audio boundaries are uncertain; retained audio will not be removed.'] if snapshot['audio_status']=='shared_audio_pending' else []}
                c.execute('INSERT INTO deletion_plans VALUES(?,?,?,?,?)',(plan_id,principal,canonical(record),expiry,'planned'));self.store.save_request(c,principal,'plan:'+card_id,body['request_id'],body,response);return response
    def commit(self,principal,plan_id,body):
        if not identifier(plan_id) or not isinstance(body,dict) or set(body)!={'request_id','plan_revision','confirmation_token'} or type(body['plan_revision']) is not int or body['plan_revision']!=1 or not isinstance(body['confirmation_token'],str):raise StoreError('invalid_commit')
        with self.store.generation_guard() as guard:
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE');prior=self.store.idempotent(c,principal,'commit:'+plan_id,body.get('request_id'),body)
                if prior:return self.status(principal,plan_id,c)
                row=c.execute('SELECT body,expires,state FROM deletion_plans WHERE id=? AND principal=?',(plan_id,principal)).fetchone()
                if not row:raise StoreError('plan_not_found',404)
                record=json.loads(row[0])
                if not hmac.compare_digest(record['token_hash'],digest(body['confirmation_token'].encode())):raise StoreError('invalid_confirmation',403)
                if row[2]=='committed':return self.status(principal,plan_id,c)
                if row[1]<=time.time():raise StoreError('plan_expired',410)
                if guard['generation']!=record['generation'] or self.snapshot(record['card_id'])['binding_hash']!=record['snapshot']['binding_hash']:raise StoreError('plan_stale',409)
                generation=guard['generation']+1;snapshot=record['snapshot']
                tx={'id':plan_id,'principal':principal,'card_id':record['card_id'],'group_ids':[snapshot['private'].get('group_id',record['card_id'])],'generation':generation,'sources':snapshot['sources'],'retained_other_cards':snapshot['related_cards'],'audio_status':snapshot['audio_status'],'warnings':[],'created_at':now()}
                c.execute("UPDATE meta SET v=? WHERE k='generation'",(generation,))
                c.execute('INSERT INTO card_tombstones VALUES(?,?,?,?,?)',(tx['group_ids'][0],generation,record['card_version'],plan_id,now()))
                for source in snapshot['sources']:
                    c.execute('INSERT INTO source_tombstones VALUES(?,?,?, ?,1) ON CONFLICT(source_id) DO UPDATE SET transaction_id=excluded.transaction_id,state=excluded.state,revision=source_tombstones.revision+1',(source['source_id'],plan_id,'pending',canonical((self.store.retirement_for(source['source_id']) or {}).get('retained_assets',[]))))
                c.execute('INSERT INTO deletion_transactions VALUES(?,?,?,?,?,?)',(plan_id,record['card_id'],generation,'pending',canonical(tx),now()))
                c.execute("UPDATE deletion_plans SET state='committed' WHERE id=?",(plan_id,))
                response=self.status(principal,plan_id,c);self.store.save_request(c,principal,'commit:'+plan_id,body['request_id'],body,response);return response
    def status(self,principal,txid,c=None):
        if c is None:
            with self.store.connect() as db:return self.status(principal,txid,db)
        row=c.execute('SELECT state,body FROM deletion_transactions WHERE id=?',(txid,)).fetchone()
        if not row:raise StoreError('deletion_not_found',404)
        tx=json.loads(row[1])
        if tx['principal']!=principal:raise StoreError('deletion_not_found',404)
        return {'version':3,'deletion_id':txid,'card_id':tx['card_id'],'state':row[0],'audio_status':tx['audio_status'],'retryable':row[0]!='completed','warnings':tx.get('warnings',[]),'retained_other_cards':tx['retained_other_cards']}
