"""Non-destructive protocol checks against the paired, awake television."""
import json, urllib.error, socket, threading, time
from pathlib import Path
from client import Receiver

def main():
    config=json.loads((Path(__file__).parent/'build/client.json').read_text(encoding='utf-8'))
    receiver=Receiver(config)
    before=receiver.request('/status')['rendered_id']
    def denied(client,path,body,expected):
        try:client.request(path,body)
        except urllib.error.HTTPError as e:assert e.code==expected,(e.code,expected)
        else:raise AssertionError('Request unexpectedly accepted')
    bad=Receiver(dict(config,token='incorrect'))
    denied(bad,'/status',None,401)
    denied(receiver,'/show',{'id':'no-consent','type':'text','text':'Do not display'},403)
    denied(receiver,'/show',{'id':'invalid-image','type':'image','image_base64':'broken','approved':True},400)
    denied(receiver,'/show',{'id':'empty','type':'text','text':'','approved':True},400)
    denied(receiver,'/show',{'id':'oversize','type':'text','text':'x'*(4*1024*1024),'approved':True},413)
    assert receiver.request('/status')['rendered_id']==before,'Rejected input changed the screen'
    print('PASS: authentication, consent, malformed image, empty text, body limit; screen unchanged')
    with socket.create_connection((config['host'],40100),timeout=5) as slow:
        stopped=threading.Event()
        def drip():
            while not stopped.wait(.3):
                try:slow.sendall(b'x')
                except OSError:break
        slow.sendall(b'G');thread=threading.Thread(target=drip,daemon=True);thread.start()
        try:
            time.sleep(.1);start=time.monotonic()
            assert receiver.request('/status')['rendered_id']==before
            assert time.monotonic()-start<5,'Slow client monopolized listener'
        finally:stopped.set();thread.join(timeout=1)
    print('PASS: slow unauthenticated request cannot extend the absolute read deadline')
if __name__=='__main__':main()
