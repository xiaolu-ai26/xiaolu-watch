"""Append-only managed daily sections; external writes reconciled by content + IDs.

No machine idempotency markers are inserted into reader-facing document bodies.
An ambiguous create is blocked until its saved response or explicit recover-doc binds it.
"""
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

from .store import atomic


def blocks(markdown):
    result = []
    for paragraph in re.split(r'\n\s*\n', markdown.strip()):
        if paragraph.startswith('## '):
            lines = paragraph.splitlines()
            result.append(('h2', lines[0][3:].strip()))
            if len(lines) > 1:
                result.append(('p', '\n'.join(lines[1:])))
        elif paragraph.strip():
            result.append(('p', paragraph.strip()))
    return result


def fragment(items):
    # Only text + modest bold; recording data can never inject XML/resources.
    lines = []
    for tag, text in items:
        escaped = html.escape(text).replace('\n', '<br/>')
        escaped = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', escaped)
        lines.append(f'<{tag}>{escaped}</{tag}>')
    return '\n'.join(lines)


def plain(text):
    return text.replace('**', '').replace('\r\n', '\n').strip()


def document_blocks(content):
    if not isinstance(content, str) or '<!DOCTYPE' in content or '<!ENTITY' in content:
        raise ValueError('unexpected document representation')
    root = ET.fromstring('<root>' + content + '</root>')
    result = []
    for element in root:
        if element.tag not in ('title', 'h1', 'h2', 'p'):
            raise ValueError('unmanaged document resource; preserve and stop')
        # itertext does not preserve br; normalize all whitespace for comparison only.
        text = ''.join(element.itertext())
        result.append({'tag': element.tag, 'text': re.sub(r'\s+', '', text.replace('**', '')), 'id': element.get('id')})
    return result


def shape(items):
    return [[x['tag'], x['text']] for x in items]


def expected(items):
    return [[tag, re.sub(r'\s+', '', plain(text))] for tag, text in items]


def identities(items):
    return [[x.get('id'), x['tag'], x['text']] for x in items]


def preserves_owned(previous, current):
    previous = [x for x in previous if x['tag'] != 'title']
    expected_ids = [x.get('id') for x in previous]
    actual_ids = [x.get('id') for x in current if x['tag'] != 'title']
    if not all(expected_ids) or len(actual_ids) != len(set(actual_ids)):
        return False
    old_set = set(expected_ids)
    return identities([x for x in current if x.get('id') in old_set]) == identities(previous)


