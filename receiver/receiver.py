#!/usr/bin/env python3
"""Sealed v2 audio chunk receiver (loopback/private bind). Durable ACK means stored, not transcribed."""
import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import threading
import time
import uuid
from platform_store import replace, sync_directory, lock_exclusive
# The public receiver always retains originals. Remote-archive custody and
# automatic garbage collection from the author's private deployment are not shipped.
class CustodyError(RuntimeError): pass
def verify_archive(*args): raise CustodyError("receiver retains original custody")
def collect_garbage(*args, **kwargs): raise CustodyError("receiver GC is disabled")
import wire_v2
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
# Runtime data (config, secret, audio, database) lives outside the source tree by default.
ROOT = Path(os.environ.get('XIAOLU_WATCH_RECEIVER_ROOT') or (REPO / 'data' / 'receiver')).resolve()
# capture_date (the "day" a recording belongs to) uses this zone; set "timezone" in config.
CAPTURE_TZ = ZoneInfo(os.environ.get('XIAOLU_WATCH_TIMEZONE') or 'Asia/Shanghai')
MAX_BYTES = 16 * 1024 * 1024
PRIVATE_NETS = [ipaddress.ip_network(s) for s in ['127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10']]

class RequestError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code

def private_address(value):
    try:
        addr = ipaddress.ip_address(value)
        return any(addr in net for net in PRIVATE_NETS)
    except ValueError:
        return False

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x',encoding='utf-8') as f:
        os.chmod(tmp,0o600);json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    replace(tmp,path)
    sync_directory(path.parent)

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)

def metadata(raw, sha, length, maximum_encoded_size=8192):
    if not raw or len(raw)>maximum_encoded_size:
        raise RequestError(400,'metadata_missing_or_too_large')
    try:
        value=json.loads(base64.b64decode(raw,validate=True).decode('utf-8'))
    except (ValueError,UnicodeError):raise RequestError(400,'invalid_metadata')
    if not isinstance(value,dict):raise RequestError(400,'invalid_metadata')
    required={'schema_version','chunk_id','session_id','sequence','capture_started_at','duration_s','sha256','byte_count','original_name','sample_rate_hz','channels'}
    if not required <= value.keys():raise RequestError(400,'missing_metadata_fields')
    try:
        if value['schema_version'] != 1 or type(value['schema_version']) is not int:raise ValueError()
        for name in ['chunk_id','session_id']:
            if str(uuid.UUID(value[name])) != value[name]:raise ValueError()
        if type(value['sequence']) is not int or not 0<=value['sequence']<2**31:raise ValueError()
        if type(value['byte_count']) is not int or value['byte_count'] != length:raise ValueError()
        if value['sha256'] != sha or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):raise ValueError()
        mime=value.get('mime','audio/x-caf')
        extension={'audio/x-caf':'.caf','audio/mp4':'.m4a'}.get(mime)
        if extension is None or value['original_name'] != value['chunk_id']+extension:raise ValueError()
        if type(value['sample_rate_hz']) is not int or type(value['channels']) is not int or value['sample_rate_hz'] != 16000 or value['channels'] != 1:raise ValueError()
        if type(value['duration_s']) not in [int,float] or not math.isfinite(value['duration_s']) or not 0<value['duration_s']<=600:raise ValueError()
        created=dt.datetime.fromisoformat(value['capture_started_at'].replace('Z','+00:00'))
        if created.tzinfo is None or created.utcoffset()!=dt.timedelta(0):raise ValueError()
        if created.timestamp()>time.time()+300:raise ValueError()
        if created.year<2020:raise ValueError()
        if value.get('source_app','watch-memory')!='watch-memory':raise ValueError()
        if not isinstance(value.get('device','Apple Watch'),str) or len(value.get('device','Apple Watch'))>80:raise ValueError()
        if type(value.get('is_test',False)) is not bool:raise ValueError()
        allowed=required|{'source_app','device','is_test','mime'}
        if set(value)-allowed:raise ValueError()
    except (ValueError,TypeError,AttributeError,OverflowError):raise RequestError(400,'invalid_metadata_fields')
    value.setdefault('mime','audio/x-caf');value['source_app']='watch-memory';value.setdefault('device','Apple Watch');value.setdefault('is_test',False)
    value['capture_date']=created.astimezone(CAPTURE_TZ).date().isoformat()
    return value

