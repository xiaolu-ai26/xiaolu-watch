import _support  # noqa: F401  (adds receiver/ to sys.path)
import http.client,threading,unittest
import https_ingress as i
class GatewayChecks(unittest.TestCase):
 def test_routes_limits(self):
  server=i.Server(('127.0.0.1',0),1);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  def request(method,path,body=None,headers={}):
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3);c.request(method,path,body,headers);r=c.getresponse();status=r.status;data=r.read();c.close();return status,data
  try:
   self.assertEqual(request('GET','/health'),(200,b'{"ok":true}'))
   for p in ['/','/objects/x','/private/config.json','/v2/chunks/x']:self.assertEqual(request('GET',p)[0],404)
   self.assertEqual(request('POST','/v2/chunks/x')[0],404)
   path='/v2/chunks/11111111-1111-4111-8111-111111111111';h={'X-Watch-Challenge':'22222222-2222-4222-8222-222222222222','Content-Type':'application/octet-stream'}
   self.assertEqual(request('PUT',path,b'x',h)[0],413)
   self.assertEqual(request('PUT',path,b'x'*33,{**h,'Authorization':'Bearer nope'})[0],400)
   server.tokens=0;self.assertEqual(request('PUT',path,b'x'*33,h)[0],429)
  finally:server.shutdown();server.server_close()
 def test_deadline_cancels_slow_upstream_and_releases_slot(self):
  import socketserver,time,socket
  released=threading.Event()
  class Slow(socketserver.BaseRequestHandler):
   def handle(self):
    self.request.recv(65536);released.wait(2)
  upstream=socketserver.ThreadingTCPServer(('127.0.0.1',0),Slow);upstream.daemon_threads=True
  threading.Thread(target=upstream.serve_forever,daemon=True).start()
  server=i.Server(('127.0.0.1',0),upstream.server_address[1]);server.request_deadline_seconds=.25
  threading.Thread(target=server.serve_forever,daemon=True).start()
  started=time.monotonic()
  try:
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
   c.request('PUT','/v2/chunks/11111111-1111-4111-8111-111111111111',b'x'*33,{'X-Watch-Challenge':'22222222-2222-4222-8222-222222222222','Content-Type':'application/octet-stream'})
   try:
    response=c.getresponse();self.assertEqual(response.status,502);response.read()
   except (http.client.HTTPException,ConnectionError,OSError):pass
   c.close();time.sleep(.15)
   self.assertLess(time.monotonic()-started,1)
   self.assertTrue(server.slots.acquire(False));self.assertTrue(server.slots.acquire(False));server.slots.release();server.slots.release()
   # A client that sends headers but never sends the body must free its slot too.
   sock=socket.create_connection(('127.0.0.1',server.server_port),timeout=2)
   sock.sendall(b'PUT /v2/chunks/11111111-1111-4111-8111-111111111111 HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\nX-Watch-Challenge: 22222222-2222-4222-8222-222222222222\r\nContent-Type: application/octet-stream\r\n\r\n')
   time.sleep(.5);sock.close()
   self.assertTrue(server.slots.acquire(False));self.assertTrue(server.slots.acquire(False));server.slots.release();server.slots.release()
  finally:released.set();server.shutdown();server.server_close();upstream.shutdown();upstream.server_close()
 def test_connection_close_drip_body_deadline(self):
  import socketserver,time
  done=threading.Event()
  class Drip(socketserver.BaseRequestHandler):
   def handle(self):
    try:
     self.request.recv(65536);self.request.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 1000\r\n\r\n')
     while not done.wait(.04):self.request.sendall(b'x')
    except OSError:pass
  upstream=socketserver.ThreadingTCPServer(('127.0.0.1',0),Drip);upstream.daemon_threads=True
  threading.Thread(target=upstream.serve_forever,daemon=True).start()
  server=i.Server(('127.0.0.1',0),upstream.server_address[1]);server.request_deadline_seconds=.25
  threading.Thread(target=server.serve_forever,daemon=True).start()
  try:
   c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2);start=time.monotonic()
   c.request('PUT','/v2/chunks/11111111-1111-4111-8111-111111111111',b'x'*33,{'X-Watch-Challenge':'22222222-2222-4222-8222-222222222222','Content-Type':'application/octet-stream'})
   try:r=c.getresponse();r.read()
   except (http.client.HTTPException,OSError):pass
   c.close();time.sleep(.15);self.assertLess(time.monotonic()-start,1)
   self.assertTrue(server.slots.acquire(False));self.assertTrue(server.slots.acquire(False));server.slots.release();server.slots.release()
  finally:done.set();server.shutdown();server.server_close();upstream.shutdown();upstream.server_close()
if __name__=='__main__':unittest.main()
