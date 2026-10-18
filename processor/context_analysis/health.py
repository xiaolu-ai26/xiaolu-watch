"""Experimental, non-clinical within-person comparison with explicit missingness.

Input samples have stable id, revision, kind, start_at/end_at ISO timestamps,
value/unit, source and optional deleted. HRV means SDNN milliseconds, never RMSSD.
Thresholds are engineering heuristics, not diagnostic cutoffs or psychological labels.
"""
import datetime as dt
import math
import statistics
from zoneinfo import ZoneInfo

UNITS = {'heart_rate': ('count/min', 25, 240), 'resting_heart_rate': ('count/min', 25, 240), 'hrv_sdnn': ('ms', 1, 500),
         'steps': ('count', 0, 100000), 'sleep': ('min', 0, 1440),
         'workout': ('min', 0, 1440)}


def stamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp_required')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed.timestamp()


def normalize(samples, now):
    """Last revision wins including deletions; conflicting same revisions are rejected."""
    latest = {}; invalid = 0; conflicted = set()
    for row in samples:
        try:
            key = (row['source'], row['id'])
            if any(not isinstance(v, str) or not v or len(v) > 256 for v in key):
                raise ValueError()
            revision = row['revision']
            if type(revision) is not int or revision < 0:
                raise ValueError()
            prior = latest.get(key)
            if prior and prior['revision'] == revision and prior != row:
                conflicted.add(key)
            if prior is None or revision > prior['revision']:
                latest[key] = row; conflicted.discard(key)
        except (KeyError, TypeError, ValueError):
            invalid += 1
    output = []
    for key, row in latest.items():
        if key in conflicted:
            invalid += 1; continue
        if row.get('deleted') is True:
            continue
        try:
            unit, low, high = UNITS[row['kind']]
            value = row['value']; start = stamp(row['start_at']); end = stamp(row['end_at'])
            if row['unit'] != unit or type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError()
            if end < start or end > now + 60 or end - start > 86400:
                raise ValueError()
            output.append(dict(row, start=start, end=end))
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    return output, invalid


