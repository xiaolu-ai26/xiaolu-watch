#!/usr/bin/env python3
"""Offline, scoped faster-whisper queue. No network/model downloads; originals are retained.

The model must already exist as a local CTranslate2 directory (model.bin, config.json,
tokenizer.json). `python3 scripts/setup.py` can download one once, before first use.
"""
from __future__ import annotations
import argparse
import contextlib
from datetime import datetime
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading

# Fallback zone only when a file carries no capture_date (pipeline jobs always do).
TZ = datetime.now().astimezone().tzinfo
EXTENSIONS = {'.wav', '.m4a', '.mp3', '.aac', '.flac', '.ogg', '.mp4', '.opus', '.caf'}

def atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)

def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def run(*args):
    timeout = 60 if args[0] == 'ffprobe' else 1800
    return subprocess.run(args, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)

def metadata_for(path, fallback):
    sidecar = path.with_suffix(path.suffix + '.json')
    value = json.loads(sidecar.read_text(encoding='utf-8')) if sidecar.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Audio metadata must be a JSON object')
    capture_date = value.get('capture_date') or fallback or datetime.now(TZ).date().isoformat()
    datetime.strptime(capture_date, '%Y-%m-%d')
    return capture_date, str(value.get('original_name') or path.name)

def load_pcm(wav):
    """16 kHz mono s16le WAV (written by ffmpeg above) -> float32 array for faster-whisper.

    Passing samples avoids faster-whisper's PyAV decoder, whose arguments changed across
    PyAV releases; FFmpeg has already done the decoding and resampling."""
    import wave
    import numpy as np
    with wave.open(str(wav), 'rb') as w:
        if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise ValueError('Expected 16 kHz mono 16-bit PCM from ffmpeg')
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768.0

def markdown(data):
    lines = [f'# 录音转写 {data["capture_date"]}', '', f'来源：{data["original_name"]}',
             f'来源 ID：{data["source_id"]}', '', '以下是机器转写；未识别说话人，内容未经人工核实。', '']
    lines += [f'[{s["start"]:.2f}–{s["end"]:.2f}] {s["text"]}' for s in data['segments']]
    return '\n'.join(lines) + '\n'

