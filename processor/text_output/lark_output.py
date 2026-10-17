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
            day = {'create_pending': True, 'expected': expected(items), 'batch_ids': [first['id']], 'initial_batch': first['id']}
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
        day.update(expected=pending['base'], snapshot=current, pending=None)
        self.state.put(key, day)
        return day

    def reconcile(self, date):
        key = 'doc:' + date; day = self.state.get(key)
        current = self.fetch(day['id'])
        if shape(current) != day['expected']:
            raise RuntimeError('lark_human_edit_or_pending_outcome_preserved')
        if any(not x.get('id') for x in current if x['tag'] != 'title'):
            raise ValueError('document block ids missing')
        if (day.get('pending') or {}).get('done_key'):
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
            if shape(current) != shape(day['snapshot']):
                raise RuntimeError('lark_human_edit_preserved')
            section = next(i for i, x in enumerate(current) if x['tag'] == 'h1' and x['text'] == label)
            end = next((i for i in range(section + 1, len(current)) if current[i]['tag'] == 'h1'), len(current))
            anchor = current[end - 1]['id']
            insertion = blocks(batch[kind])
            target = shape(current[:end]) + expected(insertion) + shape(current[end:])
            day.update(expected=target, pending={'batch': batch['id'], 'kind': kind, 'done_key': done_key, 'base': shape(current)})
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
