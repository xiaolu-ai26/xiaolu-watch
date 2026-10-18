"""Loopback-only independent Phone API. Public access is via the bounded ingress."""
import argparse
import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit,parse_qs
from context_store import ContextStore,StoreError,canonical
from deletion import Deletions,bounded_json,CARD

MAX_BODY=256*1024
MAX_REPLY=2*1024*1024

def public_today(value,day,retired):
    """Positive field allowlist: raw context and private source maps cannot escape."""
    if value.get('version')!=3 or value.get('date')!=day:raise StoreError('projection_unavailable',503)
    result={k:value[k] for k in ('version','date','timezone','generated_at','revision')}
    result['overview']={k:value['overview'][k] for k in ('state','text')}
    result['body']={k:value['body'][k] for k in ('state','lines','sample_count')}
    result['coverage']=[{k:r[k] for k in ('start_at','end_at','state')} for r in value['coverage']]
    result['cards']=[{k:r[k] for k in ('id','version','title','summary','start_at','end_at','owner_label','clean','distilled')} for r in value['cards'] if r['id'] not in retired]
    def optional_time(source, target, field):
        if field not in source:
            return
        value=source[field]
        if value is not None:
            try:
                if not isinstance(value,str) or len(value)>64 or dt.datetime.fromisoformat(value.replace('Z','+00:00')).tzinfo is None:
                    raise ValueError()
            except (ValueError,TypeError):raise StoreError('projection_unavailable',503)
        target[field]=value
    for field in ('coverage_start_at','coverage_end_at','updated_at'):
        optional_time(value['overview'],result['overview'],field)
    for field in ('covered_card_count','pending_card_count'):
        if field in value['overview']:
            number=value['overview'][field]
            if type(number) is not int or not 0<=number<=100000:raise StoreError('projection_unavailable',503)
            result['overview'][field]=number
    if 'is_partial' in value['overview']:
        if type(value['overview']['is_partial']) is not bool:raise StoreError('projection_unavailable',503)
        result['overview']['is_partial']=value['overview']['is_partial']
    optional_time(value['body'],result['body'],'measured_through_at')
    if 'ingestion' in value:
        ingestion=value['ingestion']
        if not isinstance(ingestion,dict) or ingestion.get('state') not in ('empty','processing','budget_wait','up_to_date'):raise StoreError('projection_unavailable',503)
        target={'state':ingestion['state']}
        for field in ('latest_recorded_at','latest_transcribed_at','latest_organized_at'):optional_time(ingestion,target,field)
        for field in ('received_count','transcribed_count','queued_text_count','queued_asr_count'):
            number=ingestion.get(field)
            if type(number) is not int or not 0<=number<=100000:raise StoreError('projection_unavailable',503)
            target[field]=number
        result['ingestion']=target

    originals={card['id']:card for card in value['cards']}
    for card in result['cards']:
        original=originals[card['id']]
        for field,allowed in (('reading_role',('main','background','unknown')),('summary_state',('ready','pending','background','failed'))):
            if field in original:
                if original[field] not in allowed:raise StoreError('projection_unavailable',503)
                card[field]=original[field]
    # Cached summaries may still mention a deleted card. Hide them until processor
    # publishes a generation-bound projection, never resurrect an old cache read.
    if retired and value.get('retirement_generation',-1)<retired.generation:
        result['overview']={'state':'pending','text':'','is_partial':True,'covered_card_count':0,'pending_card_count':len(result['cards']),'coverage_start_at':None,'coverage_end_at':None,'updated_at':None}
        for card in result['cards']:card['summary']=''
    return result