def analyze(samples, date, timezone, now):
    zone = ZoneInfo(timezone); day = dt.date.fromisoformat(date)
    start = dt.datetime.combine(day, dt.time(), zone).timestamp()
    finish = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), zone).timestamp()
    rows, invalid = normalize(samples, now)
    today = [r for r in rows if start <= r['start'] < finish or (r['kind'] in ('sleep','workout') and r['start'] < finish and r['end'] > start)]
    workouts = [r for r in rows if r['kind'] == 'workout']
    steps = [r for r in rows if r['kind'] == 'steps']

    sleeps = [r for r in rows if r['kind'] == 'sleep' and r.get('category') in (1, 3, 4, 5)]

    def setting(row):
        if row['kind'] == 'resting_heart_rate':
            return 'daily_resting_estimate'
        if any(w['start'] - 1800 <= row['start'] <= w['end'] + 1800 for w in workouts):
            return None
        if row.get('motion_context') == 'active':
            return None
        if any(s['start'] <= row['start'] <= s['end'] for s in sleeps):
            return 'sleep'
        if row.get('motion_context') == 'sedentary':
            return 'sedentary_metadata'
        covering = [s for s in steps if s['start'] <= row['start'] <= s['end'] and 0 < s['end'] - s['start'] <= 1800]
        if covering and all(s['value'] / ((s['end']-s['start'])/60) <= 2 for s in covering):
            return 'low_steps_covered'
        return None

    # Each comparison has one metric, one measurement context and one producer.
    # A daily resting estimate is never pooled with instantaneous pulse samples.
    measured = [dict(r, measurement_setting=setting(r)) for r in rows
                if r['kind'] in ('heart_rate', 'resting_heart_rate', 'hrv_sdnn')]
    measured = [r for r in measured if r['measurement_setting'] is not None]
    current_hrv = [r for r in measured if r['kind']=='hrv_sdnn' and start <= r['start'] < finish]
    candidates = {(r['measurement_setting'], r['source']) for r in current_hrv}
    selection = max(sorted(candidates), key=lambda key: sum((r['measurement_setting'],r['source'])==key for r in current_hrv), default=None)
    current_resting = [r for r in measured if r['kind']=='resting_heart_rate' and start <= r['start'] < finish]
    hr_kind = 'resting_heart_rate' if current_resting else 'heart_rate'
    hr_context = 'daily_resting_estimate' if current_resting else (selection[0] if selection else None)
    current_hr = [r for r in measured if r['kind']==hr_kind and r['measurement_setting']==hr_context and start <= r['start'] < finish]
    hr_sources = {r['source'] for r in current_hr}
    hr_source = max(sorted(hr_sources), key=lambda source: sum(r['source']==source for r in current_hr), default=None)
    criteria = {'heart_rate': (hr_kind, hr_context, hr_source),
                'hrv_sdnn': ('hrv_sdnn', selection[0], selection[1]) if selection else ('hrv_sdnn', None, None)}
    current = {}; history = {}; days = {}
    for kind, (actual_kind, setting_name, source) in criteria.items():
        chosen = [r for r in measured if (r['kind'],r['measurement_setting'],r['source']) == (actual_kind,setting_name,source)]
        current[kind] = [r['value'] for r in chosen if start <= r['start'] < finish]
        buckets = {}
        for row in chosen:
            if start - 28*86400 <= row['start'] < start:
                key = dt.datetime.fromtimestamp(row['start'], zone).date().isoformat()
                buckets.setdefault(key, []).append(row['value'])
        minimum = 1 if actual_kind == 'resting_heart_rate' else 3
        history[kind] = [statistics.median(v) for v in buckets.values() if len(v) >= minimum]
        days[kind] = len(history[kind])
    reason = 'insufficient_current_resting_samples'
    state = 'unknown'; comparisons = {}
    if all(len(current[k]) >= (1 if criteria[k][0] == 'resting_heart_rate' else 3) for k in current):
        reason = 'insufficient_personal_baseline'
        if all(days[k] >= 7 for k in current):
            for kind in current:
                baseline = statistics.median(history[kind]); observed = statistics.median(current[kind])
                deviations = [abs(x-baseline) for x in history[kind]]
                scale = max(statistics.median(deviations)*1.4826, baseline*0.1, 3 if kind == 'heart_rate' else 5)
                comparisons[kind] = {'current_median': round(observed, 2), 'baseline_median': round(baseline, 2),
                                     'relative_deviation': round((observed-baseline)/scale, 2)}
            raised = comparisons['heart_rate']['relative_deviation'] >= 2
            lowered = comparisons['hrv_sdnn']['relative_deviation'] <= -2
            state = 'deviation' if raised and lowered else 'within_observed_range'
            reason = 'paired_resting_signal_change' if raised and lowered else 'no_paired_signal_change'
    messages = {'insufficient_current_resting_samples': '静止状态下可比较的心率和 HRV 数据不足，暂时无法判断。',
                'insufficient_personal_baseline': '个人历史基线不足，暂时无法判断。',
                'paired_resting_signal_change': '静止时心率偏高且 HRV 偏低，出现压力相关生理信号变化；原因无法由这些数据确定。',
                'no_paired_signal_change': '本次可比较样本未出现心率升高与 HRV 降低的共同变化；不代表没有心理压力。'}
    lines = [messages[reason]]
    def interval_minutes(intervals, lower, upper):
        merged = []
        for a,b in sorted((max(r['start'],lower),min(r['end'],upper)) for r in intervals if r['end']>lower and r['start']<upper):
            if b <= a:
                continue
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1],b)
            else:
                merged.append([a,b])
        return sum(b-a for a,b in merged)/60

    # Define a visible observation window instead of implying a complete night's sleep.
    sleep_lower = start - 6*3600; sleep_upper = min(start+18*3600, now)
    sleep_minutes = interval_minutes(sleeps, sleep_lower, sleep_upper)
    if sleep_minutes > 0:
        lines.append(f'昨晚18:00至今天18:00窗口内，已记录睡眠 {sleep_minutes/60:.1f} 小时（截至当前数据）。')
    resting_values = [r['value'] for r in today if r['kind']=='resting_heart_rate']
    if resting_values:
        lines.append(f'当日静息心率记录中位数 {statistics.median(resting_values):.0f} 次/分钟。')
    hrv_values = [r['value'] for r in today if r['kind']=='hrv_sdnn']
    if hrv_values:
        lines.append(f'当日 HRV（SDNN）记录中位数 {statistics.median(hrv_values):.0f} 毫秒；与压力比较所用样本范围可能不同。')
    workout_minutes = interval_minutes(workouts, start, min(finish,now))
    if workout_minutes > 0:
        lines.append(f'当天已有运动区间合计 {workout_minutes:.0f} 分钟。')
    return {'state': state, 'reason': reason, 'lines': lines, 'sample_count': len(today),
            'coverage': {'heart_rate': len(current['heart_rate']), 'hrv_sdnn': len(current['hrv_sdnn']),
                         'baseline_days': days, 'invalid_samples': invalid},
            'comparison': comparisons, 'measurement_contexts': {k: {'metric': v[0], 'setting': v[1]} for k,v in criteria.items()}, 'method': 'experimental_resting_daily_median_v1',
            'medical_diagnosis': False}


def locations(visits, start, finish, now):
    result = []
    for row in visits:
        try:
            a, b = stamp(row['start_at']), stamp(row['end_at'])
            if a >= finish or b <= start or b < a or b > now + 60 or b-a > 86400:
                continue
            alias = row.get('alias')
            label = alias.strip()[:40] if isinstance(alias, str) and alias.strip() else '大致停留地点'
            result.append({'start_at': dt.datetime.fromtimestamp(max(a,start),dt.timezone.utc).isoformat(), 'end_at': dt.datetime.fromtimestamp(min(b,finish),dt.timezone.utc).isoformat(), 'label': label,
                           'current': False})  # Recorded visit never asserts live location.
        except (KeyError, TypeError, ValueError):
            continue
    return result
