"""Recoverable source retirement and sample-exact retained assets.

Only configured managed roots are touched. Original SHA identities never change.
Unknown boundaries stay pending. No public path or arbitrary filesystem argument.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from context_store import atomic_json, canonical, now, StoreError
from deletion import bounded_json,SHA
from platform_store import replace,sync_directory

RATE=16000
MAX_PCM=RATE*600*4

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def trusted(path,anchor):
    path=Path(path);anchor=Path(anchor).absolute()
    if not path.is_absolute():path=path.absolute()
    try:relative=path.relative_to(anchor)
    except ValueError:raise StoreError('artifact_scope_conflict',409)
    current=anchor
    if current.is_symlink():raise StoreError('artifact_symlink_conflict',409)
    for part in relative.parts:
        current=current/part
        if current.is_symlink():raise StoreError('artifact_symlink_conflict',409)
    if path.resolve()!=path.absolute():raise StoreError('artifact_scope_conflict',409)
    return path

def merge(ranges):
    result=[]
    for a,b in sorted(ranges):
        if a>=b:continue
        if result and a<=result[-1][1]:result[-1][1]=max(result[-1][1],b)
        else:result.append([a,b])
    return result

def complement(count,removed):
    keep=[];cursor=0
    for a,b in merge(removed):
        if cursor<a:keep.append([cursor,a])
        cursor=max(cursor,b)
    if cursor<count:keep.append([cursor,count])
    return keep

class AudioRetirement:
    def __init__(self,store,config):
        self.store=store;self.config=config;self.root=store.root.resolve()/'deletion';self.root.mkdir(exist_ok=True,mode=0o700)
        self.assets=self.root/'assets';self.assets.mkdir(exist_ok=True,mode=0o700)
        self.ffmpeg=config['ffmpeg_path'];self.roots=[Path(p).resolve() for p in config['managed_audio_roots']];self.asr_roots=[Path(p).resolve() for p in config.get('managed_asr_roots',[])];self.receipt_roots=[Path(p).resolve() for p in config.get('managed_receipt_roots',[])]
    def decode(self,path):
        out=subprocess.run([self.ffmpeg,'-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(path),'-ar',str(RATE),'-ac','1','-f','f32le','pipe:1'],capture_output=True,timeout=60)
        if out.returncode or not out.stdout or len(out.stdout)>MAX_PCM or len(out.stdout)%4:raise StoreError('audio_decode_failed',409)
        return out.stdout
    def asset(self,pcm):
        temp=self.assets/('.'+uuid.uuid4().hex+'.wav')
        try:
            out=subprocess.run([self.ffmpeg,'-nostdin','-v','error','-f','f32le','-ar',str(RATE),'-ac','1','-i','pipe:0','-c:a','pcm_f32le','-f','wav',str(temp)],input=pcm,capture_output=True,timeout=60)
            if out.returncode or self.decode(temp)!=pcm:raise StoreError('retained_audio_verification_failed',409)
            with temp.open('rb') as f:os.fsync(f.fileno())
            key=sha(temp);target=self.assets/(key+'.wav')
            if target.exists():
                trusted(target,self.assets)
                if sha(target)!=key:raise StoreError('retained_asset_conflict',409)
            else:replace(temp,target);sync_directory(self.assets)
            return key
        finally:temp.unlink(missing_ok=True)
    def originals(self,sid):
        found=[]
        for root in self.roots:
            trusted(root,root)
            for ext in ('.caf','.m4a','.wav','.flac','.mp3','.aac','.opus','.ogg'):
                p=trusted(root/(sid+ext),root)
                if p.exists():
                    if not p.is_file() or sha(p)!=sid:raise StoreError('original_hash_conflict',409)
                    found.append(p)
        return found
    def retire_file(self,path,trash,expected=None):
        """Verified copy-before-unlink; journaled destination makes retries recoverable."""
        if path.exists():
            if path.is_symlink() or not path.is_file():raise StoreError('artifact_scope_conflict',409)
            before=path.stat();key=sha(path)
            if expected and key!=expected:raise StoreError('artifact_changed',409)
            if trash.exists():
                if trash.is_symlink() or sha(trash)!=key:raise StoreError('trash_conflict',409)
            else:
                temporary=trash.with_suffix(trash.suffix+'.part')
                with path.open('rb') as src,temporary.open('wb') as dst:
                    shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
                if sha(temporary)!=key:raise StoreError('trash_verification_failed',409)
                replace(temporary,trash);sync_directory(trash.parent)
            after=path.stat()
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or sha(path)!=key:raise StoreError('artifact_changed',409)
            path.unlink();sync_directory(path.parent)
        elif not trash.is_file() or (expected and sha(trash)!=expected):raise StoreError('missing_retirement_artifact',409)
    def stage_source(self,tx,source):
        sid=source['source_id'];folder=self.root/tx['id']/sid;folder.mkdir(parents=True,exist_ok=True,mode=0o700);journal=folder/'audio-plan.json'
        if journal.exists():return bounded_json(journal)
        old=self.store.retirement_for(sid);mapping=old['retained_assets'] if old else []
        files=self.originals(sid)
        if mapping:
            inputs=[]
            for item in mapping:
                asset=trusted(self.assets/(item['asset_id']+'.wav'),self.assets)
                if not SHA.fullmatch(item['asset_id']) or sha(asset)!=item['asset_id']:raise StoreError('retained_asset_conflict',409)
                inputs.append((item['original_start_sample'],item['original_end_sample'],asset))
        elif files:
            count=len(self.decode(files[0]))//4;inputs=[(0,count,files[0])]
        else:raise StoreError('original_audio_unavailable',409)
        deleted=merge([[math.ceil(a*RATE),math.floor(b*RATE)] for a,b in source['delete_intervals']]);new=[]
        for begin,end,path in inputs:
            pcm=self.decode(path)
            if len(pcm)//4!=end-begin:raise StoreError('asset_mapping_conflict',409)
            cuts=[[max(0,a-begin),min(end-begin,b-begin)] for a,b in deleted if a<end and b>begin]
            for a,b in complement(end-begin,cuts):
                part=pcm[a*4:b*4];asset_id=self.asset(part)
                new.append({'asset_id':asset_id,'original_start_sample':begin+a,'original_end_sample':begin+b,'sample_rate_hz':RATE,'asset_start_sample':0,'pcm_sha256':hashlib.sha256(part).hexdigest()})
        operations=[{'path':str(p),'trash':str(folder/('audio-'+str(i)+'-'+p.name)),'sha256':sid} for i,p in enumerate(files)]
        # Old residual assets can contain the newly deleted card. Retire those too;
        # retain newly generated content-addressed assets before touching any input.
        for i,(_,_,path) in enumerate(inputs):
            if path.parent==self.assets and path.stem not in {m['asset_id'] for m in new}:operations.append({'path':str(path),'trash':str(folder/('residual-'+str(i)+'-'+path.name)),'sha256':path.stem})
        result={'version':1,'source_id':sid,'retained_assets':new,'operations':operations,'prepared_at':now()};atomic_json(journal,result);return result
    def transcript(self,tx,source,plan):
        """Archive original text; create a separate retained-word projection for B.

        A coarse ASR segment is never copied if it intersects removed speech.
        These derived files are local recovery assets, not rediscovered sources.
        """
        sid=source['source_id'];folder=self.root/tx['id']/sid;manifest=folder/'transcript-plan.json'
        if manifest.exists():operations=bounded_json(manifest)['operations']
        else:
            operations=[];keep=plan['retained_assets'];retained=[]
            for index,root in enumerate(self.asr_roots):
                target=trusted(root/'outbox'/sid,root)
                receipt=target/'receipt.json'
                if receipt.exists():
                    value=bounded_json(receipt)
                    if value.get('source_id')!=sid:raise StoreError('transcript_identity_conflict',409)
                    for segment in value.get('segments',[]):
                        words=segment.get('words',[])
                        if words:
                            for word in words:
                                a,b=word.get('start'),word.get('end')
                                if type(a) not in (int,float) or type(b) not in (int,float):continue
                                if any(m['original_start_sample']<=a*RATE and b*RATE<=m['original_end_sample'] for m in keep):retained.append(dict(word))
                        elif any(m['original_start_sample']<=segment.get('start',-1)*RATE and segment.get('end',1e9)*RATE<=m['original_end_sample'] for m in keep):retained.append({k:segment[k] for k in ('start','end','text')})
                for p in sorted(target.glob('*')) if target.is_dir() else []:
                    trusted(p,root)
                    if not p.is_file():raise StoreError('transcript_scope_conflict',409)
                    operations.append({'path':str(p),'trash':str(folder/('asr-'+str(index)+'-'+p.name)),'sha256':sha(p)})
                state=trusted(root/'state'/(sid+'.json'),root)
                if state.exists():operations.append({'path':str(state),'trash':str(folder/('asr-state-'+str(index)+'.json')),'sha256':sha(state)})
            # Local pipeline copies full ASR text into receipts/<date>/<SHA>.
            # Retire exactly these three owned files; keep client-receipt.json,
            # which is historical processing/ACK metadata without transcript text.
            for index,root in enumerate(self.receipt_roots):
                for day in sorted(root.glob('????-??-??')):
                    trusted(day,root)
                    try:__import__('datetime').date.fromisoformat(day.name)
                    except ValueError:continue
                    target=trusted(day/sid,root)
                    receipt=target/'receipt.json'
                    if receipt.exists() and bounded_json(receipt).get('source_id')!=sid:raise StoreError('transcript_identity_conflict',409)
                    for name in ('receipt.json','transcript.json','transcript.md'):
                        path=trusted(target/name,root)
                        if path.exists():
                            if not path.is_file():raise StoreError('transcript_scope_conflict',409)
                            operations.append({'path':str(path),'trash':str(folder/('receipt-copy-'+str(index)+'-'+day.name+'-'+name)),'sha256':sha(path)})
            atomic_json(folder/'retained-transcript.json',{'version':1,'original_source_id':sid,'retained_assets':keep,'words':retained,'publication':False})
            atomic_json(manifest,{'operations':operations})
        for op in operations:self.retire_file(Path(op['path']),Path(op['trash']),op['sha256'])
    def advance(self,txid):
        # Serializes publication, another source deletion, and receiver restore.
        with self.store.generation_guard(timeout=30):
            with self.store.connect() as c:
                row=c.execute('SELECT body,state FROM deletion_transactions WHERE id=?',(txid,)).fetchone()
            if not row:return
            tx=json.loads(row[0])
            if row[1]=='completed':return
            try:
                if tx['audio_status']=='shared_audio_pending':
                    self.finish(tx,'shared_audio_pending');return
                for source in tx['sources']:
                    marker=self.root/tx['id']/source['source_id']/'source-complete.json'
                    if marker.exists():
                        proof=bounded_json(marker)
                        if proof.get('transaction_id')!=txid or proof.get('source_id')!=source['source_id']:raise StoreError('source_journal_conflict',409)
                        with self.store.connect() as c:c.execute("UPDATE source_tombstones SET state='retired' WHERE source_id=? AND transaction_id=?",(source['source_id'],txid))
                        continue
                    current=self.store.retirement_for(source['source_id'])
                    if not current or current['deletion_id']!=txid:raise StoreError('source_deletion_ownership_changed',409)
                    plan=self.stage_source(tx,source)
                    # Durable asset mapping is published before original retirement.
                    with self.store.connect() as c:c.execute('UPDATE source_tombstones SET mapping=?,state=? WHERE source_id=?',(canonical(plan['retained_assets']),'retiring',source['source_id']))
                    for op in plan['operations']:
                        # Residual WAVs are deduplicated across original sources.
                        # Publish this source's new mapping first, then protect every
                        # live reference under the same cross-process generation lock.
                        path=Path(op['path'])
                        if path.parent==self.assets:
                            with self.store.connect() as c:references=[json.loads(r[0]) for r in c.execute('SELECT mapping FROM source_tombstones')]
                            if any(asset.get('asset_id')==path.stem for mapping in references for asset in mapping):continue
                        anchor=next((r for r in self.roots+[self.assets] if path.is_relative_to(r)),None)
                        if anchor is None:raise StoreError('artifact_scope_conflict',409)
                        trusted(path,anchor);self.retire_file(path,Path(op['trash']),op['sha256'])
                    self.transcript(tx,source,plan)
                    atomic_json(marker,{'version':1,'source_id':source['source_id'],'transaction_id':txid,'finished_at':now()})
                    with self.store.connect() as c:c.execute("UPDATE source_tombstones SET state='retired' WHERE source_id=?",(source['source_id'],))
                self.store.report_derivative_stage(txid,'transcript','done',{'originals_in_recoverable_trash':True})
                self.finish(tx,'running')
            except (OSError,ValueError,subprocess.SubprocessError) as error:
                tx['warnings']=[getattr(error,'code','audio_retirement_retry_required')];self.finish(tx,'retryable_failed')
    def finish(self,tx,state):
        with self.store.connect() as c:
            stages=dict(c.execute('SELECT stage,status FROM derivative_stages WHERE transaction_id=?',(tx['id'],)))
            required=('transcript','processed_text','notes','cloud','summary','projection')
            if state=='running' and all(stages.get(k)=='done' for k in required):state='completed'
            c.execute('UPDATE deletion_transactions SET state=?,body=?,updated=? WHERE id=?',(state,canonical(tx),now(),tx['id']))
