#!/usr/bin/env python3
"""Local processing pipeline on the receiver computer (Mac or Windows).

receiver inbox -> local faster-whisper ASR -> Markdown notes (Obsidian or any folder)
                                           -> optional Feishu/Lark daily document (off by default)

No cloud ASR fallback. Originals are retained. Run as the logged-in user (never as a
service account) so that an optional `lark-cli` login stays in that user's profile.

    python3 processor/pipeline.py            # loop forever (service mode)
    python3 processor/pipeline.py --once     # one scan, then exit
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'receiver'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_store import replace, sync_directory, lock_exclusive  # noqa: E402
import asr_worker  # noqa: E402
from retirement_bridge import source_lease, protected_pull  # noqa: E402

EXTS = {'.m4a', '.wav', '.mp3', '.aac', '.flac', '.ogg', '.mp4', '.caf'}
NOTE_DIR = '录音资料'
SNAPSHOT_DIR = '飞书修订快照'

# Runtime configuration. configure() fills these from data/settings.json.
HOME = REPO / 'data'
RECEIVER_INBOX = HOME / 'receiver' / 'inbox'
NOTES_ROOT = HOME / 'notes'
NOTES = NOTES_ROOT / NOTE_DIR
FEISHU = False
WRITE_MANIFEST = False
PUBLISH_OUTPUTS = True
TZ = ZoneInfo('Asia/Shanghai')
ASR = {}
_processor = None


def configure(settings, home):
    """Apply settings.json values to module state. Paths in settings are absolute."""
    global HOME, RECEIVER_INBOX, NOTES_ROOT, NOTES, FEISHU, WRITE_MANIFEST, PUBLISH_OUTPUTS, TZ, ASR, _processor
    HOME = Path(home).resolve()
    RECEIVER_INBOX = Path(settings.get('receiver', {}).get('root') or HOME / 'receiver') / 'inbox'
    NOTES_ROOT = Path(settings['notes']['dir'])
    NOTES = NOTES_ROOT / NOTE_DIR
    PUBLISH_OUTPUTS = settings['notes'].get('publish_outputs', True)
    if not isinstance(PUBLISH_OUTPUTS, bool):
        raise ValueError('notes.publish_outputs must be boolean')
    FEISHU = bool(settings.get('feishu', {}).get('enabled', False)) and PUBLISH_OUTPUTS
    WRITE_MANIFEST = bool(settings['notes'].get('manifest', False))
    TZ = ZoneInfo(settings.get('timezone') or 'Asia/Shanghai')
    ASR = dict(settings.get('asr', {}))
    _processor = None
    if settings.get('ffmpeg_dir'):
        os.environ['PATH'] = str(settings['ffmpeg_dir']) + os.pathsep + os.environ.get('PATH', '')
    for name in ('inbox', 'outbox', 'state', 'work', 'archive'):
        (HOME / 'asr' / name).mkdir(parents=True, exist_ok=True)
    (HOME / 'state').mkdir(parents=True, exist_ok=True)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.pending')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    replace(temp, path)
    sync_directory(path.parent)


def _resolve_command(argv):
    if argv[0] != 'lark-cli' or os.name != 'nt':
        return argv
    # npm installs a .cmd shim on Windows; prefer an executable or the Node entry point.
    exe = shutil.which('lark-cli.exe')
    if exe:
        return [exe, *argv[1:]]
    node = shutil.which('node')
    entry = Path(os.environ.get('APPDATA', '')) / 'npm' / 'node_modules' / '@larksuite' / 'cli' / 'scripts' / 'run.js'
    if node and entry.is_file():
        return [node, str(entry), *argv[1:]]
    shim = shutil.which('lark-cli.cmd') or shutil.which('lark-cli')
    if shim:
        return [shim, *argv[1:]]
    raise RuntimeError('lark-cli not found; install it or set feishu.enabled=false')


def command(argv, timeout=120, cwd=None):
    p = subprocess.run(_resolve_command(argv), text=True, capture_output=True, timeout=timeout, cwd=cwd, encoding='utf-8')
    if p.returncode:
        raise RuntimeError(f'{Path(argv[0]).name} exit {p.returncode}: {p.stderr[-1500:]} {p.stdout[-1000:]}')
    return p.stdout


def lark(args, cwd=None):
    data = json.loads(command(['lark-cli', 'docs', *args, '--as', 'user'], cwd=cwd))
    if not data.get('ok'):
        raise RuntimeError('lark operation failed: ' + json.dumps(data, ensure_ascii=False)[:1500])
    return data


def db_open():
    (HOME / 'state').mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(HOME / 'state' / 'pipeline.sqlite3')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS completed_inputs (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, job_key TEXT NOT NULL)')
    db.commit()
    return db


def save(db, key, value):
    value['updated_at'] = now()
    db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
    db.commit()


def date_valid(value):
    dt.date.fromisoformat(value)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('capture_date must be YYYY-MM-DD')
    return value


def fingerprint(path):
    s = path.stat()
    return f'{s.st_dev}:{s.st_ino}:{s.st_size}:{s.st_mtime_ns}'


def hash_stable(path):
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTS:
        raise ValueError('Only regular audio input files are accepted')
    before = fingerprint(path)
    if time.time() - path.stat().st_mtime < 2:
        raise RuntimeError('Input recently changed; retry after it is finished')
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    if fingerprint(path) != before:
        raise RuntimeError('Input changed during hashing')
    return h.hexdigest(), before


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def transfer(path, sha, capture_date, test, source_url, original_name, date_basis, app_metadata=None):
    """Stage one audio file into the local ASR queue (metadata first, audio last)."""
    target = HOME / 'asr' / 'inbox' / (sha + path.suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    meta = {'source_id': sha, 'capture_date': capture_date, 'original_name': original_name, 'capture_date_basis': date_basis,
            'is_test': test, 'source_url': source_url, 'app_metadata': app_metadata}
    atomic_json(target.with_suffix(target.suffix + '.json'), meta)
    if not target.exists():
        temporary = target.with_suffix(target.suffix + '.part')
        with path.open('rb') as src, temporary.open('wb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if _sha256(temporary) != sha:
            temporary.unlink(missing_ok=True)
            raise RuntimeError('local copy identity mismatch')
        replace(temporary, target)
    elif _sha256(target) != sha:
        raise RuntimeError('local queue identity conflict')


def run_asr():
    """Transcribe everything queued in data/asr/inbox with the configured local model."""
    global _processor
    root = HOME / 'asr'
    if _processor is None:
        language = ASR.get('language', 'auto')
        if language is not None and (not isinstance(language, str) or not language.isalpha()):
            raise ValueError('ASR language must be auto or a language code')
        if not ASR.get('model'):
            raise RuntimeError('No local ASR model configured; run: python3 scripts/setup.py --download-model')
        args = SimpleNamespace(root=root, model=ASR['model'], device=ASR.get('device', 'cpu'),
                               compute_type=ASR.get('compute_type', 'int8'), language=language,
                               multilingual=bool(ASR.get('multilingual', True)), chunk_seconds=600,
                               job_timeout=int(ASR.get('job_timeout', 3600)), capture_date=None,
                               cpu_threads=int(ASR.get('cpu_threads', 4)))
        _processor = asr_worker.Processor(args)
    with asr_worker.exclusive_lock(root):
        for source in sorted((root / 'inbox').iterdir()):
            if source.is_file() and not source.is_symlink() and source.suffix.lower() in asr_worker.EXTENSIONS:
                _processor.process(source)


@protected_pull
def pull(sha, dest):
    dest.mkdir(parents=True, exist_ok=True)
    for name in ['receipt.json', 'transcript.json', 'transcript.md']:
        source = HOME / 'asr' / 'outbox' / sha / name
        temporary = dest / (name + '.part')
        with source.open('rb') as src, temporary.open('wb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        replace(temporary, dest / name)
    receipt = json.loads((dest / 'receipt.json').read_text(encoding='utf-8'))
    if receipt.get('source_id') != sha or receipt.get('status') not in ['done', 'completed', 'success']:
        raise RuntimeError('local ASR receipt mismatch')
    return receipt


def original_candidates(segments):
    cues = re.compile('决定|需要|计划|学到|发现|记住|问题|打算|应该')
    matches = []
    for segment in segments:
        text = segment.get('text', '')
        if cues.search(text):
            matches.append(f"[{float(segment.get('start', 0)):.1f}–{float(segment.get('end', 0)):.1f}] {text}")
        if len(matches) == 5:
            break
    return matches or ['未发现明确候选；全文仍保留在逐字稿中。']


def segments_of(receipt, dest):
    data = json.loads((dest / 'transcript.json').read_text(encoding='utf-8'))
    return receipt.get('segments') or (data.get('segments', []) if isinstance(data, dict) else data)


# ---------------------------------------------------------------- optional Feishu/Lark

def create_doc(title, lines, job, db, key, dest):
    decision = {'audience': '录音者本人', 'reader_task': '核对带时间戳的录音转写、原始来源与测试标记',
                'genre_contract': None, 'adapter': None, 'presentation_mode': 'normal',
                'visual_plan': {'reason': '原始转写存档用来源和时间戳段落，无需图像或画板', 'blocks': []}}
    init = json.loads(command(['lark-cli', 'docs', '+script', '--command', 'init-draft',
                               '--presentation-decision', json.dumps(decision, ensure_ascii=False), '--format', 'json'], cwd=HOME))
    if not init.get('ok'):
        raise RuntimeError('Could not initialize Lark draft')
    work = HOME / init['data']['workspace']
    draft = HOME / init['data']['draft_path']
    try:
        xml = '<title>' + html.escape(title) + '</title>\n' + '\n'.join('<p>' + html.escape(x) + '</p>' for x in lines)
        draft.write_text(xml, encoding='utf-8')
        parsed = json.loads(command(['lark-cli', 'docs', '+script', '--command', 'parse',
                                     '--content', '@./' + draft.relative_to(HOME).as_posix(), '--format', 'json'], cwd=HOME))
        atomic_json(dest / 'lark-draft-check.json', parsed)
        status = parsed.get('data', {}).get('assessment', {}).get('status')
        if not parsed.get('ok') or status not in ['pass', 'passed']:
            raise RuntimeError('Lark draft profile failed: ' + str(status))
        # Mark before external create: an ambiguous network failure must not duplicate documents.
        job['lark_create_pending'] = True
        save(db, key, job)
        result = lark(['+create', '--doc-format', 'xml', '--content', '@./' + draft.relative_to(HOME).as_posix()], cwd=HOME)
        atomic_json(dest / 'lark-create.json', result)
        document = result['data']['document']
        job['lark_doc_id'] = document['document_id']
        job['lark_url'] = document.get('url', '')
        job['lark_create_pending'] = False
        save(db, key, job)
        if result['data'].get('warnings'):
            raise RuntimeError('Lark create returned warnings; inspect saved receipt')
    finally:
        if work.parent == HOME and work.name.startswith('draft_'):
            shutil.rmtree(work)


def publish_daily(title, lines, job, db, key, dest):
    daily_key = 'daily:' + ('test:' if job['is_test'] else 'real:') + job['capture_date']
    row = db.execute('SELECT payload FROM jobs WHERE id=?', (daily_key,)).fetchone()
    daily = json.loads(row[0]) if row else {'source_id': job['source_id'], 'capture_date': job['capture_date'], 'is_test': job['is_test']}
    if not daily.get('lark_doc_id'):
        if daily.get('lark_create_pending'):
            raise RuntimeError('Daily Lark create outcome unknown; run recover-doc with the daily job key')
        daily_title = ('【测试】' if job['is_test'] else '') + job['capture_date'] + '_录音日汇总'
        create_doc(daily_title, lines, daily, db, daily_key, dest)
    else:
        before = lark(['+fetch', '--doc', daily['lark_doc_id'], '--detail', 'full'])
        current = before['data']['document']['content']
        if job['source_id'] in current and ('record_end: ' + job['source_id']) not in current:
            raise RuntimeError('Partial daily recording block exists; reconcile before retry')
        pending = daily.get('append_pending')
        if pending and ('record_end: ' + pending) not in current:
            raise RuntimeError('Daily append outcome unknown; inspect document and clear pending only after reconciliation')
        if pending:
            daily['append_pending'] = None
            save(db, daily_key, daily)
        if job['source_id'] not in current:
            fragment = dest / 'lark-append.xml'
            fragment.write_text('\n'.join('<p>' + html.escape(x) + '</p>' for x in lines), encoding='utf-8')
            daily['append_pending'] = job['source_id']
            save(db, daily_key, daily)
            result = lark(['+update', '--doc', daily['lark_doc_id'], '--command', 'append', '--doc-format', 'xml',
                           '--content', '@./' + fragment.relative_to(HOME).as_posix()], cwd=HOME)
            atomic_json(dest / 'lark-append.json', result)
            if result['data'].get('warnings') or result['data'].get('result') != 'success':
                raise RuntimeError('Daily append returned warnings/partial result; inspect before retry')
    daily['append_pending'] = None
    save(db, daily_key, daily)
    job.update(lark_doc_id=daily['lark_doc_id'], lark_url=daily.get('lark_url', ''), lark_create_pending=False)
    save(db, key, job)


def sync_documents(db, doc_ids=None):
    """One-way Lark revisions -> isolated snapshot notes; never replaces edited notes."""
    snapshots = NOTES_ROOT / SNAPSHOT_DIR
    snapshots.mkdir(parents=True, exist_ok=True)
    docs = {}
    for row in db.execute('SELECT payload FROM jobs'):
        job = json.loads(row[0])
        if job.get('lark_doc_id') and (doc_ids is None or job['lark_doc_id'] in doc_ids):
            docs[job['lark_doc_id']] = job
    for doc_id, job in docs.items():
        raw = lark(['+fetch', '--doc', doc_id, '--doc-format', 'markdown', '--detail', 'simple'])
        doc = raw['data']['document']
        revision = str(doc['revision_id'])
        if not re.fullmatch(r'[A-Za-z0-9_-]+', doc_id + revision):
            raise RuntimeError('Unexpected document identifier')
        target = snapshots / f"{job['capture_date']}_{doc_id}_修订{revision}.md"
        text = '# 飞书修订快照\n\n' + ('【测试】测试样本，非真实事件。\n\n' if job.get('is_test') else '')
        text += f"来源文档：{job.get('lark_url', doc_id)}\n\n修订：{revision}\n\n" + doc['content'] + '\n'
        if target.exists():
            if target.read_text(encoding='utf-8') != text:
                raise RuntimeError('Existing revision snapshot changed by a human; preserving it')
        else:
            with target.open('x', encoding='utf-8') as f:
                f.write(text)


# ---------------------------------------------------------------- one recording

def process(db, path, capture_date, is_test=False, source_url=None, no_lark=None, original_name=None, date_basis='explicit', app_metadata=None):
    sha,_=hash_stable(path)
    with source_lease(sha) as lease:
        if lease.retired:return {'source_id':sha,'capture_date':capture_date,'status':'retired','publication_status':'owner_deleted'}
        return _process_unretired(db,path,capture_date,is_test,source_url,no_lark,original_name,date_basis,app_metadata,_lease=lease)


def _process_unretired(db, path, capture_date, is_test=False, source_url=None, no_lark=None, original_name=None, date_basis='explicit', app_metadata=None, _lease=None):
    use_feishu = FEISHU if no_lark is None else not no_lark
    date_valid(capture_date)
    original_name = original_name or path.name
    sha, fp = hash_stable(path)
    key = capture_date + ':' + sha
    for other_key, payload in db.execute("SELECT id,payload FROM jobs WHERE id NOT LIKE 'daily:%'"):
        other = json.loads(payload)
        if other.get('source_id') == sha and other_key != key:
            raise ValueError('Same audio already registered with another capture_date; preserve source provenance')
    row = db.execute('SELECT payload FROM jobs WHERE id=?', (key,)).fetchone()
    job = json.loads(row[0]) if row else {'source_id': sha, 'capture_date': capture_date, 'created_at': now(),
                                          'is_test': is_test, 'source_url': source_url, 'original_name': original_name,
                                          'capture_date_basis': date_basis, 'attempts': 0}
    if app_metadata is not None:
        if not isinstance(app_metadata, dict) or app_metadata.get('source_app') != 'watch-memory':
            raise ValueError('Invalid App source identity')
        if job.get('app_metadata', app_metadata) != app_metadata:
            raise ValueError('Same job cannot change App session/chunk provenance')
        job['app_metadata'] = app_metadata
    expected_identity = {'is_test': is_test, 'source_url': source_url, 'original_name': original_name, 'capture_date_basis': date_basis}
    if any(field in job and job[field] != value for field, value in expected_identity.items()):
        raise ValueError('Same job cannot change source provenance')
    if job.get('status') in {'done', 'no_speech'}:
        return job
    job['attempts'] += 1
    dest = HOME / 'receipts' / capture_date / sha
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if job.get('lark_create_pending') and not job.get('lark_doc_id'):
            # A saved create response recovers after a crash between response persistence and DB commit.
            response = dest / 'lark-create.json'
            if response.exists():
                doc = json.loads(response.read_text(encoding='utf-8'))['data']['document']
                job.update(lark_doc_id=doc['document_id'], lark_url=doc.get('url', ''), lark_create_pending=False)
            else:
                raise RuntimeError('Lark create outcome unknown: reconcile the existing document before retry; use recover-doc')
        if not job.get('remote_done'):
            transfer(path, sha, capture_date, is_test, source_url, original_name, date_basis, app_metadata)
            if fingerprint(path) != fp:
                raise RuntimeError('Input changed while queueing')
            if _lease:
                _lease.run_unlocked(run_asr)
                if _lease.retired:return {'source_id':sha,'capture_date':capture_date,'status':'retired','publication_status':'owner_deleted'}
            else:run_asr()
            receipt = pull(sha, dest)
            if receipt.get('capture_date') != capture_date or receipt.get('original_name') != original_name:
                raise RuntimeError('ASR receipt capture_date/original_name mismatch; reconcile source provenance')
            if receipt.get('is_test') is not is_test or receipt.get('source_url') != source_url or receipt.get('capture_date_basis') != date_basis:
                raise RuntimeError('ASR receipt test/source/date-basis mismatch; refuse provenance substitution')
            job['remote_done'] = True  # field name kept for receipt compatibility: "ASR finished"
            save(db, key, job)
        receipt = json.loads((dest / 'receipt.json').read_text(encoding='utf-8'))
        if receipt.get('capture_date') != capture_date or receipt.get('original_name') != original_name or receipt.get('source_id') != sha:
            raise RuntimeError('ASR receipt date/name/hash identity mismatch')
        if receipt.get('is_test') is not is_test or receipt.get('source_url') != source_url or receipt.get('capture_date_basis') != date_basis:
            raise RuntimeError('ASR receipt test/source/date-basis mismatch; refuse provenance substitution')
        segments = segments_of(receipt, dest)
        if not PUBLISH_OUTPUTS:
            job.update(status='done' if segments else 'no_speech', remote_done=True,
                       output_policy='asr_only', publication_status='disabled', lark_verified=False,
                       note_path=None, last_error=None)
            save(db, key, job)
            atomic_json(dest / 'client-receipt.json', job)
            return job
        if app_metadata is not None and not segments:
            job.update(status='no_speech', note_path=None, last_error=None, consumed=True,
                       processing_result='No speech segments; raw audio and App event metadata retained, no empty note created')
            save(db, key, job)
            atomic_json(dest / 'client-receipt.json', job)
            return job
        label = '【测试】' if is_test else ''
        title = f'{label}{capture_date}_手表录音_{sha[:12]}'
        lines = ['测试音频，非真实事件。' if is_test else '机器转写待核对；不把对话自动视为本人观点。',
                 f'capture_date: {capture_date} | source_id: {sha}',
                 f'来源：{source_url or original_name}',
                 f'模型：{Path(str(receipt.get("model", "unknown"))).name}；本地转写，未启用云转写回退。',
                 '以下是机器逐字稿；未执行声纹身份识别、重叠语音分离或事实确认。']
        if app_metadata is not None:
            lines[1] += f" | App: watch-memory | session: {app_metadata['session_id']} | sequence: {app_metadata['sequence']} | chunk: {app_metadata['chunk_id']}"
        if date_basis == 'receive_date_unconfirmed':
            lines[1] += '；日期依据：接收日，实际录音日期未确认。'
        lines += [f'[{float(s.get("start", 0)):.1f}–{float(s.get("end", 0)):.1f}] {s.get("text", "")}' for s in segments]
        lines += ['原句摘录候选（规则筛选，待确认；不识别说话人，不自动写 Todo）'] + original_candidates(segments)
        lines.append('record_end: ' + sha)
        if not segments:
            raise RuntimeError('No transcript segments; do not publish an empty note')
        if use_feishu:
            if not job.get('lark_doc_id'):
                publish_daily(title, lines, job, db, key, dest)
            fetched = lark(['+fetch', '--doc', job['lark_doc_id'], '--detail', 'with-ids'])
            atomic_json(dest / 'lark-fetch.json', fetched)
            content = fetched['data']['document']['content']
            if sha not in content or lines[0] not in content or ('record_end: ' + sha) not in content or any(
                    html.escape(line) not in content and line not in html.unescape(content) for line in lines[5:]):
                raise RuntimeError('Lark readback missing provenance/test marker; keep human edits intact')
            job['lark_verified'] = True
            save(db, key, job)
        NOTES.mkdir(parents=True, exist_ok=True)
        note = NOTES / (title + '.md')
        text = f'---\ntype: 录音转写\ncapture_date: {capture_date}\nsource_id: {sha}\nis_test: {str(is_test).lower()}\n---\n\n# {title}\n\n'
        text += '\n\n'.join(lines[:5]) + '\n\n'
        if job.get('lark_url'):
            text += f'[飞书原始文档]({job["lark_url"]})\n\n'
        text += '## 转写原文\n\n' + '\n\n'.join(lines[5:]) + '\n'
        if note.exists():
            if note.read_text(encoding='utf-8') != text:
                raise RuntimeError('Existing note differs: preserving human changes, manual reconciliation required')
        else:
            with note.open('x', encoding='utf-8') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        if note.read_text(encoding='utf-8') != text:
            raise RuntimeError('Note readback mismatch')
        job.update(status='done', note_path=str(note), last_error=None)
        save(db, key, job)
        atomic_json(dest / 'client-receipt.json', job)
        if use_feishu and job.get('lark_doc_id'):
            sync_documents(db, doc_ids={job['lark_doc_id']})
        return job
    except Exception as exc:
        job.update(status='failed', last_error=str(exc))
        save(db, key, job)
        atomic_json(dest / 'client-receipt.json', job)
        raise


def scan(db, input_directory=None):
    """Process every receiver-published `<sha>.<ext>` + `.ready` marker that is stable."""
    input_directory = Path(input_directory) if input_directory is not None else RECEIVER_INBOX
    failures = []
    if not input_directory.is_dir():
        return 0
    for marker in sorted(input_directory.glob('*.ready')):
        try:
            metadata = json.loads(marker.read_text(encoding='utf-8'))
            path = marker.with_suffix('')
            if path.is_symlink() or path.suffix.lower() not in EXTS or not path.is_file():
                continue
            fp = fingerprint(path)
            completed_fp = fp + ':' + fingerprint(marker)
            completed = db.execute('SELECT fingerprint,job_key FROM completed_inputs WHERE path=?', (str(path),)).fetchone()
            if completed and completed[0] == completed_fp:
                row_done = db.execute('SELECT payload FROM jobs WHERE id=?', (completed[1],)).fetchone()
                if row_done and json.loads(row_done[0]).get('status') in {'done', 'no_speech'}:
                    continue
            row = db.execute('SELECT fingerprint FROM seen WHERE path=?', (str(path),)).fetchone()
            db.execute('INSERT OR REPLACE INTO seen VALUES (?,?)', (str(path), fp))
            db.commit()
            # Require two observations of an unchanged file at least 60 s old.
            if not row or row[0] != fp or time.time() - path.stat().st_mtime < 60:
                continue
            job = process(db, path, metadata['capture_date'], metadata.get('is_test', False), metadata.get('source_url'),
                          original_name=metadata.get('original_name'), date_basis=metadata.get('capture_date_basis', 'explicit'),
                          app_metadata=metadata.get('app_metadata'))
            if isinstance(job, dict) and job.get('status') in {'done', 'no_speech'} and fingerprint(path) == fp:
                db.execute('INSERT OR REPLACE INTO completed_inputs VALUES (?,?,?)', (str(path), completed_fp, job['capture_date'] + ':' + job['source_id']))
                db.commit()
        except Exception as exc:
            failures.append({'input': marker.name, 'error_type': type(exc).__name__, 'error': str(exc)[:300]})
    if failures:
        # File names are content hashes; no transcript text or credentials are printed.
        print(json.dumps({'event': 'pipeline_failures', 'failures': failures}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1
    return 0


_manifest_cache = {}


def write_manifest():
    """Windows receiver + Mac notes: publish a manifest that processor/sync_notes.py pulls."""
    if not WRITE_MANIFEST:
        return
    files = []
    for sub in (NOTE_DIR, SNAPSHOT_DIR):
        base = NOTES_ROOT / sub
        if not base.is_dir():
            continue
        for p in sorted(base.glob('*.md')):
            if p.is_symlink() or not p.is_file():
                raise RuntimeError('No symlinks in notes outbox')
            st = p.stat()
            ident = (str(p), st.st_size, st.st_mtime_ns)
            if ident not in _manifest_cache:
                _manifest_cache[ident] = _sha256(p)
            files.append({'relative_path': f'{sub}/{p.name}', 'sha256': _manifest_cache[ident], 'bytes': st.st_size})
    manifest = NOTES_ROOT / 'manifest.json'
    previous = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
    if previous.get('files') != files:
        atomic_json(manifest, {'version': 1, 'generated_at': now(), 'files': files})


def load_settings(home):
    path = Path(home) / 'settings.json'
    if not path.is_file():
        raise SystemExit(f'Missing {path}. Run: python3 scripts/setup.py')
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('XIAOLU_WATCH_HOME') or REPO / 'data'))
    parser.add_argument('--once', action='store_true', help='scan once and exit')
    sub = parser.add_subparsers(dest='action')
    sub.add_parser('status', help='print job states (no transcript text)')
    recover = sub.add_parser('recover-doc', help='attach an existing Feishu document to a job after an unknown create outcome')
    recover.add_argument('job')
    recover.add_argument('document_id')
    args = parser.parse_args()
    configure(load_settings(args.home), args.home)
    db = db_open()
    if args.action == 'status':
        for row in db.execute('SELECT id,payload FROM jobs ORDER BY id'):
            job = json.loads(row[1])
            print(json.dumps({'job': row[0], 'status': job.get('status'), 'attempts': job.get('attempts'),
                              'note': job.get('note_path'), 'last_error': job.get('last_error')}, ensure_ascii=False))
        return 0
    if args.action == 'recover-doc':
        row = db.execute('SELECT payload FROM jobs WHERE id=?', (args.job,)).fetchone()
        if not row:
            raise SystemExit('Unknown job')
        job = json.loads(row[0])
        fetched = lark(['+fetch', '--doc', args.document_id])
        if job['source_id'] not in fetched['data']['document']['content']:
            raise SystemExit('Document source_id does not match job')
        job.update(lark_doc_id=args.document_id, lark_create_pending=False)
        save(db, args.job, job)
        return 0
    db.close()
    with (HOME / 'state' / 'pipeline.lock').open('a+') as lock:
        lock_exclusive(lock)
        print(json.dumps({'event': 'pipeline_started', 'notes_dir': str(NOTES_ROOT), 'feishu': FEISHU}, ensure_ascii=False), flush=True)
        while True:
            db = db_open()
            try:
                scan(db)
                write_manifest()
            except Exception as exc:
                print(json.dumps({'event': 'pipeline_retry', 'error_type': type(exc).__name__}), flush=True)
            finally:
                db.close()
            if args.once:
                return 0
            time.sleep(30)


if __name__ == '__main__':
    sys.exit(main())