class Retired(set):
    def __init__(self,snapshot):super().__init__(snapshot['group_ids']);self.generation=snapshot['generation']

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store,reading_root,endpoint,timezone='UTC'):
        if address[0]!='127.0.0.1':raise ValueError('Phone API must remain loopback-only')
        origin=urlsplit(endpoint)
        if origin.scheme!='https' or not origin.hostname or origin.username or origin.password or origin.path not in ('','/') or origin.query or origin.fragment:raise ValueError('Expected HTTPS origin')
        self.store=store;self.deletions=Deletions(store,reading_root);self.reading_root=Path(reading_root);self.endpoint=endpoint.rstrip('/');self.timezone=timezone
        self.slots=threading.BoundedSemaphore(8);self.rate_lock=threading.Lock();self.rates={}
        super().__init__(address,Handler)
    def allow(self,principal):
        with self.rate_lock:
            minute=int(time.time()//60);key=(principal,minute)
            self.rates={k:v for k,v in self.rates.items() if k[1]>=minute}
            n=self.rates.get(key,0)
            if n>=120:return False
            self.rates[key]=n+1;return True
    def process_request(self,request,address):
        if not self.slots.acquire(False):self.shutdown_request(request);return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1';server_version='';sys_version=''
    def log_message(self,*args):pass
    def setup(self):super().setup();self.connection.settimeout(15)
    def reply(self,status,data):
        raw=canonical(data).encode('utf-8')
        if len(raw)>MAX_REPLY:status=503;raw=b'{"version":3,"error":"projection_too_large"}'
        self.close_connection=True
        with contextlib.suppress(OSError):
            self.send_response_only(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('Pragma','no-cache');self.send_header('Connection','close');self.end_headers();self.wfile.write(raw)
    def body(self,limit=MAX_BODY):
        lengths=self.headers.get_all('Content-Length',[])
        if self.headers.get('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isdigit():raise StoreError('length_required',411)
        size=int(lengths[0])
        if not 1<=size<=limit:raise StoreError('body_too_large',413)
        if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':raise StoreError('json_required',415)
        raw=self.rfile.read(size)
        if len(raw)!=size:raise StoreError('incomplete_body')
        def unique(pairs):
            value={}
            for k,v in pairs:
                if k in value:raise ValueError('duplicate')
                value[k]=v
            return value
        try:return json.loads(raw.decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))
        except (ValueError,UnicodeError,RecursionError):raise StoreError('invalid_json')
    def principal(self,scope):
        headers=self.headers.get_all('Authorization',[])
        if len(headers)!=1 or not headers[0].startswith('Bearer '):raise StoreError('unauthorized',401)
        principal=self.server.store.authorize(headers[0][7:],scope)
        if not self.server.allow(principal):raise StoreError('rate_limited',429)
        return principal
    def dispatch(self,method):
        path=urlsplit(self.path)
        if path.fragment or path.scheme or path.netloc:raise StoreError('not_found',404)
        if method=='POST' and self.path=='/v3/phone/pair/claim':
            status,value=self.server.store.claim(self.body(1024),self.client_address[0],self.server.endpoint);return status,value
        if method=='POST' and self.path=='/v3/phone/context':
            principal=self.principal('context:write');return 200,self.server.store.ingest(principal,self.body())
        if method=='GET' and path.path=='/v3/phone/today':
            self.principal('today:read');query=parse_qs(path.query,keep_blank_values=True)
            if set(query)!={'date'} or len(query['date'])!=1 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',query['date'][0]):raise StoreError('invalid_date')
            day=query['date'][0]
            try:dt.date.fromisoformat(day)
            except ValueError:raise StoreError('invalid_date')
            with self.server.store.generation_guard() as snap:
                filename=self.server.reading_root/(day+'.json')
                if not filename.exists():return 200,{'version':3,'date':day,'timezone':self.server.timezone,'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'revision':0,'overview':{'state':'pending','text':''},'body':{'state':'unknown','lines':[],'sample_count':0},'coverage':[],'cards':[]}
                return 200,public_today(bounded_json(filename),day,Retired(snap))
        match=re.fullmatch(r'/v3/phone/cards/([A-Za-z0-9_-]{1,128})/deletion-plan',self.path)
        if method=='POST' and match:
            principal=self.principal('cards:delete');return 200,self.server.deletions.plan(principal,match[1],self.body(2048))
        match=re.fullmatch(r'/v3/phone/deletions/([0-9a-f-]{36})/commit',self.path)
        if method=='POST' and match:
            principal=self.principal('cards:delete');return 202,self.server.deletions.commit(principal,match[1],self.body(2048))
        match=re.fullmatch(r'/v3/phone/deletions/([0-9a-f-]{36})',self.path)
        if method=='GET' and match:return 200,self.server.deletions.status(self.principal('cards:delete'),match[1])
        raise StoreError('not_found',404)
    def handle_method(self,method):
        try:status,value=self.dispatch(method);self.reply(status,value)
        except StoreError as e:self.reply(e.status,{'version':3,'error':e.code})
        except (OSError,sqlite3.Error):self.reply(503,{'version':3,'error':'temporarily_unavailable'})
        except (ValueError,KeyError,TypeError,OverflowError):self.reply(503,{'version':3,'error':'projection_unavailable'})
    def do_GET(self):self.handle_method('GET')
    def do_POST(self):self.handle_method('POST')
    def do_PUT(self):self.reply(405,{'version':3,'error':'method_not_allowed'})
    def do_DELETE(self):self.reply(405,{'version':3,'error':'method_not_allowed'})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);args=parser.parse_args()
    config=json.loads(args.config.read_text(encoding='utf-8'))
    store=ContextStore(config['context_root'])
    from deletion_audio import AudioRetirement
    audio=AudioRetirement(store,config)
    def work():
        while True:
            try:
                with store.connect() as c:ids=[r[0] for r in c.execute("SELECT id FROM deletion_transactions WHERE state!='completed'")]
                for txid in ids:audio.advance(txid)
            except (OSError,ValueError,sqlite3.Error):pass  # Durable transactions remain retryable; no private payload logging.
            time.sleep(10)
    threading.Thread(target=work,daemon=True).start()
    Server(('127.0.0.1',config.get('port',18439)),store,config['reading_root'],config['endpoint'],config.get('timezone','UTC')).serve_forever()
if __name__=='__main__':main()