def media_check(path, meta, ffprobe_path=None):
    with path.open('rb') as f:
        magic=f.read(12)
        if meta['mime']=='audio/x-caf' and magic[:4]!=b'caff':raise RequestError(415,'invalid_caf_container')
        if meta['mime']=='audio/mp4' and magic[4:8]!=b'ftyp':raise RequestError(415,'invalid_m4a_container')
    try:
        out=subprocess.run([(ffprobe_path or ('/opt/homebrew/bin/ffprobe' if os.name != 'nt' else 'ffprobe')),'-v','error','-protocol_whitelist','file,pipe','-show_entries','format=duration:stream=codec_type,sample_rate,channels','-of','json',str(path)],capture_output=True,text=True,timeout=20)
        if out.returncode:raise ValueError()
        info=json.loads(out.stdout);audio=[s for s in info.get('streams',[]) if s.get('codec_type')=='audio']
        duration=float(info['format']['duration'])
        if len(audio)!=1 or int(audio[0]['sample_rate'])!=16000 or audio[0]['channels']!=1:raise ValueError()
        if not math.isfinite(duration) or duration<=0 or abs(duration-meta['duration_s'])>max(.3,meta['duration_s']*.01):raise ValueError()
    except (ValueError,KeyError,TypeError,subprocess.TimeoutExpired):raise RequestError(415,'audio_metadata_mismatch')

def streaming_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

