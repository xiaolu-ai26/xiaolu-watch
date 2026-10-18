"""Local utterance/time identity and strict editor grouping. No model-authored clocks/owners."""
import hashlib
import json
import math
import re

from .core import validate_output, next_batch
from .editorial import READING_POLICY, READING_POLICY_VERSION, validate_reading, summary_body

GROUP_SCHEMA = 'conversation-groups-2'
GROUP_POLICY = '''本次用下面groups顶层格式取代旧clean/distilled顶层格式，前面的双稿编辑标准适用于每一组。这是按自然对话整理的版本，不把每60秒文件或处理窗口当成对话。
输入units每项是不可拆换的语句单元，i是唯一索引，text是录音数据，owner只来自本地验证，gap_before仅是停顿线索。说话人变化不等于新对话，同一人也可换题。按内容真实连续性分组，短想法不能漏，不能把全窗口机械合成一个主题。
只返回JSON：{"groups":[{"title":"具体自然主题","indices":[0],"continue_previous":false,"clean":"口语清稿正文","distilled":"二级主题标题加完整整理正文"}],"uncertain_terms":[]}。
groups按原顺序，每项indices必须是连续的新unit索引，所有输入unit恰好出现一次；不能遗漏/重复/重排，禁止自由生成时间、sourceID、人物姓名或身份。只编辑本批units；previous/context只是前文线索，不重新输出旧正文。
第一组若确实延续previous同一讨论，可continue_previous=true且title必须与previous.title完全相同；其它组必须false。停顿或轮换只是线索，话题未变则延续，内容明显换题则另组。每组必须提供两份完整去杂正文，不能仅摘一句摘要；distilled必须以与title一致的##标题开头。clean不要自行加身份标签或标题。
同一话题要求具体问题或讨论目的连续，不能只因同一个软件、同一个人或同一个录音session就当成同一对话；多个独立想法应分开，针对同一问题的问答与解释保持一起。
按完整交谈事件整理，不按每一句的名词或每个小问题切卡。一个来回中的追问、回答、转折、例子、短插话及返回原问题通常仍是同一讨论；只有新的独立事件或讨论目的确实开始才拆组。不要为每句短答都另写一个摘要。无法确认连续性时保留不确定，不凭时间近或同一说话人硬并。previous.retained_context是上一组的净化语义；若本批接着解释其未完论证或回答其问题，第一组继续原title与ID。独立出现的有价值短想法仍单独保留。例如连续说“这个设备还能保修吗”“纸巾递我一下”“保修要带什么凭证”，属于同一保修交谈中的短插话，应保持一组，而不是按纸巾和保修拆三张；若后来明确开始讨论另一项工作，才另开独立组。
owner为unknown不表示没有内容，也不得由文本猜是谁。两稿中的我保留原话但不是已验证本人观点，身份说明由本地程序添加。
'''
GROUP_POLICY += '\n完整合成示例：输入两unit“我打算明天散步。”“不过可能会下雨。”，输出' + json.dumps({
    'groups': [{'title': '散步安排', 'indices': [0, 1], 'continue_previous': False,
                'clean': '我打算明天散步，不过可能会下雨。', 'distilled': '## 散步安排\n\n我打算明天散步，不过可能会下雨。'}],
    'uncertain_terms': [],
}, ensure_ascii=False)


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def utterances(rows):
    """Use actual ASR segment coordinates. Missing coordinates stay unknown, never interpolated."""
    units = []
    for row in rows:
        parts = row.segments or (({'text': row.text, 'start': None, 'end': None},) if row.text else ())
        for segment_index, part in enumerate(parts):
            text = part['text'].strip()
            if not text:
                continue
            start, end = part.get('start'), part.get('end')
            timed = finite_number(start) and finite_number(end) and 0 <= start < end <= row.duration + 1
            sentences = [s.strip() for s in re.split(r'(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z])', text) if s.strip()]
            word_cursor = 0
            words = part.get('words') or []
            aligned_text = ''.join(w.get('word', '') for w in words).strip()
            alignment_valid = aligned_text == text and all(finite_number(w.get('start')) and finite_number(w.get('end')) and 0 <= w['start'] < w['end'] <= row.duration + 1 for w in words)
            for sentence_index, sentence in enumerate(sentences):
                sentence_start, sentence_end, precision = start, end, 'asr_parent_span' if timed else 'unknown'
                if alignment_valid:
                    selected = []; assembled = ''
                    while word_cursor < len(words) and len(assembled.strip()) < len(sentence):
                        word = words[word_cursor]; selected.append(word); assembled += word['word']; word_cursor += 1
                    if assembled.strip() == sentence and selected:
                        sentence_start, sentence_end, precision = selected[0]['start'], selected[-1]['end'], 'asr_word_span'
                    else:
                        alignment_valid = False
                unit = {'i': len(units), 'text': sentence, 'source_id': row.source_id, 'source_receipt_hash': row.receipt_hash,
                        'asr_segment_index': segment_index, 'sentence_index': sentence_index,
                        'time_precision': precision,
                        'start': sentence_start if timed else None, 'end': sentence_end if timed else None,
                        'absolute_start': row.captured + sentence_start if timed else None,
                        'absolute_end': row.captured + sentence_end if timed else None, 'owner': 'unknown', 'speaker_reason': 'unconfigured'}
                units.append(unit)
    for i, unit in enumerate(units):
        prior = units[i-1] if i else None
        gap = unit['absolute_start'] - prior['absolute_end'] if prior and unit['absolute_start'] is not None and prior['absolute_end'] is not None else None
        unit['gap_before'] = 'unknown' if gap is None or gap < 0 else 'long' if gap >= 30 else 'pause' if gap >= 3 else 'short'
    return units


