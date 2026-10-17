"""Loopback-only HTTPS upstream. No TLS bypass, credentials, reads, or management.

Tailscale Funnel or your own reverse proxy terminates public TLS. This gateway forwards only bounded sealed v2 PUTs
and the opaque receipt MAC; the receiver authenticates AES-GCM before storage.
"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import contextlib,http.client,re,socket,threading,time,sqlite3
import wire_v2

class Server(ThreadingHTTPServer):
    daemon_threads=True
    request_deadline_seconds=90
    def __init__(self,address,upstream_port,bootstrap=None):
        if address[0]!='127.0.0.1':raise ValueError('Ingress must remain loopback-only')
        self.bootstrap=bootstrap
        self.upstream_port=upstream_port;self.slots=threading.BoundedSemaphore(2);self.rate_lock=threading.Lock();self.tokens=120.;self.last=time.monotonic()
        super().__init__(address,Handler)
    def rate_allow(self):
        with self.rate_lock:
            now=time.monotonic();self.tokens=min(120.,self.tokens+(now-self.last)*2);self.last=now
            if self.tokens<1:return False
            self.tokens-=1;return True
    def process_request(self,request,client_address):
        if not self.slots.acquire(False):self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version=''
    sys_version=''
    def log_message(self,*args):pass
    def setup(self):
        super().setup();self.connection.settimeout(20)
        self.expires_at=time.monotonic()+self.server.request_deadline_seconds
        self.upstream_lock=threading.Lock();self.upstream=None;self.upstream_socket=None;self.expired=False
        self.deadline=threading.Timer(self.server.request_deadline_seconds,self.expire);self.deadline.daemon=True;self.deadline.start()
    def remaining(self):
        remaining=self.expires_at-time.monotonic()
        if remaining<=0:raise TimeoutError('request deadline')
        return remaining
    def expire(self):
        with self.upstream_lock:
            self.expired=True
            upstream=self.upstream
            actual_socket=self.upstream_socket
            if actual_socket:
                with contextlib.suppress(OSError):actual_socket.shutdown(socket.SHUT_RDWR)
            if upstream:upstream.close()
        with contextlib.suppress(OSError):self.connection.shutdown(socket.SHUT_RDWR)
    def finish(self):self.deadline.cancel();super().finish()
    def reply(self,status,body=b'{}',mac=None,no_store=False):
        self.close_connection=True
        with contextlib.suppress(OSError):
            self.send_response_only(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
            if mac:self.send_header('X-Watch-Receipt-MAC',mac)
            if no_store:self.send_header('Cache-Control','no-store');self.send_header('Pragma','no-cache')
            self.end_headers();self.wfile.write(body)
    def do_GET(self):self.reply(200,b'{"ok":true}') if self.path=='/health' else self.reply(404)
    def do_POST(self):
        if self.path!='/v2/pair/claim' or self.server.bootstrap is None:self.reply(404);return
        if not self.server.rate_allow():self.reply(429);return
        lengths=self.headers.get_all('Content-Length',[])
        if self.headers.get('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isdigit():self.reply(411);return
        size=int(lengths[0])
        if not 1<=size<=1024:self.reply(413);return
        if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':self.reply(415);return
        try:
            import json
            self.connection.settimeout(min(10,self.remaining()))
            raw=self.rfile.read(size)
            if len(raw)!=size:self.reply(400);return
            def unique(pairs):
                result={}
                for k,v in pairs:
                    if k in result:raise ValueError('duplicate key')
                    result[k]=v
                return result
            value=json.loads(raw.decode('utf-8'),object_pairs_hook=unique)
            status,data=self.server.bootstrap.claim(value,self.client_address[0])
            payload=json.dumps(data,separators=(',',':')).encode('utf-8')
            if len(payload)>8192:self.reply(500);return
            self.reply(status,payload,no_store=True)
        except (ValueError,UnicodeError):self.reply(400)
        except (OSError,sqlite3.Error):self.reply(503)
    def do_DELETE(self):self.reply(405)
    def do_PUT(self):
        if not self.server.rate_allow():self.reply(429);return
        if not re.fullmatch(r'/v2/chunks/[0-9a-f-]{36}',self.path):self.reply(404);return
        if not wire_v2.canonical_uuid(self.path.rsplit('/',1)[-1]) or not wire_v2.canonical_uuid(self.headers.get('X-Watch-Challenge','')):self.reply(400);return
        lengths=self.headers.get_all('Content-Length',[])
        if self.headers.get('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isdigit():self.reply(411);return
        size=int(lengths[0])
        if not 33<=size<=wire_v2.MAX_WIRE:self.reply(413);return
        if self.headers.get('Authorization') or self.headers.get('X-Watch-Metadata'):self.reply(400);return
        if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/octet-stream':self.reply(415);return
        upstream=None
        try:
            self.connection.settimeout(min(20,self.remaining()))
            body=self.rfile.read(size)
            if len(body)!=size:self.reply(400);return
            upstream=http.client.HTTPConnection('127.0.0.1',self.server.upstream_port,timeout=min(60,self.remaining()))
            with self.upstream_lock:
                if self.expired:raise TimeoutError('request deadline')
                self.upstream=upstream
            upstream.connect()
            with self.upstream_lock:
                self.upstream_socket=upstream.sock
                if self.expired:
                    if self.upstream_socket:self.upstream_socket.shutdown(socket.SHUT_RDWR)
                    raise TimeoutError('request deadline')
            if upstream.sock:upstream.sock.settimeout(self.remaining())
            upstream.request('PUT',self.path,body=body,headers={'Content-Type':'application/octet-stream','Content-Length':str(size),'X-Watch-Challenge':self.headers['X-Watch-Challenge']})
            if upstream.sock:upstream.sock.settimeout(self.remaining())
            r=upstream.getresponse()
            if upstream.sock:upstream.sock.settimeout(self.remaining())
            reply=r.read(65537)
            if len(reply)>65536:self.reply(502);return
            self.reply(r.status,reply,r.getheader('X-Watch-Receipt-MAC'))
        except (OSError,http.client.HTTPException):self.reply(502)
        finally:
            if upstream:upstream.close()
            with self.upstream_lock:self.upstream=None;self.upstream_socket=None

def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18438);p.add_argument('--upstream-port',type=int,default=18437);p.add_argument('--bootstrap-root');a=p.parse_args()
    bootstrap=None
    if a.bootstrap_root:
        from bootstrap_pairing import Bootstrap
        bootstrap=Bootstrap(a.bootstrap_root)
    Server(('127.0.0.1',a.port),a.upstream_port,bootstrap).serve_forever()
if __name__=='__main__':main()