class Store:
    def __init__(self,root,max_local_bytes=2*1024**3,minimum_free_bytes=2*1024**3):
        self.root=root;self.lock=threading.Lock();self.reserved=0;self.max_local_bytes=max_local_bytes;self.minimum_free_bytes=minimum_free_bytes;self.gc_enabled=False;self.connection_slots=threading.BoundedSemaphore(8)
        for name in ['private','inbox','objects','receipts','tmp','sessions']:
            (root/name).mkdir(parents=True,exist_ok=True,mode=0o700)
        self.dbpath=root/'private/events.sqlite3'
        con=self.connect();con.execute('CREATE TABLE IF NOT EXISTS events (chunk_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL, sha TEXT NOT NULL, metadata TEXT NOT NULL, receipt TEXT NOT NULL, UNIQUE(session_id,sequence))');
        if 'processing_status' not in {row[1] for row in con.execute('PRAGMA table_info(events)')}:con.execute("ALTER TABLE events ADD COLUMN processing_status TEXT NOT NULL DEFAULT 'pending'")
        con.commit();con.close();os.chmod(self.dbpath,0o600)
    def connect(self):
        con=sqlite3.connect(self.dbpath,timeout=30);con.execute('PRAGMA synchronous=FULL');return con
    def reserve(self,length):
        with self.lock:
            used=sum(path.stat().st_size for name in ['objects','inbox','tmp'] for path in (self.root/name).iterdir() if path.is_file())
            amount=length*3
            if used+self.reserved+amount>self.max_local_bytes or shutil.disk_usage(self.root).free<self.minimum_free_bytes+self.reserved+amount:
                raise RequestError(507,'storage_backpressure')
            self.reserved+=amount
            return amount
    def release(self,amount):
        with self.lock:self.reserved-=amount
    def check_space(self):
        if shutil.disk_usage(self.root).free<self.minimum_free_bytes:
            raise RequestError(507,'storage_backpressure')
    def processing(self,sha,capture_date):
        path=self.root.parent/'receipts'/capture_date/sha/'client-receipt.json'
        state={'state':'pending','canonical_source_id':sha,'canonical_capture_date':capture_date}
        if path.is_file():
            try:
                job=json.loads(path.read_text(encoding='utf-8'))
                if job.get('source_id')!=sha:raise ValueError()
                status=job.get('status')
                state['state']={'done':'transcribed','no_speech':'no_speech','failed':'failed'}.get(status,'pending')
                state['client_receipt']=str(path.relative_to(self.root.parent))
                if status=='done':state['document_id']=job.get('lark_doc_id')
            except (OSError,ValueError):pass
        return state
    def ensure_queue(self,target,meta):
        sha=meta['sha256'];extension=Path(meta['original_name']).suffix
        inbox=self.root/'inbox'/(sha+extension);ready=Path(str(inbox)+'.ready')
        ready_meta={'capture_date':meta['capture_date'],'capture_date_basis':'watch_app_capture_utc','original_name':meta['original_name'],'is_test':meta['is_test'],'source_url':None,'source_adapter':'watch_memory_app','app_metadata':meta}
        if ready.exists() and json.loads(ready.read_text(encoding='utf-8'))!=ready_meta:raise RequestError(409,'queued_metadata_conflict')
        if inbox.exists() and streaming_hash(inbox)!=sha:raise RequestError(409,'queued_audio_hash_conflict')
        if not ready.exists():atomic_json(ready,ready_meta)
        if not inbox.exists():
            staged=self.root/'tmp'/(uuid.uuid4().hex+extension+'.part')
            try:
                with target.open('rb') as src,staged.open('xb') as dst:
                    os.chmod(staged,0o600);shutil.copyfileobj(src,dst,1024*1024);dst.flush();os.fsync(dst.fileno())
                replace(staged,inbox)
                sync_directory(inbox.parent)
            finally:staged.unlink(missing_ok=True)
    def commit(self,temp,meta):
        sha=meta['sha256'];encoded=canonical(meta)
        with self.lock:
            con=self.connect()
            try:
                con.execute('BEGIN IMMEDIATE')
                existing=con.execute('SELECT metadata,receipt FROM events WHERE chunk_id=? OR (session_id=? AND sequence=?)',(meta['chunk_id'],meta['session_id'],meta['sequence'])).fetchall()
                if existing and (len(existing)!=1 or existing[0][0]!=encoded):raise RequestError(409,'chunk_identity_conflict')
                extension=Path(meta['original_name']).suffix;target=self.root/'objects'/(sha+extension)
                first=con.execute('SELECT metadata FROM events WHERE sha=? ORDER BY rowid LIMIT 1',(sha,)).fetchone()
                canonical_meta=json.loads(first[0]) if first else meta
                canonical_target=self.root/'objects'/(sha+Path(canonical_meta['original_name']).suffix)
                custody_path=self.root/'private/custody'/(sha+'.json')
                custody=json.loads(custody_path.read_text(encoding='utf-8')) if custody_path.exists() else None
                remote_only=False
                if not canonical_target.exists() and custody and custody.get('state')=='remote_verified' and self.processing(sha,canonical_meta['capture_date'])['state'] in ['transcribed','no_speech']:
                    try:
                        fresh=verify_archive(sha,Path(canonical_meta['original_name']).suffix,canonical_meta['byte_count'])
                        fresh.update(canonical_capture_date=canonical_meta['capture_date'],local_copies_reclaimed=True)
                        atomic_json(custody_path,fresh);remote_only=True
                    except CustodyError:
                        # The incoming full body has just passed hash/media checks; restoring
                        # and fsyncing it re-establishes local custody before any ACK.
                        remote_only=False
                if not remote_only:
                    if target.exists():
                        if target.is_symlink() or not target.is_file() or streaming_hash(target)!=sha:raise RequestError(409,'stored_object_hash_conflict')
                    else:
                        os.chmod(temp,0o600);replace(temp,target)
                        sync_directory(target.parent)
                    if not canonical_target.exists() or canonical_target.is_symlink() or streaming_hash(canonical_target)!=sha:raise RequestError(409,'canonical_custody_not_verified')
                    self.ensure_queue(canonical_target,canonical_meta)
                    if custody:
                        atomic_json(custody_path,{**custody,'state':'local_restored','local_restored_at':dt.datetime.now(dt.timezone.utc).isoformat(),'local_copies_reclaimed':False})
                if existing:
                    result=json.loads(existing[0][1]);result['status']='already_accepted'
                else:
                    result={'stored':True,'sha256':sha,'source_id':sha,'status':'accepted','session_id':meta['session_id'],'sequence':meta['sequence'],'chunk_id':meta['chunk_id'],'capture_date':meta['capture_date'],'transcription_status':'queued' if not first else 'shared_audio_object','stored_at':dt.datetime.now(dt.timezone.utc).isoformat()}
                processing=self.processing(sha,canonical_meta['capture_date'])
                atomic_json(self.root/'sessions'/meta['session_id']/(str(meta['sequence'])+'.json'),{'metadata':meta,'receipt':result,'processing':processing})
                if not existing:
                    con.execute('INSERT INTO events (chunk_id,session_id,sequence,sha,metadata,receipt,processing_status) VALUES (?,?,?,?,?,?,?)',(meta['chunk_id'],meta['session_id'],meta['sequence'],sha,encoded,canonical(result),processing['state']))
                else:con.execute('UPDATE events SET processing_status=? WHERE chunk_id=?',(processing['state'],meta['chunk_id']))
                con.commit();atomic_json(self.root/'receipts'/(meta['chunk_id']+'.json'),result)
                result['processing']=processing
                return (200 if existing else 201),result
            finally:con.close()
    def refresh_processing(self):
        with self.lock:
            con=self.connect()
            try:
                canonical_rows=con.execute('SELECT metadata FROM events WHERE rowid IN (SELECT min(rowid) FROM events GROUP BY sha)').fetchall()
                recovery_errors=[]
                for (encoded,) in canonical_rows:
                    try:
                        meta=json.loads(encoded);sha=meta['sha256'];extension=Path(meta['original_name']).suffix
                        inbox=self.root/'inbox'/(sha+extension);target=self.root/'objects'/(sha+extension)
                        journal=self.root/'private/custody'/(sha+'.json')
                        custody=json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else None
                        remote_only=custody and custody.get('state')=='remote_verified' and not target.exists()
                        if not remote_only and (not inbox.exists() or not Path(str(inbox)+'.ready').exists()):
                            if target.exists() and not target.is_symlink() and streaming_hash(target)==sha:self.ensure_queue(target,meta)
                    except (RequestError,OSError,ValueError) as exc:
                        recovery_errors.append({'source_id':meta.get('sha256'),'error_code':exc.code if isinstance(exc,RequestError) else 'canonical_recovery_deferred'})
                if recovery_errors:atomic_json(self.root/'receipts/reconcile-errors.json',{'errors':recovery_errors})
                rows=con.execute("SELECT metadata,receipt FROM events WHERE processing_status NOT IN ('transcribed','no_speech')").fetchall()
                for encoded,receipt_json in rows:
                    meta=json.loads(encoded);sha=meta['sha256']
                    first=con.execute('SELECT metadata FROM events WHERE sha=? ORDER BY rowid LIMIT 1',(sha,)).fetchone()
                    canonical_meta=json.loads(first[0]);processing=self.processing(sha,canonical_meta['capture_date'])
                    atomic_json(self.root/'sessions'/meta['session_id']/(str(meta['sequence'])+'.json'),{'metadata':meta,'receipt':json.loads(receipt_json),'processing':processing})
                    con.execute('UPDATE events SET processing_status=? WHERE chunk_id=?',(processing['state'],meta['chunk_id']))
                con.commit()
            finally:con.close()

    def maybe_gc(self):
        if not self.gc_enabled:return
        request=self.root/'private/gc-request.json';schedule=self.root/'private/gc-schedule.json'
        previous=json.loads(schedule.read_text(encoding='utf-8')) if schedule.exists() else {}
        used=sum(path.stat().st_size for name in ['objects','inbox','tmp'] for path in (self.root/name).iterdir() if path.is_file())
        if not request.exists() and time.time()-previous.get('last_successful_pass',0)<86400 and used<self.max_local_bytes*.7:return
        report=collect_garbage(self)
        if report['collected']:atomic_json(schedule,{'last_successful_pass':time.time(),'collected':len(report['collected'])})
        if request.exists():request.unlink()

