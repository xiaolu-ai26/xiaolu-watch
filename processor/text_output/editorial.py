"""Shared retained-semantics contract for clean text, distilled text and summaries."""
import re

READING_POLICY_VERSION = 'retained-reading-1'
READING_POLICY = '''每组额外返回reading对象：{"role":"main|background|unknown","retained_indices":[输入单元索引],"excluded":[{"index":输入索引,"reason":"filler|greeting|promotion|repetition"}],"summary":"直接的简短内容"}。
retained_indices与excluded覆盖该组全部indices且不重不漏，依据只存在私有sidecar，不写进正文。main为有实际信息，unknown为语义不清但仍保留，background仅整组没有可保留语义；unknown和其他人的完整讲解不可降权。不能根据长度、声纹、第一人称、像媒体来排除内容。三到八字也能是重要想法，如“先验证需求”“不要默认删”。别人完整解释而本人未插话也是有价值内容。课程、故事、倾听内容可总结为录音中的内容，不能写成佩戴者的经历。
仅可剔除完整的纯填词单元、纯寒暄告别、明确无关的关注订阅推广，以及有保留单元逐字对应的重复。含论点、疑问、数字、否定、猜想的单元须保留；不清楚的真实文字仍保留并按需标不清。句内填词可直接净化，不因此剔除整句。语义相似不等于逐字重复：例如“先验证需求”和“先验证需求啊”两个单元都应放retained_indices，正文可合并表达，不可把后一单元标为repetition。excluded可为空；不确定是否满足排除条件的单元全部保留。英文um/uh等确定填词删掉，like表达喜欢/比较时保留，like口头停顿可删。相同问题的重复播放或原句冗余须合并一次，不把Yeah/just like a等口头残片复制进整理稿；但回答中的否定、条件、频率和类比必须保留。已有名称待核、未展开或话未说完的证据不能突然写成确定或完整事实。
两稿保留相同的关键语义：clean保留自然口吻，distilled完整整理推理与案例而非转述“有人说...”；禁止用“已删除该段”占位。纯推广的clean可保留清楚原话供查看，distilled应为空字符串，reading.summary也为空，role=background。可读正文不写归属未确认、包含未匹配声音之类元数据。
reading.summary只能总结distilled中保留的语义，不得从clean或excluded恢复推广、寒暄。有意义的短句直接用净化后的短句，别扩成“对话中提到...”。不要写归属未确认或套话开头；保持原话的加/或、先后、否定、可能、尚未与问句，不把提问变成已证实结论。不自动写“你认为/你决定/你经历”。对多人内容可用中性主题句。不推断身份、媒体来源、健康或心理原因。
所有组保留原有source/group边界，只改变文字与阅读层；不要合并删除范围。
'''


def normalized(text):
    return re.sub(r'[\W_]+', '', text.casefold())