class Lark:
    def __init__(self, state, home, command=None):
        self.state = state; self.home = Path(home); self.command = command or self._command

    def _command(self, arguments):
        argv = ['lark-cli', 'docs', *arguments, '--as', 'user']
        if os.name == 'nt':
            node = shutil.which('node')
            entry = Path(os.environ.get('APPDATA', '')) / 'npm/node_modules/@larksuite/cli/scripts/run.js'
            if not node or not entry.is_file():
                raise RuntimeError('lark_cli_not_installed')
            argv = [node, str(entry), *argv[1:]]
        try:
            p = subprocess.run(argv, cwd=self.home, capture_output=True, encoding='utf-8', timeout=100)
        except subprocess.TimeoutExpired:
            raise RuntimeError('lark_outcome_unknown_timeout') from None
        if p.returncode:
            raise RuntimeError('lark_operation_failed_' + str(p.returncode))
        data = json.loads(p.stdout)
        if not data.get('ok'):
            raise RuntimeError('lark_operation_not_ok')
        return data

    def fetch(self, doc_id):
        value = self.command(['+fetch', '--doc', doc_id, '--detail', 'with-ids'])
        return document_blocks(value['data']['document']['content'])

    def _save_response(self, date, name, response):
        atomic(self.home / 'lark-receipts' / date / (name + '.json'), response)

    def ensure(self, date, first):
        key = 'doc:' + date; day = self.state.get(key, {})
        if day.get('id'):
            return day
        response_path = self.home / 'lark-receipts' / date / 'create.json'
        if day.get('create_pending'):
            if response_path.exists():
                doc = json.loads(response_path.read_text(encoding='utf-8'))['data']['document']
                day['id'] = doc['document_id']; day['url'] = doc.get('url', '')
                self.state.put(key, day)
                return self.reconcile(date)
            raise RuntimeError('lark_create_unknown_use_recover_doc')
        title = date + ' 录音清稿与整理稿'
        items = [('title', title), ('h1', '口语清稿')] + blocks(first['clean']) + [('h1', '七删一改整理稿')] + blocks(first['distilled'])
        decision = {'audience': '录音者本人', 'reader_task': '连续阅读保留口吻的清稿及七删一改完整整理稿',
                    'genre_contract': None, 'adapter': None, 'presentation_mode': 'normal',
                    'visual_plan': {'reason': '原文编辑双版仅需段落和真实主题标题，不增加图像或画板', 'blocks': []}}
        init = self.command(['+script', '--command', 'init-draft', '--presentation-decision', json.dumps(decision, ensure_ascii=False), '--format', 'json'])
        work = (self.home / init['data']['workspace']).resolve()
        draft = (self.home / init['data']['draft_path']).resolve()
        if work.parent != self.home.resolve() or not work.name.startswith('draft_') or not draft.is_relative_to(work):
            raise ValueError('invalid draft workspace')
        try:
            atomic(draft, fragment(items))
            relative = '@./' + draft.relative_to(self.home.resolve()).as_posix()
            checked = self.command(['+script', '--command', 'parse', '--content', relative, '--format', 'json'])
            if checked.get('data', {}).get('assessment', {}).get('status') not in ('pass', 'passed'):
                raise RuntimeError('lark_draft_failed')
            day = {'create_pending': True, 'expected': expected(items), 'batch_ids': [first['id']], 'initial_batch': first['id'], 'initial_groups': first.get('_groups', [])}
            self.state.put(key, day)
            response = self.command(['+create', '--doc-format', 'xml', '--content', relative])
            self._save_response(date, 'create', response)
            doc = response['data']['document']
            day.update(id=doc['document_id'], url=doc.get('url', ''))
            self.state.put(key, day)
            if response['data'].get('warnings'):
                raise RuntimeError('lark_create_warnings_reconcile')
            return self.reconcile(date)
        finally:
            if work.exists():
                shutil.rmtree(work)

    def recover(self, date, doc_id):
        """Operator-selected ID only; validate expected content before committing binding."""
        day = self.state.get('doc:' + date, {})
        if not day.get('create_pending') or day.get('id'):
            raise ValueError('no ambiguous create to recover')
        current = self.fetch(doc_id)
        if shape(current) != day['expected']:
            raise ValueError('recovery document content mismatch')
        day['id'] = doc_id
        self.state.put('doc:' + date, day)
        return self.reconcile(date)

    def recover_append(self, date, retry_unapplied=False):
        """Explicit operator retry only when the entire base shape is still unchanged."""
        key = 'doc:' + date; day = self.state.get(key, {})
        pending = day.get('pending')
        if not pending:
            raise ValueError('no pending append')
        current = self.fetch(day['id'])
        if shape(current) == day['expected']:
            return self.reconcile(date)
        if not retry_unapplied or shape(current) != pending['base']:
            raise RuntimeError('partial_or_human_edit_requires_review')
        if day.get('snapshot') and not preserves_owned(day['snapshot'], current):
            raise RuntimeError('lark_owned_block_identity_changed')
        day.update(expected=pending['base'], snapshot=current, pending=None)
        self.state.put(key, day)
        return day

    def _bind_groups(self, day, groups, kind, current):
        if not groups:
            return
        rendered = []
        for group in groups:
            content = group.get(kind + '_rendered')
            if content is None:
                raise RuntimeError('group_block_ownership_requires_rendered_metadata')
            rendered.extend(blocks(content))
        if shape(current) != expected(rendered):
            raise RuntimeError('group_block_ownership_shape_mismatch')
        mapping = day.setdefault('group_blocks', {})
        offset = 0
        for group in groups:
            count = len(blocks(group[kind + '_rendered']))
            owned = current[offset:offset+count]
            if any(not x.get('id') for x in owned):
                raise RuntimeError('group_block_ownership_missing_ids')
            entry = mapping.setdefault(group['id'], {}).setdefault(kind, [])
            for block in owned:
                if block not in entry:
                    entry.append(block)
            day.setdefault('rendered_groups', {}).setdefault(kind, []).append({'id':group['id'],'rendered':group[kind+'_rendered']})
            offset += count

    def reconcile(self, date):
        key = 'doc:' + date; day = self.state.get(key)
        current = self.fetch(day['id'])
        if shape(current) != day['expected']:
            raise RuntimeError('lark_human_edit_or_pending_outcome_preserved')
        if day.get('snapshot') and not preserves_owned(day['snapshot'], current):
            raise RuntimeError('lark_owned_block_identity_changed')
        if any(not x.get('id') for x in current if x['tag'] != 'title'):
            raise ValueError('document block ids missing')
        pending = day.get('pending') or {}
        if pending.get('group_bindings'):
            begin = pending['insert_at']; end = begin + pending['insert_count']
            self._bind_groups(day, pending['group_bindings'], pending['kind'], current[begin:end])
        if day.get('initial_groups') and not day.get('initial_groups_bound'):
            for kind, label in [('clean', '口语清稿'), ('distilled', '七删一改整理稿')]:
                begin = next(i for i,x in enumerate(current) if x['tag']=='h1' and x['text']==label) + 1
                end = next((i for i in range(begin,len(current)) if current[i]['tag']=='h1'), len(current))
                self._bind_groups(day, day['initial_groups'], kind, current[begin:end])
            day['initial_groups_bound'] = True
        if pending.get('done_key'):
            self.state.put(day['pending']['done_key'], True)
        day.update(create_pending=False, snapshot=current, pending=None)
        self.state.put(key, day)
        return day

    def append(self, date, batch):
        key = 'doc:' + date; day = self.ensure(date, batch)
        if day.get('pending'):
            day = self.reconcile(date)
        if batch['id'] in day.get('batch_ids', []):
            return self.reconcile(date)
        for kind, label in [('clean', '口语清稿'), ('distilled', '七删一改整理稿')]:
            # A durable per-half checkpoint prevents repeating clean when distilled fails.
            done_key = 'published-half:' + date + ':' + batch['id'] + ':' + kind
            if self.state.get(done_key):
                continue
            current = self.fetch(day['id'])
            if identities(current) != identities(day['snapshot']):
                raise RuntimeError('lark_human_edit_preserved')
            section = next(i for i, x in enumerate(current) if x['tag'] == 'h1' and x['text'] == label)
            end = next((i for i in range(section + 1, len(current)) if current[i]['tag'] == 'h1'), len(current))
            anchor = current[end - 1]['id']
            insertion = blocks(batch[kind])
            if not insertion:
                self.state.put(done_key, True)
                continue
            target = shape(current[:end]) + expected(insertion) + shape(current[end:])
            day.update(expected=target, pending={'batch': batch['id'], 'kind': kind, 'done_key': done_key, 'base': shape(current), 'group_bindings': batch.get('_groups', []), 'insert_at': end, 'insert_count': len(insertion)})
            self.state.put(key, day)
            path = self.home / 'fragments' / (batch['id'] + '-' + kind + '.xml')
            atomic(path, fragment(insertion))
            response = self.command(['+update', '--doc', day['id'], '--command', 'block_insert_after', '--block-id', anchor,
                                     '--doc-format', 'xml', '--content', '@./' + path.relative_to(self.home).as_posix()])
            self._save_response(date, batch['id'] + '-' + kind, response)
            if response['data'].get('result') != 'success' or response['data'].get('warnings'):
                raise RuntimeError('lark_partial_update_reconcile')
            day = self.reconcile(date)
            self.state.put(done_key, True)
        day['batch_ids'].append(batch['id'])
        self.state.put(key, day)
        return day

    def remove_retired_blocks(self, date, outputs, deleted_group_ids=()):
        """Retire exact persisted group-owned IDs, never match repeated prose by shape."""
        key = 'doc:' + date; day = self.state.get(key, {})
        if not day.get('id'):
            if day.get('create_pending'):
                raise RuntimeError('deletion_ambiguous_document_create')
            return
        if day.get('pending'):
            day = self.reconcile(date)
        current = self.fetch(day['id'])
        day = self._settle_rewrite(date,day,current)
        pending = day.get('delete_pending')
        if pending:
            if identities(current) == pending.get('after_identity'):
                day.update(snapshot=current, expected=shape(current), delete_pending=None)
                self.state.put(key, day)
            elif identities(current) != pending.get('before_identity'):
                raise RuntimeError('deletion_human_edit_preserved')
        if identities(current) != identities(day.get('snapshot', [])):
            raise RuntimeError('deletion_owned_block_identity_changed')
        desired = [('title', date + ' 录音清稿与整理稿'), ('h1', '口语清稿')]
        for output in outputs:
            desired += blocks(output['clean'])
        desired += [('h1', '七删一改整理稿')]
        for output in outputs:
            desired += blocks(output['distilled'])
        target = expected(desired)
        mapping = day.get('group_blocks', {})
        owned = [block for group in deleted_group_ids for group_kind in mapping.get(group, {}).values() for block in group_kind]
        owned_by_id = {block['id']: block for block in owned}
        if len(owned_by_id) != len(owned):
            raise RuntimeError('deletion_ambiguous_owned_block')
        actual_by_id = {block.get('id'): block for block in current}
        for identity, original in owned_by_id.items():
            if identity in actual_by_id and actual_by_id[identity] != original:
                raise RuntimeError('deletion_owned_block_content_changed')
        remaining = [block for block in current if block.get('id') not in owned_by_id]
        if day.get('group_projection_complete'):
            mapped=[block['id'] for entry in mapping.values() for values in entry.values() for block in values]
            body_ids=[block['id'] for block in current if block['tag'] not in ('title','h1')]
            if len(mapped)!=len(set(mapped)) or not set(body_ids).issubset(set(mapped)) or not (set(mapped)-set(body_ids)).issubset(set(owned_by_id)):
                raise RuntimeError('deletion_projection_ownership_incomplete')
            target=shape(remaining)
        elif shape(remaining) != target:
            raise RuntimeError('deletion_group_ownership_unavailable_or_changed')
        for index in reversed(range(len(current))):
            block = current[index]
            if block.get('id') not in owned_by_id:
                continue
            if block['tag'] in ('title','h1'):
                raise RuntimeError('deletion_protected_heading')
            before = current; after = current[:index]+current[index+1:]
            day['delete_pending'] = {'before_identity': identities(before), 'after_identity': identities(after)}
            self.state.put(key, day)
            response = self.command(['+update','--doc',day['id'],'--command','block_delete','--block-id',block['id']])
            if response.get('data',{}).get('warnings'):
                raise RuntimeError('deletion_warnings_requires_reconcile')
            verified = self.fetch(day['id'])
            if identities(verified) != identities(after):
                raise RuntimeError('deletion_outcome_requires_reconcile')
            current=verified
            day.update(snapshot=current, expected=shape(current), delete_pending=None)
            self.state.put(key, day)
        for group in deleted_group_ids:
            day.get('group_blocks', {}).pop(group, None)
        for kind in ('clean','distilled'):
            if kind in day.get('rendered_groups',{}):day['rendered_groups'][kind]=[entry for entry in day['rendered_groups'][kind] if entry['id'] not in deleted_group_ids]
        if day.get('rewrite_revision'):
            day.update(rewrite_revision=None,rewritten_kinds=[],last_rewrite_revision=None,revision_interrupted_by_retirement=True)
        self.state.put(key,day)

    @staticmethod
    def _revision_signature(xml):
        tree=ET.fromstring('<root>'+xml+'</root>')
        for element in tree.iter():
            if element.tag not in ('root','title','h1','h2','p','br','b') or set(element.attrib)-{'id'}:
                raise RuntimeError('day_revision_custom_format_preserved')
        return [(e.tag,re.sub(r'\s+','',e.text or ''),re.sub(r'\s+','',e.tail or '')) for e in tree.iter()]

    def _full_snapshot(self, document_id):
        response=self.command(['+fetch','--doc',document_id,'--detail','full'])
        raw=response['data']['document']['content']
        return response,raw,document_blocks(raw)

    @staticmethod
    def _revision_groups(batches, kind):
        result=[]
        for batch in batches:
            groups=batch.get('conversation_groups',[])
            if all(kind+'_rendered' in group for group in groups) and groups:
                result.extend(groups);continue
            if not groups and not batch['output'][kind].strip():continue
            pieces=batch['output'][kind].split('\n\n## ')
            if len(pieces)!=len(groups):raise RuntimeError('legacy_group_projection_unavailable')
            result.extend(dict(group,**{kind+'_rendered':('## ' if index else '')+pieces[index]}) for index,group in enumerate(groups))
        return result

    def _set_section_projection(self, day, groups, kind, current):
        label='口语清稿' if kind=='clean' else '七删一改整理稿'
        start=next(i for i,x in enumerate(current) if x['tag']=='h1' and x['text']==label)+1
        end=next((i for i in range(start,len(current)) if current[i]['tag']=='h1'),len(current))
        for entry in day.setdefault('group_blocks',{}).values():entry.pop(kind,None)
        day.setdefault('rendered_groups',{})[kind]=[]
        self._bind_groups(day,groups,kind,current[start:end])
        expected_ids=[item['id'] for entry in day['group_blocks'].values() for values in entry.values() for item in values]
        body_ids=[item['id'] for item in current if item['tag'] not in ('title','h1')]
        day['group_projection_complete']=len(expected_ids)==len(set(expected_ids)) and set(expected_ids)==set(body_ids)

    @staticmethod
    def _rendered_day_items(date, day):
        result=[('title',date+' 录音清稿与整理稿'),('h1','口语清稿')]
        for entry in day.get('rendered_groups',{}).get('clean',[]):result+=blocks(entry['rendered'])
        result+=[('h1','七删一改整理稿')]
        for entry in day.get('rendered_groups',{}).get('distilled',[]):result+=blocks(entry['rendered'])
        return result

    def _settle_rewrite(self, date, day, current, raw=None):
        pending=day.get('rewrite_pending')
        if not pending:return day
        if raw is None:
            _,raw,latest=self._full_snapshot(day['id'])
            if identities(latest)!=identities(current):raise RuntimeError('day_revision_changed_during_reconcile')
        expected_day=dict(day,rendered_groups=dict(day.get('rendered_groups',{})))
        expected_day['rendered_groups'][pending['kind']]=[{'id':g['id'],'rendered':g[pending['kind']+'_rendered']} for g in pending['replacement_groups']]
        if self._revision_signature(raw)!=self._revision_signature(fragment(self._rendered_day_items(date,expected_day))):
            raise RuntimeError('day_revision_custom_format_preserved')
        kept={item[0] for item in pending['untouched_identity']}
        if shape(current)!=pending['target_shape'] or identities([x for x in current if x.get('id') in kept])!=pending['untouched_identity']:
            raise RuntimeError('day_revision_outcome_unknown_preserved')
        self._set_section_projection(day,pending['replacement_groups'],pending['kind'],current)
        day.update(snapshot=current,expected=shape(current),rewrite_pending=None)
        day.setdefault('rewritten_kinds',[]).append(pending['kind'])
        self.state.put('doc:'+date,day)
        return day

    def revise_managed_day(self, date, before_batches, after_batches, backup_directory, guard_factory=None):
        """Journaled machine-owned section revision, independently retirement-safe."""
        import contextlib
        import hashlib
        factory=guard_factory or contextlib.nullcontext
        key='doc:'+date;day=self.state.get(key,{})
        if not day.get('id') or day.get('pending') or day.get('delete_pending'):
            raise RuntimeError('day_revision_requires_settled_managed_document')
        def items(batches):
            result=[('title',date+' 录音清稿与整理稿'),('h1','口语清稿')]
            for batch in batches:result+=blocks(batch['output']['clean'])
            result+=[('h1','七删一改整理稿')]
            for batch in batches:result+=blocks(batch['output']['distilled'])
            return result
        target_items=items(after_batches)
        revision=hashlib.sha256(fragment(target_items).encode()).hexdigest()
        with factory():
            response,raw,current=self._full_snapshot(day['id'])
            day=self._settle_rewrite(date,day,current,raw)
            if identities(current)!=identities(day.get('snapshot',[])):
                raise RuntimeError('day_revision_human_identity_edit_preserved')
            if day.get('last_rewrite_revision')==revision and shape(current)==expected(target_items):
                if self._revision_signature(raw)!=self._revision_signature(fragment(target_items)):raise RuntimeError('day_revision_custom_format_preserved')
                return  # Cloud committed before a local crash: exact completed outcome, no new call.
            if day.get('rewrite_revision') not in (None,revision):raise RuntimeError('another_day_revision_in_progress')
            original_items=self._rendered_day_items(date,day) if day.get('group_projection_complete') else items(before_batches)
            if shape(current)!=expected(original_items) or self._revision_signature(raw)!=self._revision_signature(fragment(original_items)):
                raise RuntimeError('day_revision_source_or_format_changed')
            if not day.get('group_projection_complete'):
                for kind in ('clean','distilled'):
                    self._set_section_projection(day,self._revision_groups(before_batches,kind),kind,current)
                if not day['group_projection_complete']:raise RuntimeError('day_revision_incomplete_ownership')
            if not day.get('rewrite_revision'):
                atomic(Path(backup_directory)/'before-cloud.json',response)
                day.update(rewrite_revision=revision,rewritten_kinds=[]);self.state.put(key,day)
        for kind,label in [('distilled','七删一改整理稿'),('clean','口语清稿')]:
            if kind in day.get('rewritten_kinds',[]):continue
            with factory():
                # Reload durable state so deletion/another worker cannot be overwritten.
                day=self.state.get(key)
                if day.get('rewrite_revision')!=revision:raise RuntimeError('day_revision_interrupted')
                _,raw,current=self._full_snapshot(day['id'])
                if identities(current)!=identities(day['snapshot']):raise RuntimeError('day_revision_human_edit_preserved')
                if self._revision_signature(raw)!=self._revision_signature(fragment(self._rendered_day_items(date,day))):
                    raise RuntimeError('day_revision_custom_format_preserved')
                start=next(i for i,x in enumerate(current) if x['tag']=='h1' and x['text']==label)+1
                end=next((i for i in range(start,len(current)) if current[i]['tag']=='h1'),len(current))
                replacement=[]
                for batch in after_batches:replacement+=blocks(batch['output'][kind])
                replacement_groups=self._revision_groups(after_batches,kind)
                untouched=current[:start]+current[end:]
                target=shape(current[:start])+expected(replacement)+shape(current[end:])
                if shape(current[start:end])==expected(replacement):
                    self._set_section_projection(day,replacement_groups,kind,current)
                    day.setdefault('rewritten_kinds',[]).append(kind);self.state.put(key,day);continue
                day['rewrite_pending']={'kind':kind,'target_shape':target,'untouched_identity':identities(untouched),'replacement_groups':replacement_groups}
                self.state.put(key,day)
                if replacement:
                    path=self.home/'fragments'/('revision-'+revision+'-'+kind+'.xml');atomic(path,fragment(replacement))
                    arguments=['+update','--doc',day['id'],'--command','block_replace' if end>start else 'block_insert_after']
                    arguments+=['--start-block-id',current[start]['id'],'--end-block-id',current[end-1]['id']] if end>start else ['--block-id',current[start-1]['id']]
                    arguments+=['--doc-format','xml','--content','@./'+path.relative_to(self.home).as_posix()]
                else:arguments=['+update','--doc',day['id'],'--command','block_delete','--start-block-id',current[start]['id'],'--end-block-id',current[end-1]['id']]
                changed=self.command(arguments);atomic(Path(backup_directory)/('cloud-'+kind+'-response.json'),changed)
                _,verified_raw,verified=self._full_snapshot(day['id'])
                if changed.get('data',{}).get('warnings'):raise RuntimeError('day_revision_readback_requires_review')
                day=self._settle_rewrite(date,day,verified,verified_raw)  # Binding is durable BEFORE releasing this range guard.
        with factory():
            day=self.state.get(key);_,raw,current=self._full_snapshot(day['id'])
            if self._revision_signature(raw)!=self._revision_signature(fragment(target_items)):
                raise RuntimeError('day_revision_custom_format_preserved')
            if identities(current)!=identities(day['snapshot']) or shape(current)!=expected(target_items) or not day.get('group_projection_complete'):
                raise RuntimeError('day_revision_final_readback_mismatch')
            day.update(last_rewrite_revision=revision,rewrite_revision=None,rewritten_kinds=[])
            self.state.put(key,day)
