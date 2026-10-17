#!/usr/bin/env python3
"""Independent local speaker verification; quality model and CUDA selected by config. Never publishes or sends embeddings."""
import argparse
import datetime as dt
import json
import hashlib
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
from speaker_identity.runtime import Extractor, analyze, atomic_json, sha256, SOURCE_ID, snapshot
from speaker_identity.enrollment import enroll
from speaker_identity.resources import gpu_slot,asr_ready_first

EXTENSIONS={'.m4a','.wav','.caf','.mp3','.flac','.aac','.ogg','.opus'}

def timestamp(value):
    stamp=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('Capture cutoff requires timezone')
    return stamp.timestamp()

class Worker:
    def __init__(self,config):
        self.config=config;self.cutoff=timestamp(config['capture_since']);self.extractor=None
        self.home=Path(config['state_dir']);self.home.mkdir(parents=True,exist_ok=True)
    def get_extractor(self):
        if self.extractor is None:
            self.extractor=Extractor(self.config['model_path'],self.config['model_sha256'],self.config.get('ffmpeg','ffmpeg'),provider=self.config.get('provider','cuda'),cuda_dll_dirs=self.config.get('cuda_dll_dirs',()),profile_directory=Path(self.config['profile_path']).parent)
        return self.extractor
    def tick(self):
        cfg=self.config;count=failed=excluded=0
        profile_path=Path(cfg['profile_path'])
        if not profile_path.is_file():
            result={'status':'waiting_for_profile','completed':0,'failed':0}
            atomic_json(self.home/'status.json',result);return result
        profile_bytes=snapshot(profile_path,1024*1024);profile=json.loads(profile_bytes);profile_hash=hashlib.sha256(profile_bytes).hexdigest()
        if cfg.get('profile_sha256') and profile_hash!=cfg['profile_sha256']:raise ValueError('Unexpected speaker profile bytes')
        if profile.get('model_id')!=cfg['model_id'] or profile.get('model_revision')!=cfg['model_revision']:raise ValueError('Profile model revision differs')
        if profile.get('profile_version')!=cfg.get('profile_version'):
            raise ValueError('Unexpected speaker profile version')
        for path in sorted(Path(cfg['receiver_sessions']).glob('*/*.json')):
            if path.is_symlink() or path.stat().st_size>256*1024:raise ValueError('Invalid receiver metadata')
            metadata=json.loads(path.read_text(encoding='utf-8'))['metadata']
            if metadata.get('is_test') is not False:continue
            if timestamp(metadata['capture_started_at'])<self.cutoff:excluded+=1;continue
            source_id=metadata['sha256']
            if not SOURCE_ID.fullmatch(source_id):raise ValueError('Invalid source id')
            receipt=Path(cfg['asr_outbox'])/source_id/'receipt.json'
            if not receipt.is_file():continue
            if receipt.is_symlink() or receipt.stat().st_size>4*1024**2:raise ValueError('Invalid ASR receipt')
            raw=json.loads(receipt.read_text(encoding='utf-8'))
            if raw.get('status')!='done':continue
            if raw.get('source_id')!=source_id or raw.get('is_test') is not False or raw.get('capture_date')!=metadata['capture_date']:
                raise ValueError('ASR lineage differs')
            output=Path(cfg['outbox'])/source_id/'identity.json'
            if output.is_file():
                old=json.loads(output.read_text(encoding='utf-8'))
                if old.get('status')=='done' and old.get('audio_sha256')==source_id and old.get('asr_receipt_sha256')==sha256(receipt) and old.get('profile_version')==profile['profile_version'] and old.get('profile_sha256')==profile_hash and old.get('model_id')==cfg['model_id'] and old.get('model_sha256')==cfg['model_sha256'] and old.get('model_revision')==cfg['model_revision']:
                    continue
            sources=[p for p in Path(cfg['archive_root']).glob(source_id+'.*') if p.suffix.lower() in EXTENSIONS and p.is_file() and not p.is_symlink()]
            if len(sources)!=1:continue
            try:
                if asr_ready_first(cfg):continue
                with gpu_slot(cfg.get('asr_gpu_lock')) as available:
                    if not available:continue
                    result=analyze(self.get_extractor(),profile,sources[0],receipt,cfg['model_id'],cfg['model_revision'])
                if hashlib.sha256(snapshot(profile_path,1024*1024)).hexdigest()!=profile_hash:raise ValueError('Profile changed during analysis')
                result['profile_sha256']=profile_hash
                atomic_json(output,result);count+=1
            except Exception as error:
                failed+=1;atomic_json(output,{'version':1,'status':'failed','source_id':source_id,'audio_sha256':source_id,'turns':[],
                    'error_type':type(error).__name__,'profile_version':profile['profile_version'],'model_revision':cfg['model_revision']})
        result={'status':'ok' if not failed else 'degraded','completed_this_tick':count,'failed_this_tick':failed,
                'excluded_pre_cutoff':excluded,'capture_since':cfg['capture_since'],'profile_version':profile['profile_version'],
                'model_revision':cfg['model_revision'],'provider_requested':cfg.get('provider','cuda'),'provider':self.extractor.provider if self.extractor and self.extractor.cuda_network_node_count else 'not_invoked','checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        atomic_json(self.home/'status.json',result);return result

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True);parser.add_argument('--once',action='store_true');parser.add_argument('--enroll-plan',type=Path)
    args=parser.parse_args();config=json.loads(args.config.read_text(encoding='utf-8'));worker=Worker(config)
    if args.enroll_plan:
        plan=json.loads(args.enroll_plan.read_text(encoding='utf-8'))
        with gpu_slot(config.get('asr_gpu_lock')) as available:
            if not available or asr_ready_first(config):raise RuntimeError('ASR has GPU priority; retry enrollment later')
            result=enroll(worker.get_extractor(),plan,config['profile_path'],config['model_id'],config['model_revision'])
        print(json.dumps(result));return
    # One worker only: the OS releases this advisory lock after termination.
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'receiver'))
    from platform_store import lock_exclusive
    with (worker.home/'worker.lock').open('a+') as lock:
        lock_exclusive(lock)
        while True:
            try:worker.tick()
            except Exception as error:atomic_json(worker.home/'status.json',{'status':'failed','error_type':type(error).__name__,'checked_at':dt.datetime.now(dt.timezone.utc).isoformat()})
            if args.once:break
            time.sleep(max(5,int(config.get('poll_seconds',10))))
if __name__=='__main__':main()