def validate_reading(group, units):
    plan = group.get('reading')
    if not isinstance(plan, dict) or set(plan) != {'role','retained_indices','excluded','summary'}:
        raise ValueError('reading_plan_schema')
    if plan['role'] not in ('main','background','unknown') or not isinstance(plan['summary'],str) or len(plan['summary'])>220:
        raise ValueError('reading_plan_invalid')
    kept, excluded = plan['retained_indices'], plan['excluded']
    if not isinstance(kept,list) or any(type(i) is not int for i in kept) or not isinstance(excluded,list):
        raise ValueError('reading_indices_invalid')
    removed=[]; by_index={u['i']:u for u in units}; group_indices=group['indices']
    for item in excluded:
        if not isinstance(item,dict) or set(item)!={'index','reason'} or type(item['index']) is not int or item['index'] not in group_indices:
            raise ValueError('reading_exclusion_invalid')
        index=item['index']; reason=item['reason']; text=by_index[index]['text']; norm=normalized(text)
        if reason=='repetition':
            if not any(i in by_index and repetition_key(by_index[i]['text'])==repetition_key(text) for i in kept):
                raise ValueError('reading_repetition_without_retained_evidence')
        elif reason=='filler':
            if norm != '哎' and not re.fullmatch(r'(?:嗯|呃|啊|哦|诶|那个|就是|然后|um|uh|erm|er)+',norm):
                raise ValueError('reading_filler_contains_information')
        elif reason=='greeting':
            if not re.fullmatch(r'(?:(?:拜拜|再见|你好|早安|晚安|hello|hi|bye|goodbye|thankyou|thanks)+|thankyouforwatching(?:andseeyouinthenextvideo)?|感谢观看我们下期再见)',norm):
                raise ValueError('reading_greeting_contains_information')
        elif reason=='promotion':
            if not pure_promotion(text):
                raise ValueError('reading_promotion_not_complete_pure_cta')
        else:
            raise ValueError('reading_exclusion_reason_invalid')
        removed.append(index)
    if len(kept+removed)!=len(set(kept+removed)) or sorted(kept+removed)!=sorted(group_indices):
        raise ValueError('reading_partition_incomplete')
    if not kept:
        if plan['role']!='background' or group['distilled'].strip() or plan['summary'].strip():
            raise ValueError('reading_excluded_content_cannot_have_summary')
    elif plan['role']=='background' or not group['distilled'].strip() or not plan['summary'].strip():
        raise ValueError('reading_retained_information_requires_visibility')
    if re.search(r'归属未确认|歸屬未確認|未匹配声音|未匹配聲音|^对话中提到|^對話中提到',plan['summary']):
        raise ValueError('reading_metadata_in_summary')
    if re.match(r'^(?:你|您|本人|我)(?:今天|已经|已|刚刚)?(?:认为|决定|经历|完成|想|打算)',plan['summary']):
        raise ValueError('summary_unverified_personal_narrative')
    retained_text=' '.join(by_index[i]['text'] for i in kept)
    for item in excluded:
        if item['reason']=='promotion':
            excluded_text=by_index[item['index']]['text']
            for term in CTA_TERMS:
                if term in excluded_text.casefold() and term not in retained_text.casefold() and term in (group['distilled']+' '+plan['summary']).casefold():
                    raise ValueError('excluded_promotion_resurrected')
    check_explicit_relations(group['distilled'],plan['summary'])
    return plan


def summary_body(markdown):
    lines=[line for line in markdown.splitlines() if not line.startswith('#') and line.strip() not in (
        '归属未确认','含未匹配声音，部分归属未确认','包含未匹配本人的声音','疑似本人讲话')]
    body='\n'.join(lines).strip()
    if re.fullmatch(r'[（(].*已删除.*[）)]',body,re.S):
        return ''
    return body


def check_explicit_relations(source, summary):
    """Narrow contradiction veto, not a general semantic correctness claim."""
    compact=re.sub(r'\s+','',source)
    target=re.sub(r'\s+','',summary)
    for match in re.finditer(r'([\u4e00-\u9fff]{2,8})加([\u4e00-\u9fff]{2,8})',compact):
        left,right=match.groups()
        for a in range(2,min(6,len(left))+1):
            for b in range(2,min(6,len(right))+1):
                alternative=left[-a:]+'或'+right[:b]
                if alternative in target and alternative not in compact:
                    raise ValueError('summary_addition_changed_to_alternative')
    for a,b in re.findall(r'\b([A-Za-z]+)\s+and\s+([A-Za-z]+)\b',source):
        alternative=f'{a} or {b}'
        if alternative.lower() in summary.lower() and alternative.lower() not in source.lower():
            raise ValueError('summary_conjunction_changed_to_alternative')


CTA_TERMS=('点赞','订阅','转发','打赏','收藏','关注','subscribe','follow','share')

