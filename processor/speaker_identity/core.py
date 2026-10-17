"""Pure speaker-decision policy. Scores are cosine similarity, not accuracy."""
import math

def unit(vector):
    values = [float(x) for x in vector]
    if not values or not all(math.isfinite(x) for x in values):
        raise ValueError('Invalid embedding')
    norm = math.sqrt(sum(x*x for x in values))
    if norm < 1e-9:
        raise ValueError('Zero embedding')
    return [x/norm for x in values]

def cosine(left, right):
    left, right = unit(left), unit(right)
    if len(left) != len(right):
        raise ValueError('Embedding dimensions differ')
    return max(-1., min(1., sum(a*b for a,b in zip(left, right))))

def validate_profile(profile, model_sha256, dimension, model_id=None, model_revision=None):
    if profile.get('version') != 1 or not profile.get('profile_version'):
        raise ValueError('Invalid profile version')
    if profile.get('model_sha256') != model_sha256:
        raise ValueError('Profile model differs')
    if len(profile.get('centroid', [])) != dimension:
        raise ValueError('Profile dimension differs')
    if model_id is not None and profile.get('model_id') != model_id:
        raise ValueError('Profile model ID differs')
    if model_revision is not None and profile.get('model_revision') != model_revision:
        raise ValueError('Profile model revision differs')
    unit(profile['centroid'])
    thresholds = profile['thresholds']
    if not -1 <= thresholds['other_max'] < thresholds['self_min'] <= 1:
        raise ValueError('Invalid decision thresholds')
    if not 0 <= thresholds['consistency_min'] <= 1:
        raise ValueError('Invalid consistency threshold')
    if profile.get('enrollment_authorization') != 'user_confirmed_seed':
        raise ValueError('Profile has no authorized seed')
    if profile.get('calibration_ready') is not True:
        raise ValueError('Profile calibration not ready')

def windows(start, end, max_seconds=6.0):
    if any(not math.isfinite(x) for x in (start,end)) or start < 0 or end <= start:
        raise ValueError('Invalid speech span')
    count = max(1, math.ceil((end-start)/max_seconds))
    step = (end-start)/count
    return [(start+i*step, end if i == count-1 else start+(i+1)*step) for i in range(count)]

def decide(score, support_scores, consistency, duration, quality_ok, thresholds, overlap=False):
    if overlap:
        return 'unknown', 'overlapping_asr_spans_or_reported_overlap'
    if duration < 3:
        return 'unknown', 'speech_window_too_short'
    if not quality_ok:
        return 'unknown', 'insufficient_energy_or_clipping'
    scores = [score, *support_scores]
    if not scores or not all(x is not None and math.isfinite(x) and -1 <= x <= 1 for x in scores):
        return 'unknown', 'invalid_embedding_score'
    if not math.isfinite(consistency) or consistency < thresholds['consistency_min']:
        return 'unknown', 'inconsistent_subwindows_possible_mixed_voice'
    if max(scores)-min(scores) > .25:
        return 'unknown', 'unstable_match_possible_mixed_voice'
    if min(scores) >= thresholds['self_min']:
        return 'self', 'consistent_match_to_user_confirmed_profile'
    if max(scores) <= thresholds['other_max']:
        return 'other', 'consistent_below_owner_match_threshold'
    return 'unknown', 'between_thresholds'