class Processor:
    def __init__(self, args):
        self.args = args
        if getattr(args, "language", None) in (None, "auto", ""):
            args.language = None
        self.model = None
        self.language_detection = []

    def get_model(self):
        if self.model is None:
            model = Path(self.args.model)
            for filename in ('model.bin', 'config.json', 'tokenizer.json'):
                if not (model / filename).is_file():
                    raise FileNotFoundError(f'Local model file missing: {model / filename}')
            os.environ['HF_HUB_OFFLINE'] = '1'
            os.environ['TRANSFORMERS_OFFLINE'] = '1'
            os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
            # On Windows, an installed PyTorch bundles cuDNN/cuBLAS DLLs that CTranslate2 can use.
            if os.name == 'nt' and self.args.device == 'cuda':
                import importlib.util
                spec = importlib.util.find_spec('torch')
                if spec and spec.origin:
                    dll_path = str(Path(spec.origin).parent / 'lib')
                    os.environ['PATH'] = dll_path + os.pathsep + os.environ.get('PATH', '')
                    self.dll_handle = os.add_dll_directory(dll_path)
            from faster_whisper import WhisperModel
            self.model = WhisperModel(str(model), device=self.args.device,
                                      compute_type=self.args.compute_type,
                                      local_files_only=True, cpu_threads=getattr(self.args, 'cpu_threads', 4), num_workers=1)
        return self.model

    def transcribe(self, source, workspace):
        duration = float(run('ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'default=noprint_wrappers=1:nokey=1', str(source)).stdout.strip())
        if not duration > 0:
            raise ValueError('Audio duration must be positive')
        segments = []
        self.language_detection = []
        model = self.get_model()
        supports_multilingual = "multilingual" in inspect.signature(model.transcribe).parameters
        use_multilingual = bool(getattr(self.args, "multilingual", True) and self.args.language is None and supports_multilingual)
        # Bounded decoding memory even for a full day recording. 1 s context on each seam;
        # keep segment midpoints within their own window to avoid overlap duplication.
        for offset in range(0, int(duration) + 1, self.args.chunk_seconds):
            if offset >= duration:
                break
            end = min(duration, offset + self.args.chunk_seconds)
            audio_start = max(0, offset - 1)
            audio_end = min(duration, end + 1)
            wav = workspace / 'chunk.wav'
            run('ffmpeg', '-nostdin', '-y', '-v', 'error', '-ss', str(audio_start),
                '-i', str(source), '-t', str(audio_end - audio_start), '-ar', '16000', '-ac', '1',
                '-c:a', 'pcm_s16le', str(wav))
            options = dict(language=self.args.language, task='transcribe', beam_size=5,
                           vad_filter=True, condition_on_previous_text=False, word_timestamps=True,
                           vad_parameters={'min_silence_duration_ms': 500})
            if supports_multilingual:
                options['multilingual'] = use_multilingual
            result, info = model.transcribe(load_pcm(wav), **options)
            self.language_detection.append({'window_start_s': offset, 'scope': 'window_summary_not_segment_language',
                'language': getattr(info, 'language', None),
                'language_probability': getattr(info, 'language_probability', None),
                'multilingual_enabled': use_multilingual,
                'multilingual_supported': supports_multilingual})
            for s in result:
                start, stop = audio_start + s.start, audio_start + s.end
                midpoint = (start + stop) / 2
                if offset <= midpoint < end and s.text.strip():
                    segments.append({'start': round(start, 3), 'end': round(min(stop, duration), 3),
                                     'text': s.text.strip(), 'avg_logprob': round(s.avg_logprob, 4),
                                     'no_speech_prob': round(s.no_speech_prob, 4),
                                     'compression_ratio': round(s.compression_ratio, 4),
                                     'words': [{'word': w.word, 'start': round(audio_start+w.start, 3), 'end': round(min(audio_start+w.end, duration), 3)} for w in (getattr(s, 'words', None) or [])],
                                     **({'language': s.language} if getattr(s, 'language', None) else {})})
        return duration, segments

    def archive(self, source, source_id):
        archive = self.args.root / 'archive'
        archive.mkdir(exist_ok=True)
        dest = archive / (source_id + source.suffix.lower())
        sidecar = source.with_suffix(source.suffix + '.json')
        archived_sidecar = dest.with_suffix(dest.suffix + '.json')
        if dest.is_symlink() or archived_sidecar.is_symlink():
            raise RuntimeError('Archive destination must not be a symlink')
        if sidecar.exists() and archived_sidecar.exists():
            old = json.loads(archived_sidecar.read_text(encoding='utf-8'))
            new = json.loads(sidecar.read_text(encoding='utf-8'))
            keys = ('capture_date', 'original_name', 'is_test', 'source_url')
            if any(old.get(k) != new.get(k) for k in keys):
                raise RuntimeError('Archive metadata conflict; input and sidecar preserved')
        if not dest.exists():
            os.replace(source, dest)
        else:
            if sha256(dest) != source_id:
                raise RuntimeError('Archive hash mismatch; input preserved')
            source.unlink()  # identical bytes retained in verified archive
        if sidecar.exists():
            os.replace(sidecar, archived_sidecar)

    def process(self, source):
        context_root = getattr(self.args, 'context_root', None) or os.environ.get('WATCH_MEMORY_CONTEXT_ROOT')
        if not context_root:
            return self._process_unretired(source)
        # Deployment provides the same receiver-owned store and process lock.
        base = Path(__file__).resolve().parent.parent
        for directory in (base / 'receiver', base / 'receiver_windows', base / 'app_receiver'):
            if directory.is_dir():
                sys.path.insert(0, str(directory))
        from context_store import ContextStore
        ledger = ContextStore(context_root)
        with ledger.generation_guard(timeout=60) as snapshot:
            if sha256(source) in snapshot['retired_raw_source_ids']:
                print(json.dumps({'status': 'retired_source_skipped'}), flush=True)
                return
            return self._process_unretired(source)

    def _process_unretired(self, source):
        root = self.args.root
        source_id = sha256(source)
        target = root / 'outbox' / source_id
        state_file = root / 'state' / (source_id + '.json')
        previous = json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
        metadata_error = None
        try:
            requested_date, requested_name = metadata_for(source, self.args.capture_date)
            sidecar = source.with_suffix(source.suffix + '.json')
            requested_metadata = json.loads(sidecar.read_text(encoding='utf-8')) if sidecar.exists() else {}
            if requested_metadata.get('source_id') and requested_metadata['source_id'] != source_id:
                raise ValueError('Metadata source_id does not match audio SHA256')
        except (ValueError, TypeError) as exc:
            requested_date, requested_name = self.args.capture_date or datetime.now(TZ).date().isoformat(), source.name
            metadata_error = exc
        if metadata_error is None and (target / 'receipt.json').exists():
            complete = json.loads((target / 'receipt.json').read_text(encoding='utf-8'))
            if complete.get('status') == 'done' and all((target / n).is_file() for n in ('transcript.json', 'transcript.md')):
                if (complete.get('capture_date') != requested_date or complete.get('original_name') != requested_name
                    or bool(complete.get('is_test', False)) != bool(requested_metadata.get('is_test', False))
                    or complete.get('source_url') != requested_metadata.get('source_url')):
                    print(json.dumps({'source_id': source_id, 'status': 'metadata_conflict', 'error': 'Same bytes already committed with different metadata; existing receipt preserved'}, ensure_ascii=False), flush=True)
                    return
                try:
                    self.archive(source, source_id)
                except RuntimeError as exc:
                    print(json.dumps({'source_id': source_id, 'status': 'archive_conflict', 'error': str(exc)}, ensure_ascii=False), flush=True)
                    return
                print(json.dumps({'source_id': source_id, 'status': 'duplicate'}, ensure_ascii=False), flush=True)
                return
        attempts = previous.get('attempts', 0) + 1
        begin = time.monotonic()
        base = {'schema_version': 1, 'source_id': source_id, 'status': 'processing',
                'attempts': attempts, 'original_name': source.name,
                'capture_date': self.args.capture_date or datetime.now(TZ).date().isoformat(),
                'audio_duration_s': None, 'processing_duration_s': None,
                'model': self.args.model, 'language': self.args.language, 'segments': [],
                'transcript_md': f'outbox/{source_id}/transcript.md',
                'updated_at': datetime.now(TZ).isoformat()}
        atomic_json(state_file, base)
        def deadline():
            failed = dict(base, status='failed', error='JobDeadlineExceeded: worker terminated; rerun to retry')
            try:
                atomic_json(state_file, failed)
            finally:
                os._exit(124)  # terminate native CUDA/decoder calls and release OS lock
        timer = threading.Timer(getattr(self.args, 'job_timeout', 3600), deadline)
        timer.daemon = True
        timer.start()
        try:
            if metadata_error is not None:
                raise metadata_error
            base['capture_date'], base['original_name'] = requested_date, requested_name
            sidecar = source.with_suffix(source.suffix + '.json')
            metadata = json.loads(sidecar.read_text(encoding='utf-8')) if sidecar.exists() else {}
            if metadata.get('source_id') and metadata['source_id'] != source_id:
                raise ValueError('Metadata source_id does not match audio SHA256')
            base['capture_date_basis'] = metadata.get('capture_date_basis') or ('explicit' if metadata.get('capture_date') or self.args.capture_date else 'receive_date_unconfirmed')
            base['is_test'] = bool(metadata.get('is_test', False))
            base['source_url'] = metadata.get('source_url')
            with tempfile.TemporaryDirectory(prefix=source_id[:12] + '-', dir=root / 'work') as tmp:
                duration, segments = self.transcribe(source, Path(tmp))
            # Refuse a file replaced/changed during decoding; uploader must atomically rename .part.
            if sha256(source) != source_id:
                raise RuntimeError('Input changed during processing; result not committed')
            base['requires_human_review'] = True
            base['quality_flags'] = sorted({flag for seg in segments for flag, condition in (
                ('low_logprob', seg.get('avg_logprob', 0) < -1),
                ('high_compression', seg.get('compression_ratio', 0) > 2.4),
                ('possible_stock_phrase_hallucination', any(x in seg['text'] for x in ('点赞', '订阅', '字幕由', '打赏支持')))) if condition})
            base['language_detection'] = self.language_detection
            base['language_detection_note'] = 'Model language probabilities are not transcription accuracy; per-segment language is available only when supplied by the installed library.'
            base.update(status='done', audio_duration_s=round(duration, 3), segments=segments,
                        processing_duration_s=round(time.monotonic() - begin, 3),
                        updated_at=datetime.now(TZ).isoformat())
            target.mkdir(exist_ok=True)
            atomic_json(target / 'transcript.json', base)
            md_tmp = target / 'transcript.md.tmp'
            md_tmp.write_text(markdown(base), encoding='utf-8')
            os.replace(md_tmp, target / 'transcript.md')
            # receipt is the commit marker; client reads only status=done.
            atomic_json(target / 'receipt.json', base)
            atomic_json(state_file, base)
            try:
                self.archive(source, source_id)
                base['archive_status'] = 'done'
            except RuntimeError as exc:
                base['archive_status'] = 'conflict'
                base['archive_warning'] = str(exc)
            atomic_json(target / 'receipt.json', base)
            atomic_json(state_file, base)
        except Exception as exc:
            base.update(status='failed', error=f'{type(exc).__name__}: {exc}',
                        processing_duration_s=round(time.monotonic() - begin, 3),
                        updated_at=datetime.now(TZ).isoformat())
            atomic_json(state_file, base)
        finally:
            timer.cancel()
        # Log a summary only: transcript text never goes to service logs.
        print(json.dumps({k: base.get(k) for k in ('source_id', 'status', 'attempts', 'audio_duration_s',
                                                    'processing_duration_s', 'archive_status', 'error')}
                         | {'segment_count': len(base.get('segments') or [])}, ensure_ascii=False), flush=True)

@contextlib.contextmanager
def exclusive_lock(root):
    # OS lock auto-releases after crashes; retained lockfile avoids stale-lock deadlocks.
    with (root / 'state' / 'worker.lock').open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if handle.read(1) == b'':
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--model', required=True, help='local faster-whisper (CTranslate2) model directory')
    ap.add_argument('--device', default='cuda', choices=('cuda', 'cpu'))
    ap.add_argument('--compute-type', default='int8_float16')
    ap.add_argument('--language', default='auto', help='auto detects language; e.g. zh forces Chinese')
    ap.add_argument('--multilingual', action=argparse.BooleanOptionalAction, default=True, help='Use segment language detection when supported and language=auto')
    ap.add_argument('--capture-date')
    ap.add_argument('--chunk-seconds', type=int, default=600)
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--job-timeout', type=int, default=3600)
    ap.add_argument('--poll-seconds', type=int, default=60)
    args = ap.parse_args()
    if args.chunk_seconds < 10 or args.poll_seconds < 5 or args.job_timeout < 1:
        ap.error('chunk-seconds >= 10 and poll-seconds >= 5 required')
    if args.capture_date:
        datetime.strptime(args.capture_date, '%Y-%m-%d')
    args.root = args.root.resolve()
    for name in ('inbox', 'outbox', 'state', 'work', 'archive'):
        (args.root / name).mkdir(parents=True, exist_ok=True)
    processor = Processor(args)
    with exclusive_lock(args.root):
        while True:
            for source in sorted((args.root / 'inbox').iterdir()):
                if source.is_file() and not source.is_symlink() and source.suffix.lower() in EXTENSIONS:
                    processor.process(source)
            if args.once:
                return
            time.sleep(args.poll_seconds)

if __name__ == '__main__':
    main()
