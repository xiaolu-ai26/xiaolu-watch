"""CUDA/CPU ONNX speaker extractor and identity sidecars. No cloud client or audio uploads."""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import uuid
from .core import unit, cosine, decide, validate_profile, windows

SOURCE_ID = re.compile(r'[0-9a-f]{64}')

def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()

def snapshot(path, limit):
    path=Path(path)
    if path.is_symlink():raise ValueError('Symlink input forbidden')
    with path.open('rb') as stream:data=stream.read(limit+1)
    if len(data)>limit:raise ValueError('Input snapshot too large')
    return data


def atomic_json(path, value):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Symlink output forbidden')
    temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.pending')
    try:
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

class Extractor:
    def __init__(self, model, expected_sha256, ffmpeg='ffmpeg', provider='cuda', cuda_dll_dirs=(), profile_directory=None):
        self.model = Path(model);self.model_sha256 = sha256(self.model);self.ffmpeg = ffmpeg
        if self.model_sha256 != expected_sha256:
            raise ValueError('Speaker model SHA differs')
        self.dll_handles=[]
        for raw in cuda_dll_dirs:
            directory=Path(raw)
            if not directory.is_absolute() or not directory.is_dir():raise ValueError('CUDA DLL directory must exist and be absolute')
            if os.name=='nt':self.dll_handles.append(os.add_dll_directory(str(directory)))
            os.environ['PATH']=str(directory)+os.pathsep+os.environ.get('PATH','')
        import onnxruntime as ort
        if provider not in ('cuda','cpu'):raise ValueError('Unsupported speaker provider')
        if provider=='cuda' and 'CUDAExecutionProvider' not in ort.get_available_providers():
            raise ValueError('CUDA speaker provider unavailable')
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        self.provider=provider;self.cuda_node_count=0;self.cuda_network_node_count=0;self.cuda_attestation_failed=False
        if provider=='cuda':
            options.enable_profiling=True
            if profile_directory:
                directory=Path(profile_directory);directory.mkdir(parents=True,exist_ok=True)
                options.profile_file_prefix=str(directory/'speaker-ort-profile')
        providers=[('CUDAExecutionProvider',{'device_id':0,'gpu_mem_limit':1024**3,'arena_extend_strategy':'kSameAsRequested',
                    'use_tf32':0,'cudnn_conv_algo_search':'HEURISTIC','cudnn_conv_use_max_workspace':'0'}),'CPUExecutionProvider'] if provider=='cuda' else ['CPUExecutionProvider']
        self.session=ort.InferenceSession(str(self.model),sess_options=options,providers=providers)
        if provider=='cuda' and self.session.get_providers()[0]!='CUDAExecutionProvider':
            raise ValueError('CUDA initialization failed; CPU fallback is not accepted')
        self.session.disable_fallback();self.dim=int(self.session.get_outputs()[0].shape[-1])

    def audio(self, source):
        import numpy as np
        result = subprocess.run([self.ffmpeg, '-nostdin', '-v', 'error', '-i', str(source),
             '-t', '181', '-ar', '16000', '-ac', '1', '-f', 'f32le', 'pipe:1'],
             capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError('Audio decoding failed')
        audio = np.frombuffer(result.stdout, dtype='<f4').copy()
        if not len(audio) or len(audio) > 180*16000 or not np.isfinite(audio).all():
            raise ValueError('Invalid or oversized source audio')
        return audio

    def embedding(self, audio):
        import numpy as np
        import kaldi_native_fbank as knf
        if self.cuda_attestation_failed:raise RuntimeError('CUDA computation attestation previously failed')
        # Match WeSpeaker's official infer_onnx.py: 16k, int16-scale, hamming,
        # 80 log-mel bins, 25/10ms, dither=0, utterance CMN without CVN.
        options=knf.FbankOptions();options.frame_opts.samp_freq=16000
        options.frame_opts.frame_length_ms=25;options.frame_opts.frame_shift_ms=10
        options.frame_opts.dither=0.;options.frame_opts.window_type='hamming'
        options.mel_opts.num_bins=80;options.use_energy=False
        fbank=knf.OnlineFbank(options);fbank.accept_waveform(16000,(np.asarray(audio)*32768).tolist());fbank.input_finished()
        if fbank.num_frames_ready<100:raise ValueError('Insufficient speech frames')
        features=np.stack([fbank.get_frame(i) for i in range(fbank.num_frames_ready)]).astype(np.float32)
        features-=features.mean(axis=0,keepdims=True)
        result=self.session.run(None,{self.session.get_inputs()[0].name:features[None]})[0].reshape(-1)
        if self.provider=='cuda' and not self.cuda_network_node_count:
            self.cuda_attestation_failed=True
            log=Path(self.session.end_profiling())
            try:
                events=json.loads(log.read_text(encoding='utf-8'))
                nodes=[e.get('args',{}) for e in events if e.get('args',{}).get('provider')=='CUDAExecutionProvider' and not str(e.get('args',{}).get('op_name','')).startswith('Memcpy')]
                self.cuda_node_count=len(nodes)
                self.cuda_network_node_count=sum('Conv' in x.get('op_name','') or x.get('op_name') in ('Gemm','MatMul') for x in nodes)
                if not self.cuda_network_node_count:raise RuntimeError('No actual CUDA model computation observed')
                self.cuda_attestation_failed=False
            finally:log.unlink(missing_ok=True)
        return unit(result)

def energy_quality(audio):
    import numpy as np
    if len(audio) < 3*16000:
        return False
    rms = float(np.sqrt(np.mean(np.square(audio))))
    if rms < 10**(-45/20) or float(np.mean(np.abs(audio) >= .999)) > .02:
        return False
    frames = audio[:len(audio)//320*320].reshape(-1,320)
    active = np.sqrt(np.mean(frames*frames,axis=1)) > max(10**(-50/20), rms*.25)
    return float(np.mean(active)) >= .6

def analyze(extractor, profile, source, receipt_path, model_id, model_revision):
    validate_profile(profile, extractor.model_sha256, extractor.dim, model_id, model_revision)
    receipt_path = Path(receipt_path);receipt_bytes = receipt_path.read_bytes();receipt = json.loads(receipt_bytes)
    source_id = receipt.get('source_id')
    if not SOURCE_ID.fullmatch(source_id or '') or sha256(source) != source_id or receipt.get('status') != 'done':
        raise ValueError('Source/ASR identity mismatch')
    audio = extractor.audio(source);duration = len(audio)/16000;segments = receipt.get('segments', [])
    spans = []
    for index, segment in enumerate(segments):
        start, end = float(segment['start']),float(segment['end'])
        if not math.isfinite(start+end) or start < 0 or end <= start or end > duration+.05:
            raise ValueError('ASR span outside decoded source')
        spans.append((index,start,min(end,duration),bool(segment.get('overlap',False))))
    turns = []
    for index,start,end,reported_overlap in spans:
        overlap = reported_overlap or any(i != index and max(start,a) < min(end,b)-.05 for i,a,b,_ in spans)
        for a,b in windows(start,end):
            clip = audio[round(a*16000):round(b*16000)];score=None;support=[];consistency=float('nan');quality=energy_quality(clip)
            if b-a >= 3 and quality and not overlap:
                try:
                    embedding = extractor.embedding(clip);left=extractor.embedding(clip[:3*16000]);right=extractor.embedding(clip[-3*16000:])
                    score=cosine(embedding,profile['centroid']);support=[cosine(left,profile['centroid']),cosine(right,profile['centroid'])];consistency=cosine(left,right)
                except ValueError:
                    pass
            label,reason=decide(score,support,consistency,b-a,quality,profile['thresholds'],overlap)
            turns.append({'asr_segment_index':index,'start':round(a,6),'end':round(b,6),'label':label,
                          'score':round(score,6) if score is not None else None,'confidence_reason':reason})
    if sha256(source) != source_id or hashlib.sha256(receipt_path.read_bytes()).digest() != hashlib.sha256(receipt_bytes).digest():
        raise ValueError('Source/ASR changed during analysis')
    return {'version':1,'status':'done','source_id':source_id,'audio_sha256':source_id,
            'asr_receipt_sha256':hashlib.sha256(receipt_bytes).hexdigest(),'time_basis':'source_audio_seconds',
            'model_id':model_id,'model_revision':model_revision,'model_sha256':extractor.model_sha256,
            'profile_version':profile['profile_version'],'turns':turns,'provider_requested':extractor.provider,'provider':extractor.provider if extractor.provider=='cpu' or extractor.cuda_network_node_count else 'not_invoked','cuda_inference_node_count':extractor.cuda_node_count,'cuda_network_node_count':extractor.cuda_network_node_count,
            'score_semantics':'cosine_similarity_not_probability_or_accuracy',
            'overlap_detection':'subwindow_consistency_guard_only_not_guaranteed_separation',
            'completed_at':dt.datetime.now(dt.timezone.utc).isoformat()}