def editor_input(units, previous=None, context=''):
    # Deliberate egress allowlist: no audio, embedding, model/profile IDs, scores or timestamps.
    return json.dumps({'units': [{k: u[k] for k in ('i', 'text', 'owner', 'gap_before')} for u in units],
                       'previous': {'title': previous['title'],'retained_context': previous.get('summary_input',previous.get('distilled_fragment',previous.get('clean_fragment','')))[-1800:]} if previous else None,
                       'context': context[-600:]}, ensure_ascii=False)


def choose_window(slots, consumed, now, floor=0, max_seconds=300, max_chars=3500, tail_seconds=180, pause_seconds=12):
    """Pause flushes a bounded processing window, never declares a topic boundary."""
    if not 3 <= pause_seconds <= 120:
        raise ValueError('conversation pause signal outside bounded range')
    rows = next_batch(slots, consumed, now, max_seconds, max_chars, 0, floor)
    if not rows:
        return []
    units = utterances(rows)
    if not units:
        return rows
    maximum = sum(r.duration for r in rows) >= max_seconds - 1 or sum(len(r.text) for r in rows) >= max_chars
    pauses = any(a['absolute_end'] is not None and b['absolute_start'] is not None
                 and b['absolute_start'] - a['absolute_end'] >= pause_seconds for a, b in zip(units, units[1:]))
    trailing = units[-1]['absolute_end'] is not None and max(r.captured + r.duration for r in rows) - units[-1]['absolute_end'] >= pause_seconds
    age = now - max(r.captured + r.duration for r in rows)
    return rows if maximum or pauses or trailing or age >= tail_seconds else []


