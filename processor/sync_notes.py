#!/usr/bin/env python3
"""Mac side of the Windows-receiver setup: pull finished Markdown notes into your Vault.

The Windows receiver keeps writing notes into its notes outbox (with manifest.json) even
while the Mac is off. When the Mac is online, this script copies new notes over SSH/SCP,
checks SHA-256 and size, writes only into the configured destination folder, never
follows symlinks and never overwrites a note you edited (it is reported as a conflict).

    python3 processor/sync_notes.py            # uses data/settings.json -> "mac_sync"
"""
import argparse,datetime as dt,hashlib,json,os,stat,subprocess,sys,tempfile,uuid
from pathlib import Path,PurePosixPath

REPO=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(REPO/'receiver'))
from platform_store import replace,sync_directory

def now():return dt.datetime.now(dt.timezone.utc).isoformat()

def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name(path.name+'.pending')
    with temp.open('w',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    replace(temp,path);sync_directory(path.parent)

def run(argv,timeout):
    p=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError(f'{Path(argv[0]).name} exit {p.returncode}: {p.stderr[-800:]}')
    return p.stdout

def ssh_options(config):
    options=['-o','BatchMode=yes','-o','ConnectTimeout=10']
    if config.get('ssh_config'):options=['-F',str(config['ssh_config'])]+options
    return options

def safe_relative(raw):
    if not isinstance(raw,str) or '\\' in raw or ':' in raw or any(ord(c)<32 or ord(c)==127 for c in raw):raise ValueError('ambiguous outbox path')
    p=PurePosixPath(raw)
    if p.is_absolute() or len(p.parts)!=2 or p.parts[0] not in ['录音资料','飞书修订快照'] or p.suffix!='.md' or any(x in ['.','..'] for x in p.parts):raise ValueError('outbox path outside scoped project')
    return p

class AnchoredVault:
    def __init__(self,path):self.path=Path(path).absolute();self.fds=[];self.children={}
    def __enter__(self):
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        try:
            current=os.open('/',flags);self.fds.append(('/',None,current))
            for name in self.path.parts[1:]:
                parent=current;current=os.open(name,flags,dir_fd=parent);self.fds.append((name,parent,current))
            self.root=current;self.validate();return self
        except BaseException:self.__exit__(None,None,None);raise
    def validate(self):
        for name,parent,fd in self.fds:
            if parent is None:continue
            named=os.stat(name,dir_fd=parent,follow_symlinks=False);opened=os.fstat(fd)
            if not stat.S_ISDIR(named.st_mode) or (named.st_dev,named.st_ino)!=(opened.st_dev,opened.st_ino):raise RuntimeError('Vault directory identity changed')
    def child(self,name):
        self.validate()
        if name in self.children:return self.children[name]
        try:os.mkdir(name,mode=0o700,dir_fd=self.root)
        except FileExistsError:pass
        fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.root)
        self.fds.append((name,self.root,fd));self.children[name]=fd;self.validate();return fd
    def __exit__(self,*args):
        for _,_,fd in reversed(self.fds):os.close(fd)
        self.fds=[];self.children={}

def existing_hash(fd,name):
    try:handle=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
    except FileNotFoundError:return None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):raise ValueError('Vault target is not regular')
        digest=hashlib.sha256()
        while True:
            block=os.read(handle,1024*1024)
            if not block:break
            digest.update(block)
        return digest.hexdigest()
    finally:os.close(handle)

def publish(anchored,fd,name,source,expected):
    anchored.validate();old=existing_hash(fd,name)
    if old is not None:return 'same' if old==expected else 'conflict'
    temp='.xiaolu-sync-'+uuid.uuid4().hex
    handle=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
    try:
        with os.fdopen(handle,'wb') as dst,source.open('rb') as src:
            for block in iter(lambda:src.read(1024*1024),b''):dst.write(block)
            dst.flush();os.fsync(dst.fileno())
        anchored.validate()
        try:os.link(temp,name,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
        except FileExistsError:return 'same' if existing_hash(fd,name)==expected else 'conflict'
        os.fsync(fd);return 'copied'
    finally:os.unlink(temp,dir_fd=fd)

def remote(config,relative):
    base=str(config['remote_notes_dir']).replace('\\','/').rstrip('/')
    return f"{config['ssh_host']}:{base}/{relative}"

def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--home',type=Path,default=Path(os.environ.get('XIAOLU_WATCH_HOME') or REPO/'data'))
    args=parser.parse_args()
    settings=json.loads((args.home/'settings.json').read_text(encoding='utf-8'))
    config=settings.get('mac_sync') or {}
    if not config.get('enabled'):raise SystemExit('mac_sync is not enabled in settings.json')
    destination=Path(config['destination'])
    if not destination.is_absolute():raise ValueError('Vault destination must be absolute')
    if not destination.is_dir():raise SystemExit(f'Destination folder does not exist: {destination}')
    scp=['scp','-q']+ssh_options(config)
    with tempfile.TemporaryDirectory(prefix='xiaolu-watch-pull-') as td:
        listing=Path(td)/'manifest.json'
        run(scp+[remote(config,'manifest.json'),str(listing)],timeout=30)
        manifest=json.loads(listing.read_text(encoding='utf-8'))
    if manifest.get('version')!=1 or not isinstance(manifest.get('files'),list):raise ValueError('unsupported outbox manifest')
    copied=[];conflicts=[]
    with AnchoredVault(destination) as anchor:
        for item in manifest['files']:
            rel=safe_relative(item['relative_path']);expected=item['sha256'];size=item['bytes']
            if len(expected)!=64 or any(x not in '0123456789abcdef' for x in expected) or type(size) is not int or not 0<size<=16*1024**2:raise ValueError('invalid outbox identity')
            fd=anchor.child(rel.parts[0]);old=existing_hash(fd,rel.name)
            if old is not None:
                if old!=expected:conflicts.append(str(rel))
                continue
            with tempfile.TemporaryDirectory(prefix='xiaolu-watch-pull-') as td:
                tmp=Path(td)/'note.md'
                run(scp+[remote(config,str(rel)),str(tmp)],timeout=60)
                if tmp.stat().st_size!=size or hashlib.sha256(tmp.read_bytes()).hexdigest()!=expected:raise ValueError('note identity mismatch')
                result=publish(anchor,fd,rel.name,tmp,expected)
                if result=='copied':copied.append(str(rel))
                elif result=='conflict':conflicts.append(str(rel))
    atomic_json(args.home/'state'/'mac-sync.json',{'checked_at':now(),'copied':copied,'conflicts_preserved':conflicts,'remote_file_count':len(manifest['files']),'status':'conflict' if conflicts else 'synced'})
    print(json.dumps({'copied_count':len(copied),'conflict_count':len(conflicts)}))
if __name__=='__main__':main()