class Server(ThreadingHTTPServer):
    daemon_threads=True
    request_queue_size=8
    def __init__(self,address,store,token,allow_legacy_fixture=False,ffprobe_path=None):
        self.ffprobe_path=ffprobe_path
        self.store,self.token=store,token;self.pairing_secret=token;self.allow_legacy_fixture=allow_legacy_fixture;self.connection_slots=store.connection_slots
        super().__init__(address,Handler)
    def server_bind(self):
        socketserver.TCPServer.server_bind(self);self.server_name=self.server_address[0];self.server_port=self.server_address[1]
    def process_request(self,request,client_address):
        if not self.connection_slots.acquire(blocking=False):
            with contextlib.suppress(OSError):request.sendall(b'HTTP/1.1 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:
            self.connection_slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.connection_slots.release()

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='WatchMemoryReceiver/1'
    def setup(self):
        super().setup();self.connection.settimeout(30)
        self.deadline=threading.Timer(120,self.expire);self.deadline.daemon=True;self.deadline.start()
    def expire(self):
        with contextlib.suppress(OSError):self.connection.shutdown(socket.SHUT_RDWR)
    def finish(self):
        self.deadline.cancel()
        super().finish()
    def log_message(self,fmt,*args):
        pass  # Never log auth/headers/audio contents or pairing credentials.
    def reply(self,status,data):
        context=getattr(self,'v2_context',None)
        if context:data={**data,'schema_version':2,'protocol':wire_v2.PROTOCOL}
        payload=json.dumps(data,separators=(',',':')).encode()
        self.close_connection=True
        with contextlib.suppress(BrokenPipeError,ConnectionResetError,OSError):
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.send_header('Connection','close')
            if context:self.send_header('X-Watch-Receipt-MAC',wire_v2.receipt_mac(self.server.pairing_secret,context[0],context[1],status,payload))
            self.end_headers();self.wfile.write(payload)
    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization','').encode('utf-8'),('Bearer '+self.server.token).encode('utf-8'))
    def do_GET(self):
        if self.server.allow_legacy_fixture and not self.authorized():self.reply(401,{'error':'unauthorized'});return
        if self.path=='/health':self.reply(200,{'ok':True,'protocol':'watch-memory-chunks-v1' if self.server.allow_legacy_fixture else wire_v2.PROTOCOL,'ack_semantics':'durably_stored_not_transcribed'})
        else:self.reply(404,{'error':'not_found'})
    def do_PUT(self):
        if self.path.startswith('/v2/'):
            self.put_v2();return
        if not self.server.allow_legacy_fixture or not ipaddress.ip_address(self.client_address[0]).is_loopback or not ipaddress.ip_address(self.server.server_address[0]).is_loopback:
            self.reply(426,{'stored':False,'error':'sealed_v2_required'});return
        self.put_legacy_fixture()
    def put_legacy_fixture(self):
        temp=None;reservation=0
        try:
            if not self.authorized():raise RequestError(401,'unauthorized')
            match=re.fullmatch(r'/v1/chunks/([0-9a-f]{64})',self.path)
            if not match:raise RequestError(404,'invalid_chunk_path')
            if self.headers.get('Transfer-Encoding'):raise RequestError(411,'content_length_required')
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)!=1 or not lengths[0].isdigit():raise RequestError(411,'content_length_required')
            length=int(lengths[0])
            if not 0<length<=MAX_BYTES:raise RequestError(413,'chunk_too_large')
            mime=self.headers.get('Content-Type','').split(';')[0].strip().lower()
            if mime not in ['audio/x-caf','audio/mp4']:raise RequestError(415,'audio_format_required')
            sha=match.group(1);meta=metadata(self.headers.get('X-Watch-Metadata'),sha,length)
            if meta['mime']!=mime:raise RequestError(415,'content_type_mismatch')
            reservation=self.server.store.reserve(length)
            temp=self.server.store.root/'tmp'/(uuid.uuid4().hex+'.upload.part');digest=hashlib.sha256();remaining=length
            with temp.open('xb') as f:
                os.chmod(temp,0o600)
                while remaining:
                    self.server.store.check_space()
                    chunk=self.rfile.read(min(1024*1024,remaining))
                    if not chunk:raise RequestError(400,'incomplete_body')
                    f.write(chunk);digest.update(chunk);remaining-=len(chunk)
                f.flush();os.fsync(f.fileno())
            if not hmac.compare_digest(digest.hexdigest(),sha):raise RequestError(422,'sha256_mismatch')
            media_check(temp,meta,self.server.ffprobe_path)
            status,result=self.server.store.commit(temp,meta);self.reply(status,result)
        except RequestError as exc:self.reply(exc.status,{'stored':False,'error':exc.code})
        except (socket.timeout,TimeoutError):self.reply(408,{'stored':False,'error':'upload_timeout'})
        except (OSError,sqlite3.Error,ValueError):self.reply(503,{'stored':False,'error':'temporary_storage_error'})
        finally:
            if temp is not None:temp.unlink(missing_ok=True)
            if reservation:self.server.store.release(reservation)

    def put_v2(self):
        wire_path=None;audio_path=None;reservation=0
        try:
            prefix='/v2/chunks/'
            chunk_id=self.path[len(prefix):] if self.path.startswith(prefix) else ''
            challenge=self.headers.get('X-Watch-Challenge','')
            if not wire_v2.canonical_uuid(chunk_id) or not wire_v2.canonical_uuid(challenge):raise RequestError(400,'invalid_v2_route_or_challenge')
            if self.headers.get('Authorization') or self.headers.get('X-Watch-Metadata'):raise RequestError(400,'plaintext_credentials_not_allowed')
            if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/octet-stream':raise RequestError(415,'sealed_body_required')
            if self.headers.get('Transfer-Encoding'):raise RequestError(411,'content_length_required')
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)!=1 or not lengths[0].isdigit():raise RequestError(411,'content_length_required')
            length=int(lengths[0])
            if not 33<=length<=wire_v2.MAX_WIRE:raise RequestError(413,'wire_size_limit')
            reservation=self.server.store.reserve(length)
            wire_path=self.server.store.root/'tmp'/(uuid.uuid4().hex+'.sealed.part');remaining=length;digest=hashlib.sha256()
            with wire_path.open('xb') as f:
                os.chmod(wire_path,0o600)
                while remaining:
                    self.server.store.check_space();block=self.rfile.read(min(1024*1024,remaining))
                    if not block:raise RequestError(400,'incomplete_body')
                    f.write(block);digest.update(block);remaining-=len(block)
                f.flush();os.fsync(f.fileno())
            self.v2_context=(challenge,digest.hexdigest())
            decoded,audio=wire_v2.unseal(self.server.pairing_secret,wire_path.read_bytes(),self.path,challenge)
            if decoded.get('chunk_id')!=chunk_id:raise RequestError(400,'chunk_route_mismatch')
            sha=decoded.get('sha256');encoded=base64.b64encode(json.dumps(decoded,separators=(',',':'),ensure_ascii=False).encode()).decode()
            meta=metadata(encoded,sha,len(audio),90000)
            if not hmac.compare_digest(hashlib.sha256(audio).hexdigest(),sha):raise RequestError(422,'sha256_mismatch')
            audio_path=self.server.store.root/'tmp'/(uuid.uuid4().hex+'.audio.part')
            with audio_path.open('xb') as f:
                os.chmod(audio_path,0o600);f.write(audio);f.flush();os.fsync(f.fileno())
            del audio
            media_check(audio_path,meta,self.server.ffprobe_path)
            status,result=self.server.store.commit(audio_path,meta);self.reply(status,result)
        except wire_v2.WireError as exc:self.reply(401 if str(exc)=='invalid_seal' else 400,{'stored':False,'error':str(exc)})
        except RequestError as exc:self.reply(exc.status,{'stored':False,'error':exc.code})
        except (socket.timeout,TimeoutError):self.reply(408,{'stored':False,'error':'upload_timeout'})
        except (OSError,sqlite3.Error,ValueError):self.reply(503,{'stored':False,'error':'temporary_storage_error'})
        finally:
            if wire_path is not None:wire_path.unlink(missing_ok=True)
            if audio_path is not None:audio_path.unlink(missing_ok=True)
            if reservation:self.server.store.release(reservation)