def validate_groups(value, units, previous=None, context='', require_reading=False):
    if not isinstance(value, dict) or set(value) != {'groups', 'uncertain_terms'}:
        raise ValueError('conversation schema invalid')
    groups, terms = value['groups'], value['uncertain_terms']
    if not isinstance(groups, list) or not 1 <= len(groups) <= min(16, len(units)):
        raise ValueError('conversation group count invalid')
    covered = []
    for n, group in enumerate(groups):
        if not isinstance(group, dict) or not {'title','indices','continue_previous','clean','distilled'}.issubset(group) or set(group)-{'title','indices','continue_previous','clean','distilled','reading'} or (require_reading and 'reading' not in group):
            raise ValueError('conversation group schema invalid')
        title, indices, continuation = group['title'], group['indices'], group['continue_previous']
        if not isinstance(title, str) or not title.strip() or title.strip() in ('主题', '标题', '整理稿', '真实内容主题', '具体自然主题') or len(title) > 60 or re.search(r'[\n\r#<>]|[0-9a-f]{32}', title):
            raise ValueError('conversation title invalid')
        if type(continuation) is not bool or (continuation and (n != 0 or not previous or title != previous['title'])):
            raise ValueError('conversation continuation invalid')
        if not isinstance(indices, list) or not indices or any(type(i) is not int or not 0 <= i < len(units) for i in indices):
            raise ValueError('conversation indices invalid')
        if indices != list(range(indices[0], indices[-1] + 1)):
            raise ValueError('conversation indices not contiguous')
        covered.extend(indices)
        if 'reading' in group:
            validate_reading(group, units)
        validation_distilled = group['distilled'] or ('## '+title+'\n\n'+group['clean'])
        validate_output({'clean': group['clean'], 'distilled': validation_distilled, 'uncertain_terms': terms}, '\n'.join(units[i]['text'] for i in indices))
        if re.search(r'(?m)^#+\s|^(?:Speaker|说话人)\s*\d+\s*[:：]', group['clean']):
            raise ValueError('conversation clean has generated labels or headings')
        if group['distilled'].strip() and group['distilled'].strip().split('\n', 1)[0] != '## ' + title:
            raise ValueError('conversation title does not match body')
        # Reject obvious replay of a long prior passage absent from the new units.
        normal = lambda x: re.sub(r'\s+', '', x)
        prior = normal(context); new = normal('\n'.join(units[i]['text'] for i in indices))
        rendered = normal(group['clean'] + group['distilled'])
        prior_fragment = normal(previous.get('clean_fragment', '')) if previous else ''
        if len(prior_fragment) >= 24 and prior_fragment in rendered and prior_fragment not in new:
            raise ValueError('conversation replays previous content')
        for offset in range(0, max(0, len(prior) - 79), 40):
            excerpt = prior[offset:offset+80]
            if len(excerpt) == 80 and excerpt in rendered and excerpt not in new:
                raise ValueError('conversation replays previous content')
    if covered != list(range(len(units))):
        raise ValueError('conversation coverage/order invalid')
    return value


def owner_caption(units):
    labels = {u['owner'] for u in units}
    if labels == {'self'}:
        return '疑似本人讲话'
    if 'other' in labels:
        return '含未匹配声音，部分归属未确认' if 'unknown' in labels else '包含未匹配本人的声音'
    return '归属未确认'


def materialize(value, units, batch_id, previous=None):
    """Only append current units. Stable IDs live in sidecars, human titles in notes."""
    clean, distilled, metadata = [], [], []
    for n, group in enumerate(value['groups']):
        subset = [units[i] for i in group['indices']]
        continuing = group['continue_previous']
        identifier = previous['id'] if continuing else hashlib.sha256(f'{batch_id}:{n}'.encode()).hexdigest()
        title = group['title']
        visible_title = title + '（续）' if continuing else title
        caption = owner_caption(subset)
        clean.append('## ' + visible_title + '\n\n' + group['clean'].strip())
        body = group['distilled'].strip().split('\n', 1)[1].lstrip() if group['distilled'].strip() else ''
        distilled.append('## ' + visible_title + '\n\n' + body if body else '')
        metadata.append({'id': identifier, 'title': title, 'continuation': continuing, 'indices': group['indices'],
                         'clean_fragment': group['clean'], 'distilled_fragment': group['distilled'],
                         'clean_rendered': clean[-1], 'distilled_rendered': distilled[-1],
                         'started_at': previous.get('started_at') if continuing else next((u['absolute_start'] for u in subset if u['absolute_start'] is not None), None),
                         'window_count': previous.get('window_count', 1) + 1 if continuing else 1,
                         'owner_caption': caption, 'reading': group.get('reading'), 'reading_version': READING_POLICY_VERSION if group.get('reading') else None,
                         'summary_input': summary_body(group['distilled']),
                         'reading_units': [{k:u[k] for k in ('i','source_id','source_receipt_hash','asr_segment_index','sentence_index','start','end')} for u in subset] if group.get('reading') else [], 'source_spans': [{k: u[k] for k in ('source_id', 'source_receipt_hash', 'asr_segment_index', 'sentence_index', 'time_precision', 'start', 'end', 'absolute_start', 'absolute_end', 'owner', 'speaker_reason')} for u in subset]})
    return {'clean': '\n\n'.join(clean), 'distilled': '\n\n'.join(x for x in distilled if x), 'uncertain_terms': value['uncertain_terms']}, metadata
