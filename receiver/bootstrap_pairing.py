"""One-time, high-entropy Watch bootstrap. Audio v2 protocol is unchanged.

Only local CLI can issue codes. The public claim route cannot issue/list codes.
Codes are hashed in SQLite; the private local pairing card is not a server route.
"""
import argparse,contextlib,datetime,base64,hashlib,hmac,json,os,re,secrets,sqlite3,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
from platform_store import replace,sync_directory

DENIED={'error':'pairing_not_authorized'}

def configuration(root,endpoint):
    url=urlsplit(endpoint)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ['', '/']:
        raise ValueError('Bootstrap requires an HTTPS origin')
    c=json.loads((Path(root)/'private/config.json').read_text(encoding='utf-8'))
    return {'version':2,'endpoint':endpoint.rstrip('/'),'pairing_secret':c['pairing_secret'],'protocol':'watch-memory-chunks-v2'}

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')

class Bootstrap:
    def __init__(self,root,clock=time.time):
        self.root=Path(root);self.clock=clock;self.db=self.root/'private/bootstrap.sqlite3'
        self.db.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS codes (digest TEXT PRIMARY KEY, expires REAL NOT NULL, endpoint TEXT NOT NULL, config_digest TEXT NOT NULL, attempt_id TEXT, claims INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS buckets (name TEXT PRIMARY KEY, tokens REAL NOT NULL, updated REAL NOT NULL)')
        os.chmod(self.db,0o600)
    @contextlib.contextmanager
    def connect(self):
        db=sqlite3.connect(self.db,timeout=5);db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()
    def issue(self,endpoint):
        config=configuration(self.root,endpoint);code=base64.b32encode(secrets.token_bytes(10)).decode('ascii');now=self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');db.execute('DELETE FROM codes WHERE expires<=?',(now,))
            db.execute('INSERT INTO codes(digest,expires,endpoint,config_digest) VALUES(?,?,?,?)',(hashlib.sha256(code.encode()).hexdigest(),now+600,config['endpoint'],hashlib.sha256(encoded(config)).hexdigest()))
        return code,now+600
    def _bucket(self,db,name,capacity,per_second,now):
        row=db.execute('SELECT tokens,updated FROM buckets WHERE name=?',(name,)).fetchone()
        tokens=capacity if row is None else min(capacity,row[0]+max(0,now-row[1])*per_second)
        allowed=tokens>=1
        db.execute('INSERT OR REPLACE INTO buckets VALUES(?,?,?)',(name,tokens-1 if allowed else tokens,now))
        return allowed
    def claim(self,value,peer):
        began=time.monotonic();status=401;body=DENIED
        # Only the actual socket peer is used. Never trust a public X-Forwarded-For.
        # Behind Funnel, the loopback peer bucket is an aggregate stricter limit.
        now=self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            global_ok=self._bucket(db,'global',10,10/60,now)
            peer_key='peer:'+hashlib.sha256(str(peer).encode()).hexdigest()
            peer_ok=self._bucket(db,peer_key,5,5/60,now)
            db.execute('DELETE FROM buckets WHERE updated<? AND name!=?',(now-3600,'global'))
            if not global_ok or not peer_ok:status=429;body={'error':'pairing_rate_limited'}
            elif isinstance(value,dict) and set(value)=={'code','attempt_id'}:
                code=value['code'];attempt=value['attempt_id']
                if isinstance(code,str) and len(code)<=64 and isinstance(attempt,str):
                    code=code.replace('-','').replace(' ','').upper()
                    try:valid_attempt=str(uuid.UUID(attempt))==attempt
                    except (ValueError,AttributeError):valid_attempt=False
                    if re.fullmatch('[A-Z2-7]{16}',code) and valid_attempt:
                        digest=hashlib.sha256(code.encode('ascii')).hexdigest()
                        row=db.execute('SELECT expires,endpoint,config_digest,attempt_id,claims FROM codes WHERE digest=?',(digest,)).fetchone()
                        if row and now<row[0] and row[4]<8:
                            config=configuration(self.root,row[1]);current=hashlib.sha256(encoded(config)).hexdigest()
                            bound=row[3]
                            # Commit the first binding before returning any static secret.
                            if hmac.compare_digest(row[2],current) and (bound is None or hmac.compare_digest(bound,attempt)):
                                db.execute('UPDATE codes SET attempt_id=?,claims=claims+1 WHERE digest=?',(attempt,digest));status=200;body=config
                            else:db.execute('UPDATE codes SET claims=claims+1 WHERE digest=?',(digest,))
        # Uniform denial latency for unknown, expired, malformed and wrong-binding codes.
        if status==401:time.sleep(max(0,.05-(time.monotonic()-began)))
        return status,body

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path(__file__).resolve().parent.parent/'data'/'receiver');p.add_argument('--endpoint',required=True);args=p.parse_args()
    code,expires=Bootstrap(args.root).issue(args.endpoint)
    card=args.root/'private/watch-pairing-card.txt';temp=card.with_suffix('.pending')
    with temp.open('w',encoding='utf-8') as f:
        os.chmod(temp,0o600);f.write('小鹿录音：手表首次连接\n\nHTTPS 地址：'+args.endpoint+'\n\n一次性配对码：'+'-'.join(code[i:i+4] for i in range(0,16,4))+'\n\n到期时间：'+datetime.datetime.fromtimestamp(expires).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')+'\n\n仅用于本次手表初始化，勿公开。\n');f.flush();os.fsync(f.fileno())
    replace(temp,card);sync_directory(card.parent)
    print(json.dumps({'private_pairing_card':str(card),'expires_at':expires,'entropy_bits':80,'code_printed':False,'secret_printed':False}))
if __name__=='__main__':main()