def main():
    global ROOT,CAPTURE_TZ
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['serve']);parser.add_argument('--root',type=Path,default=None,help='receiver data directory (default: data/receiver)');args=parser.parse_args()
    os.umask(0o077)
    if args.root is not None:ROOT=args.root.resolve()
    config_path=ROOT/'private/config.json'
    if not config_path.is_file():raise SystemExit(f'Missing {config_path}. Run: python3 scripts/setup.py')
    config=json.loads(config_path.read_text(encoding='utf-8'))
    if config.get('timezone'):CAPTURE_TZ=ZoneInfo(config['timezone'])
    if config.get('gc_enabled',False):raise RuntimeError('This receiver retains originals; gc_enabled must be false')
    if config.get('protocol_version')!=2:raise RuntimeError('Production requires sealed v2 config')
    wire_v2.derive(config['pairing_secret'])
    supplied_ffprobe=config.get('ffprobe_path')
    if supplied_ffprobe and (not isinstance(supplied_ffprobe,str) or not Path(supplied_ffprobe).is_absolute()):raise RuntimeError('Configured ffprobe_path must be absolute')
    default_ffprobe='/opt/homebrew/bin/ffprobe' if os.name!='nt' and Path('/opt/homebrew/bin/ffprobe').is_file() else shutil.which('ffprobe')
    ffprobe=supplied_ffprobe or default_ffprobe
    if not ffprobe or not Path(ffprobe).is_file():raise RuntimeError('Configure an existing absolute ffprobe_path for this receiver account')
    ffprobe=str(Path(ffprobe).resolve())
    probe=subprocess.run([ffprobe,'-version'],capture_output=True,timeout=10)
    if probe.returncode:raise RuntimeError('Configured ffprobe startup check failed')
    if any(not private_address(ip) for ip in config['bind_addresses']):raise RuntimeError('Refusing non-private bind')
    with (ROOT/'private/receiver.lock').open('a+') as lock:
        lock_exclusive(lock)
        store=Store(ROOT,config.get('max_local_bytes',2*1024**3),config.get('minimum_free_bytes',2*1024**3));store.gc_enabled=config.get('gc_enabled',False);servers=[]
        for address in config['bind_addresses']:
            try:
                server=Server((address,config['port']),store,config['pairing_secret'],ffprobe_path=ffprobe);servers.append(server);threading.Thread(target=server.serve_forever,daemon=True).start()
                print(json.dumps({'event':'listening','address':address,'port':config['port']}),flush=True)
            except OSError:print(json.dumps({'event':'private_interface_unavailable','address':address}),flush=True)
        if not servers:raise RuntimeError('No configured private interface is currently bindable')
        try:
            while True:
                time.sleep(30)
                try:store.refresh_processing();store.maybe_gc()
                except (OSError,sqlite3.Error,ValueError,RequestError):print(json.dumps({'event':'reconcile_deferred'}),flush=True)
        finally:
            for server in servers:server.shutdown();server.server_close()

if __name__=='__main__':main()
