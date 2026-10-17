"""Read local speaker evidence, fail to unknown. Never send biometric data to providers."""
import json
import hashlib
from pathlib import Path
import re

from .conversation import finite_number


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise ValueError('speaker sidecar invalid file')
    raw = path.read_bytes()
    if len(raw) > 512 * 1024:
        raise ValueError('speaker sidecar exceeds bounded size')
    value = json.loads(raw.decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('speaker sidecar root must be an object')
    return value, hashlib.sha256(raw).hexdigest()


def attach(units, rows, config, state, now):
    """Returns bounded waiting and local diagnostics; mutates only per-unit local labels."""
    if not isinstance(config, dict):
        return {'waiting': False, 'unknown_units': len(units), 'reason': 'speaker_profile_unconfigured'}
    expected = (config.get('profile_version'), config.get('model_revision'))
    full_model = (config.get('model_id'), config.get('model_sha256'), config.get('profile_sha256'))
    if not config.get('outbox') or not all(isinstance(x, str) and x for x in (*expected, *full_model)) or any(not re.fullmatch('[0-9a-f]{64}', x) for x in full_model[1:]):
        return {'waiting': False, 'unknown_units': len(units), 'reason': 'speaker_profile_unconfigured'}
    wait = config.get('wait_seconds', 60)
    if type(wait) not in (int, float) or not 0 <= wait <= 60:
        raise ValueError('speaker waiting must be bounded to sixty seconds')
    waiting = False; reasons = {}; evidence = {}
    by_id = {r.source_id: r for r in rows}
    for source_id in sorted({u['source_id'] for u in units}):
        subset = [u for u in units if u['source_id'] == source_id]
        path = Path(config['outbox']) / source_id / 'identity.json'
        if not path.exists():
            key = 'speaker-wait:' + source_id
            first = state.get(key)
            if first is None:
                first = now; state.put(key, first)
            pending = finite_number(first) and 0 <= now - first < wait
            waiting |= pending
            reasons[source_id] = 'speaker_pending' if pending else 'speaker_missing_timeout'
            for unit in subset:
                unit['speaker_reason'] = reasons[source_id]
            continue
        try:
            value, sidecar_sha = _read(path); row = by_id[source_id]
            if value.get('version') != 1 or value.get('status') != 'done':
                raise ValueError('speaker unavailable')
            identity = {'source_id': source_id, 'audio_sha256': source_id, 'asr_receipt_sha256': row.receipt_hash,
                        'profile_version': expected[0], 'profile_sha256': config['profile_sha256'], 'model_revision': expected[1], 'time_basis': 'source_audio_seconds'}
            if any(value.get(k) != v for k, v in identity.items()):
                raise ValueError('speaker evidence mismatch')
            if not isinstance(value.get('model_id'), str) or not value['model_id'] or not re.fullmatch('[0-9a-f]{64}', value.get('model_sha256', '')):
                raise ValueError('speaker model provenance missing')
            for field in ('model_id', 'model_sha256'):
                if value[field] != config[field]:
                    raise ValueError('speaker model provenance mismatch')
            turns = value.get('turns')
            if not isinstance(turns, list) or len(turns) > 1024:
                raise ValueError('speaker turns invalid')
            for t in turns:
                if not isinstance(t, dict) or t.get('label') not in ('self', 'other', 'unknown') or type(t.get('asr_segment_index')) is not int or t['asr_segment_index'] < 0:
                    raise ValueError('speaker turn schema invalid')
                if not finite_number(t.get('start')) or not finite_number(t.get('end')) or not 0 <= t['start'] < t['end'] <= row.duration + 1:
                    raise ValueError('speaker time invalid')
                if t['label'] != 'unknown' and (not finite_number(t.get('score')) or not -1 <= t['score'] <= 1):
                    raise ValueError('speaker score invalid')
                if not isinstance(t.get('confidence_reason'), str) or not t['confidence_reason'] or len(t['confidence_reason']) > 160:
                    raise ValueError('speaker confidence reason missing')
            for unit in subset:
                unit['speaker_reason'] = 'speaker_span_uncovered'
                if unit['start'] is None:
                    continue
                overlaps = []
                for t in turns:
                    if t['asr_segment_index'] != unit['asr_segment_index']:
                        continue
                    overlap = max(0, min(unit['end'], t['end']) - max(unit['start'], t['start']))
                    if overlap > 0:
                        overlaps.append((t, (max(unit['start'], t['start']), min(unit['end'], t['end']))))
                labels = {t['label'] for t, _ in overlaps}
                # Mixed/unknown spans never upgrade to self. Positive labels require broad coverage.
                intervals = sorted(bounds for _, bounds in overlaps)
                merged = []
                for start, end in intervals:
                    if merged and start <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                    else:
                        merged.append((start, end))
                coverage = sum(end-start for start, end in merged) / (unit['end'] - unit['start'])
                if len(labels) == 1 and 'unknown' not in labels and .98 <= coverage <= 1.0 + 1e-6:
                    unit['owner'] = next(iter(labels)); unit['speaker_reason'] = 'verified_local_span'
                elif overlaps:
                    unit['speaker_reason'] = 'mixed_or_uncertain_span'
            evidence[source_id] = {'sidecar_sha256': sidecar_sha, **{k: value[k] for k in ('asr_receipt_sha256', 'profile_version', 'profile_sha256', 'model_id', 'model_revision', 'model_sha256')}, 'unit_labels': [{'i': u['i'], 'asr_segment_index': u['asr_segment_index'], 'sentence_index': u['sentence_index'], 'label': u['owner'], 'reason': u['speaker_reason']} for u in subset]}
            reasons[source_id] = 'speaker_evidence_read'
        except (ValueError, KeyError, TypeError, OSError):
            reasons[source_id] = 'speaker_evidence_invalid'
            for unit in subset:
                unit['owner'] = 'unknown'; unit['speaker_reason'] = 'speaker_evidence_invalid'
    return {'waiting': waiting, 'unknown_units': sum(u['owner'] == 'unknown' for u in units), 'reasons': reasons, 'evidence': evidence}
