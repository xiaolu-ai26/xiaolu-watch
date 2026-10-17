"""Explicit, locally authorized enrollment. Never elect the largest speaker cluster."""
import datetime as dt
import json
import hashlib
import uuid
from pathlib import Path
from .core import cosine, unit
from .runtime import sha256, energy_quality, atomic_json, snapshot

def coverage(start, end, segments):
    ranges=sorted((max(start,float(s['start'])),min(end,float(s['end']))) for s in segments if min(end,float(s['end']))>max(start,float(s['start'])))
    total=0.;last=start
    for a,b in ranges:
        total+=max(0.,b-max(a,last));last=max(last,b)
    return total/(end-start)

def enroll(extractor, plan, profile_path, model_id, model_revision):
    if plan.get('authorization') != 'user_confirmed_seed':
        raise ValueError('Enrollment requires an explicitly user-confirmed seed')
    source=Path(plan['source_path']);source_id=plan['source_id']
    if sha256(source)!=source_id:
        raise ValueError('Enrollment source identity differs')
    receipt_path=Path(plan['asr_receipt']);receipt_bytes=snapshot(receipt_path,4*1024**2);receipt=json.loads(receipt_bytes)
    if receipt.get('source_id')!=source_id or receipt.get('status')!='done':
        raise ValueError('Enrollment ASR identity differs')
    audio=extractor.audio(source);duration=len(audio)/16000;segments=receipt.get('segments',[])
    candidates=plan['enrollment_windows'];holdouts=plan['holdout_windows']
    if not 3<=len(candidates)<=5 or not holdouts:
        raise ValueError('Enrollment requires 3–5 seed windows and a separate holdout')
    all_windows=[]
    for start,end in candidates+holdouts:
        if start<0 or end>duration or end-start<3 or end-start>6 or coverage(start,end,segments)<.9:
            raise ValueError('Enrollment window lacks confirmed ASR speech coverage')
        if any(max(start,a)<min(end,b) for a,b in all_windows):
            raise ValueError('Enrollment and holdout windows must not overlap')
        all_windows.append((start,end))
    def compute(window):
        start,end=window;clip=audio[round(start*16000):round(end*16000)]
        if not energy_quality(clip):return None
        whole=extractor.embedding(clip);first=extractor.embedding(clip[:3*16000]);last=extractor.embedding(clip[-3*16000:])
        if cosine(first,last)<.60:return None
        return whole
    anchor=compute(candidates[0])
    if anchor is None:
        raise ValueError('Explicit opening seed has insufficient stable voice')
    accepted=[];rejected=[];vectors=[]
    for window in candidates:
        vector=compute(window)
        if vector is None or cosine(vector,anchor)<.65:
            rejected.append(window);continue
        vectors.append(vector);accepted.append(window)
    if len(vectors)<3:
        raise ValueError('Fewer than three consistent windows match the explicit opening seed')
    centroid=unit([sum(items)/len(vectors) for items in zip(*vectors)])
    positives=[]
    for window in holdouts:
        vector=compute(window)
        positives.append({'window':window,'score':cosine(vector,centroid) if vector else None})
    negatives=[]
    for item in plan.get('negative_samples',[]):
        path=Path(item['path'])
        if sha256(path)!=item['sha256']:raise ValueError('Public negative source identity differs')
        other=extractor.audio(path);start,end=item['window'];clip=other[round(start*16000):round(end*16000)]
        if end-start<3 or len(clip)<3*16000 or not energy_quality(clip):
            raise ValueError('Public negative has insufficient speech energy')
        negatives.append({'source_sha256':item['sha256'],'window':item['window'],'score':cosine(extractor.embedding(clip),centroid)})
    valid=[x['score'] for x in positives if x['score'] is not None]
    minimum=min(valid) if valid else -1;maximum=max((x['score'] for x in negatives),default=1)
    self_min=max(.65,min(.85,minimum-.04));other_max=min(.35,maximum+.04)
    ready=len(valid)>=2 and len(negatives)>=1 and minimum>=.65 and maximum<self_min-.15
    profile={'version':1,'profile_version':plan.get('profile_version') or uuid.uuid4().hex,
             'model_id':model_id,'model_revision':model_revision,'model_sha256':extractor.model_sha256,
             'sample_rate':16000,'preprocessing':'ffmpeg mono float32 16k; no enhancement',
             'enrollment_authorization':'user_confirmed_seed','source_id':source_id,
             'asr_receipt_sha256':hashlib.sha256(receipt_bytes).hexdigest(),'enrollment_windows':accepted,'rejected_windows':rejected,
             'holdout_sanity':positives,'public_negative_sanity':negatives,'centroid':centroid,
             'thresholds':{'self_min':self_min,'other_max':other_max,'consistency_min':.60},
             'calibration_ready':ready,'calibration_scope':'same-seed held-out speech + public different-speaker sanity only; not a general accuracy estimate',
             'background_music_limitation':'ASR span coverage, frame energy and embedding consistency are guards, not reliable music/overlap separation',
             'created_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    if sha256(source)!=source_id:raise ValueError('Enrollment source changed')
    if snapshot(receipt_path,4*1024**2)!=receipt_bytes:raise ValueError('Enrollment ASR receipt changed')
    atomic_json(profile_path,profile)
    expected=json.dumps(profile,ensure_ascii=False,indent=2).encode('utf-8')
    if snapshot(profile_path,1024*1024)!=expected:raise ValueError('Profile changed at enrollment commit')
    return {**{k:v for k,v in profile.items() if k!='centroid'},'profile_sha256':hashlib.sha256(expected).hexdigest()}
