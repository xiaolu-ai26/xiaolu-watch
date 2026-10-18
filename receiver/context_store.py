"""Private Phone context, independent credentials and deletion generation ledger.

The configured root must be on a protected local filesystem. No public raw-data read API.
"""
import base64,contextlib,datetime as dt,hashlib,hmac,json,math,os,re,secrets,sqlite3,time,uuid,threading
from pathlib import Path
from zoneinfo import ZoneInfo
from platform_store import lock_exclusive, replace, sync_directory

_GUARD_LOCAL=threading.local()
SCOPES=('context:write','today:read','cards:delete')
STAGES=('transcript','processed_text','notes','cloud','summary','projection')
STATUS=('pending','done','retryable_failed','shared_audio_pending')
class StoreError(ValueError):
    def __init__(self,code,status=400):self.code=code;self.status=status;super().__init__(code)
def canonical(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True,allow_nan=False)
def digest(value):return hashlib.sha256(value if isinstance(value,bytes) else canonical(value).encode()).hexdigest()
def identifier(value):
    try:return isinstance(value,str) and str(uuid.UUID(value))==value
    except ValueError:return False
def instant(value):
    try:
        parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError()
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError,TypeError,AttributeError):raise StoreError('invalid_timestamp')
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with tmp.open('x',encoding='utf-8') as f:os.chmod(tmp,0o600);f.write(canonical(value));f.flush();os.fsync(f.fileno())
        replace(tmp,path);sync_directory(path.parent)
    finally:tmp.unlink(missing_ok=True)