def pure_promotion(text):
    """Closed grammar for complete platform CTAs, not a keyword classifier."""
    raw=text.casefold().strip()
    # Negation, questions, numbers or mixed propositions are never inferred to be
    # disposable promotion. The conventional phrase 不吝 is explicitly matched.
    checked=raw.replace('不吝','')
    if re.search(r'[?？0-9]|不|没|未|别|勿|为什么|如何|如果|但是|因为|可能|尚|why|because|unless|not|never|maybe',checked):
        return False
    compact=re.sub(r'[\s，,。.!！、;；:：]+','',raw)
    prefix=r'(?:请大家|请|欢迎大家|欢迎|麻烦大家)?(?:不吝)?'
    action=r'(?:点赞|订阅|转发|打赏|收藏|加关注|关注|一键三连)'
    tail=r'(?:支持[一-鿿A-Za-z]{0,20}(?:栏目|频道))?'
    if re.fullmatch(prefix+r'(?:'+action+r'(?:和|与|并|及)?){1,8}'+tail,compact):
        actions=re.findall(action,compact)
        return compact.startswith(('请','欢迎','麻烦','不吝')) or len(actions)>=2
    return bool(re.fullmatch(r'(?:please\s+(?:like\s+and\s+)?subscribe|like\s+and\s+subscribe)(?:\s+to\s+(?:the|our)\s+channel)?[.!]?',raw))


def repetition_key(text):
    # Preserve decimal points, signs, fractions, negation and sentence modality.
    return re.sub(r'\s+',' ',text).strip()


def retain_literal_repetitions(value, units):
    """Conservatively retain a disputed unit already present in BOTH full drafts.

    This never authorizes an exclusion. The provider response stays untouched;
    returned actions are private provenance, not reader-facing text.
    """
    import copy
    result=copy.deepcopy(value);actions=[];by_index={u['i']:u['text'] for u in units}
    def literal(text):
        return repetition_key(re.sub(r'\*\*([^*]+)\*\*',r'\1',text))
    def contains(body,unit):
        body=literal(body);unit=literal(unit)
        if not unit:return False
        # Avoid matching an English word inside an unrelated longer word.
        left=r'(?<![A-Za-z0-9])' if unit[0].isascii() and unit[0].isalnum() else ''
        right=r'(?![A-Za-z0-9])' if unit[-1].isascii() and unit[-1].isalnum() else ''
        return bool(re.search(left+re.escape(unit)+right,body))
    groups=result.get('groups',[]) if isinstance(result,dict) else []
    if not isinstance(groups,list):return result,actions
    for group in groups:
        if not isinstance(group,dict):continue
        plan=group.get('reading',{})
        if not isinstance(plan,dict):continue
        if plan.get('role') not in ('main','unknown') or not isinstance(plan.get('retained_indices'),list) or not isinstance(plan.get('excluded'),list):continue
        if not isinstance(group.get('clean'),str) or not isinstance(group.get('distilled'),str) or not isinstance(group.get('indices'),list):continue
        kept=plan['retained_indices'];removed=[]
        if any(type(i) is not int for i in kept):continue
        for item in plan['excluded']:
            index=item.get('index') if isinstance(item,dict) else None
            text=by_index.get(index) if type(index) is int else None
            already_proved=bool(text and any(i in by_index and repetition_key(by_index[i])==repetition_key(text) for i in kept))
            if (isinstance(item,dict) and item.get('reason')=='repetition' and index in group.get('indices',[]) and
                index not in kept and text and not already_proved and
                contains(summary_body(group['clean']),text) and contains(summary_body(group['distilled']),text)):
                kept.append(index);actions.append({'unit_index':index,'original_reason':'repetition','action':'retained_literal_text_in_both_drafts'})
            else:removed.append(item)
        plan['retained_indices']=sorted(kept);plan['excluded']=removed
    return result,actions


def normalize_editor_response(value,units):
    """Lossless conversion of one known terms representation; raw stays untouched."""
    import copy
    candidate=copy.deepcopy(value);actions=[]
    terms=candidate.get('uncertain_terms') if isinstance(candidate,dict) else None
    if isinstance(terms,list) and 0<len(terms)<=30 and all(isinstance(x,dict) and set(x)=={'term','note'} for x in terms):
        if all(isinstance(x[k],str) and x[k].strip() and len(x[k])<=80 for x in terms for k in ('term','note')):
            strings=[x['term']+'（'+x['note']+'）' for x in terms]
            if all(len(x)<=80 for x in strings):
                candidate['uncertain_terms']=strings
                actions=[{'field':'uncertain_terms','index':i,'action':'lossless_term_note_to_string'} for i in range(len(strings))]
    normalized,repetitions=retain_literal_repetitions(candidate,units)
    return normalized,actions+repetitions