class ContextStore:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700);self.dbpath=self.root/'context.sqlite3'
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v INTEGER NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('generation',0),('context_revision',0);
            CREATE TABLE IF NOT EXISTS principals(id TEXT PRIMARY KEY,token_hash TEXT UNIQUE,role TEXT,scopes TEXT,revoked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS pair_codes(hash TEXT PRIMARY KEY,principal TEXT,expires REAL,attempt TEXT,claims INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS rates(name TEXT PRIMARY KEY,minute INTEGER,n INTEGER);
            CREATE TABLE IF NOT EXISTS requests(principal TEXT,purpose TEXT,id TEXT,body_hash TEXT,response TEXT,PRIMARY KEY(principal,purpose,id));
            CREATE TABLE IF NOT EXISTS controls(principal TEXT PRIMARY KEY,revision INTEGER,observed TEXT,flags TEXT,state TEXT);
            CREATE TABLE IF NOT EXISTS health(id TEXT PRIMARY KEY,principal TEXT,revision INTEGER,data TEXT,deleted INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS places(id TEXT PRIMARY KEY,principal TEXT,observed TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS card_tombstones(group_id TEXT PRIMARY KEY,generation INTEGER,card_version INTEGER,transaction_id TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS source_tombstones(source_id TEXT PRIMARY KEY,transaction_id TEXT,state TEXT,mapping TEXT,revision INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS deletion_plans(id TEXT PRIMARY KEY,principal TEXT,body TEXT,expires REAL,state TEXT);
            CREATE TABLE IF NOT EXISTS deletion_transactions(id TEXT PRIMARY KEY,group_id TEXT,generation INTEGER,state TEXT,body TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS derivative_stages(transaction_id TEXT,stage TEXT,status TEXT,detail TEXT,PRIMARY KEY(transaction_id,stage));
            CREATE TABLE IF NOT EXISTS stage_events(id INTEGER PRIMARY KEY AUTOINCREMENT,transaction_id TEXT,stage TEXT,status TEXT,detail TEXT,at TEXT);
            ''')
        key=self.root/'phone-master.key'
        if not key.exists():
            try:
                fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as f:f.write(secrets.token_bytes(32));f.flush();os.fsync(f.fileno())
            except FileExistsError:pass
        self.master=key.read_bytes()
        if len(self.master)!=32:raise RuntimeError('Invalid private Phone master')
    @contextlib.contextmanager
    def connect(self):
        c=sqlite3.connect(self.dbpath,timeout=15)
        try:
            c.execute('PRAGMA synchronous=FULL');c.execute('PRAGMA journal_mode=WAL')
            with c:yield c
        finally:c.close()
    def token(self,principal):
        raw=hmac.new(self.master,b'watch-memory-phone-principal-v1\x00'+principal.encode(),hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    def issue_pair(self,ttl=600):
        if not 1<=ttl<=600:raise ValueError('Invalid pairing TTL')
        principal=str(uuid.uuid4());code=base64.b32encode(secrets.token_bytes(10)).decode();token=self.token(principal)
        with self.connect() as c:
            c.execute('INSERT INTO principals VALUES(?,?,?,?,0)',(principal,digest(token.encode()),'phone',canonical(SCOPES)))
            c.execute('INSERT INTO pair_codes VALUES(?,?,?,NULL,0)',(digest(code.encode()),principal,time.time()+ttl))
        return {'code':code,'principal_id':principal,'expires_at':dt.datetime.fromtimestamp(time.time()+ttl,dt.timezone.utc).isoformat()}
    def claim(self,value,peer,endpoint):
        denied={'version':3,'error':'pairing_denied','message':'Pairing could not be verified'}
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');minute=int(time.time()//60)
            for key,limit in [('global',10),('peer:'+peer,5)]:
                row=c.execute('SELECT minute,n FROM rates WHERE name=?',(key,)).fetchone();n=row[1] if row and row[0]==minute else 0
                if n>=limit:c.commit();return 429,{'version':3,'error':'rate_limited'}
                c.execute('INSERT OR REPLACE INTO rates VALUES(?,?,?)',(key,minute,n+1))
            if not isinstance(value,dict) or set(value)!={'code','attempt_id'} or not re.fullmatch('[A-Z2-7]{16}',str(value.get('code',''))) or not identifier(value.get('attempt_id')):c.commit();return 401,denied
            row=c.execute('SELECT principal,expires,attempt,claims FROM pair_codes WHERE hash=?',(digest(value['code'].encode()),)).fetchone()
            if not row or row[1]<=time.time() or (row[2] and row[2]!=value['attempt_id']) or row[3]>=8:c.commit();return 401,denied
            c.execute('UPDATE pair_codes SET attempt=?,claims=claims+1 WHERE hash=?',(value['attempt_id'],digest(value['code'].encode())));c.commit()
            return 200,{'version':3,'protocol':'watch-memory-phone-v3','endpoint':endpoint,'token':self.token(row[0]),'principal_id':row[0],'scopes':list(SCOPES)}
    def authorize(self,token,scope):
        if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_-]{43}',token):raise StoreError('unauthorized',401)
        token_hash=digest(token.encode())
        with self.connect() as c:row=c.execute('SELECT id,token_hash,role,scopes,revoked FROM principals WHERE token_hash=?',(token_hash,)).fetchone()
        if not row or not hmac.compare_digest(row[1],token_hash) or row[2]!='phone' or row[4]:raise StoreError('unauthorized',401)
        if scope not in json.loads(row[3]):raise StoreError('forbidden',403)
        return row[0]
    def idempotent(self,c,principal,purpose,request_id,body):
        if not identifier(request_id):raise StoreError('invalid_request_id')
        row=c.execute('SELECT body_hash,response FROM requests WHERE principal=? AND purpose=? AND id=?',(principal,purpose,request_id)).fetchone()
        if row:
            if row[0]!=digest(body):raise StoreError('idempotency_conflict',409)
            return json.loads(row[1])
        return None
    def save_request(self,c,principal,purpose,request_id,body,response):
        c.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(principal,purpose,request_id,digest(body),canonical(response)))
    def ingest(self,principal,payload):
        from context_validation import validate_context
        value=validate_context(payload)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');prior=self.idempotent(c,principal,'context',value['request_id'],value)
            if prior:return prior
            flags=value['function_flags'];rev=flags['control_revision'];control=c.execute('SELECT revision,observed,flags,state FROM controls WHERE principal=?',(principal,)).fetchone()
            if control and rev==control[0] and canonical(flags)!=control[2]:raise StoreError('control_revision_conflict',409)
            if not control or rev>control[0] or (rev==control[0] and value['observed_at']>control[1]):
                state=json.loads(control[3]) if control else {'health':'not_queried','location':'not_queried'}
                state.update({k:v for k,v in value['collection_state'].items() if v!='not_queried'})
                c.execute('INSERT OR REPLACE INTO controls VALUES(?,?,?,?,?)',(principal,rev,value['observed_at'],canonical(flags),canonical(state)))
                active=flags
            else:active=json.loads(control[2])
            for sample_id in value['health_deleted']:
                owner=c.execute('SELECT principal FROM health WHERE id=?',(sample_id,)).fetchone()
                if owner and owner[0]!=principal:raise StoreError('sample_owner_conflict',409)
                c.execute("INSERT INTO health VALUES(?,?,0,'{}',1) ON CONFLICT(id) DO UPDATE SET data='{}',deleted=1",(sample_id,principal))
            ignored=0
            for sample in value['health']:
                owner=c.execute('SELECT principal FROM health WHERE id=?',(sample['id'],)).fetchone()
                if owner and owner[0]!=principal:raise StoreError('sample_owner_conflict',409)
                if not active['health_enabled']:ignored+=1;continue
                row=c.execute('SELECT revision,data,deleted FROM health WHERE id=?',(sample['id'],)).fetchone()
                if row and row[2]:continue
                if row and sample['revision']<row[0]:continue
                if row and sample['revision']==row[0] and canonical(sample)!=row[1]:raise StoreError('health_revision_conflict',409)
                c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?,?,0)',(sample['id'],principal,sample['revision'],canonical(sample)))
            for place in value['places']:
                owner=c.execute('SELECT principal FROM places WHERE id=?',(place['id'],)).fetchone()
                if owner and owner[0]!=principal:raise StoreError('sample_owner_conflict',409)
                if not active['location_enabled']:ignored+=1;continue
                row=c.execute('SELECT observed FROM places WHERE id=?',(place['id'],)).fetchone()
                if not row or value['observed_at']>row[0]:c.execute('INSERT OR REPLACE INTO places VALUES(?,?,?,?)',(place['id'],principal,value['observed_at'],canonical(place)))
            c.execute("UPDATE meta SET v=v+1 WHERE k='context_revision'");revision=c.execute("SELECT v FROM meta WHERE k='context_revision'").fetchone()[0]
            response={'version':3,'request_id':value['request_id'],'accepted':True,'stored_at':now(),'context_revision':revision,'ignored_disabled_rows':ignored};self.save_request(c,principal,'context',value['request_id'],value,response);return response
    def export_context(self,day,history_days=28):
        dt.date.fromisoformat(day)
        with self.connect() as c:
            control=c.execute('SELECT flags,state FROM controls ORDER BY observed DESC LIMIT 1').fetchone();samples=[json.loads(x[0]) for x in c.execute('SELECT data FROM health WHERE deleted=0')];places=[json.loads(x[0]) for x in c.execute('SELECT data FROM places')];revision=c.execute("SELECT v FROM meta WHERE k='context_revision'").fetchone()[0]
        lower=(dt.date.fromisoformat(day)-dt.timedelta(days=history_days+1)).isoformat();upper=(dt.date.fromisoformat(day)+dt.timedelta(days=2)).isoformat()
        samples=[x for x in samples if x['end_at'][:10]>=lower and x['start_at'][:10]<upper]
        places=[x for x in places if x['end_at'][:10]>=(dt.date.fromisoformat(day)-dt.timedelta(days=1)).isoformat() and x['start_at'][:10]<upper]
        return {'context_revision':revision,'health':samples,'samples':samples,'places':places,'function_flags':json.loads(control[0]) if control else {'health_enabled':False,'location_enabled':False,'share_derived_with_ai':False,'control_revision':0},'collection_state':json.loads(control[1]) if control else {'health':'not_queried','location':'not_queried'}}
    def retirement_snapshot(self):
        with self.connect() as c:
            generation=c.execute("SELECT v FROM meta WHERE k='generation'").fetchone()[0];groups=[x[0] for x in c.execute('SELECT group_id FROM card_tombstones')];sources=[x[0] for x in c.execute('SELECT source_id FROM source_tombstones')];transactions=[json.loads(x[0]) for x in c.execute('SELECT body FROM deletion_transactions')]
        return {'generation':generation,'group_ids':groups,'retired_group_ids':groups,'retired_raw_source_ids':sources,'source_ids':[],'transactions':transactions,'raw_retirement_does_not_retire_other_groups':True}
    @contextlib.contextmanager
    def generation_guard(self,expected=None,timeout=15):
        # ASR/pipeline layers share this lock. Re-enter only on this thread and
        # this same canonical root; other threads/processes still take the OS lock.
        key=os.path.normcase(str(self.root.resolve()))
        active=getattr(_GUARD_LOCAL,'active',None)
        if active is not None:
            if active[0]!=key:raise StoreError('nested_different_context_root',409)
            if expected is not None and active[1]['generation']!=expected:raise StoreError('retirement_generation_changed',409)
            yield active[1];return
        end=time.monotonic()+timeout
        with (self.root/'generation.lock').open('a+') as handle:
            while True:
                try:lock_exclusive(handle);break
                except (OSError,BlockingIOError):
                    if time.monotonic()>=end:raise StoreError('publication_busy',423)
                    time.sleep(.05)
            value=self.retirement_snapshot()
            if expected is not None and value['generation']!=expected:raise StoreError('retirement_generation_changed',409)
            _GUARD_LOCAL.active=(key,value)
            try:yield value
            finally:_GUARD_LOCAL.active=None
    def retirement_for(self,source_id):
        with self.connect() as c:row=c.execute('SELECT transaction_id,state,mapping,revision FROM source_tombstones WHERE source_id=?',(source_id,)).fetchone()
        return {'deletion_id':row[0],'state':row[1],'retained_assets':json.loads(row[2]),'revision':row[3]} if row else None
    def report_derivative_stage(self,transaction_id,stage,status,detail=None):
        if stage not in STAGES or status not in STATUS:raise StoreError('invalid_stage')
        with self.connect() as c:
            if not c.execute('SELECT 1 FROM deletion_transactions WHERE id=?',(transaction_id,)).fetchone():raise StoreError('unknown_deletion',404)
            c.execute('INSERT OR REPLACE INTO derivative_stages VALUES(?,?,?,?)',(transaction_id,stage,status,canonical(detail or {})))
            c.execute('INSERT INTO stage_events(transaction_id,stage,status,detail,at) VALUES(?,?,?,?,?)',(transaction_id,stage,status,canonical(detail or {}),now()))
