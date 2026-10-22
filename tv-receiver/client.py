"""Authenticated LAN sender. --approved is supplied only after consent by the caller."""
import argparse, base64, json, time, urllib.request, urllib.error, uuid
from pathlib import Path

class Receiver:
    def __init__(self, config):
        self.base='http://'+config['host']+':40100'
        self.launch_url='http://'+config['host']+':6095/controller?action=startapp&type=packagename&packagename=org.lira.receiver'
        self.token=config['token']
    def request(self,path,body=None):
        req=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as r:
            return json.load(r)
    def clear(self,expected_content_id):
        import re
        if not isinstance(expected_content_id,str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}',expected_content_id):
            raise ValueError('Invalid content ID')
        status=self.request('/status')
        if 'clear_content_id' not in status.get('capabilities',[]):raise RuntimeError('Receiver upgrade required')
        if not status.get('interactive') or not status.get('foreground'):raise RuntimeError('Display unavailable or dismissed')
        result=self.request('/clear',{'expected_content_id':expected_content_id})
        if result.get('cleared_id')!=expected_content_id:raise RuntimeError('Clear not confirmed')
        return result

    def show(self,payload,wait=0):
        if payload.get('approved') is not True:raise ValueError('Foreground consent required')
        deadline=time.monotonic()+max(wait,30)
        last_launch=-float('inf')
        payload=dict(payload,id=payload.get('id') or str(uuid.uuid4()))
        while time.monotonic()<deadline:
            try:
                status=self.request('/status')
            except urllib.error.HTTPError:
                raise
            except OSError:
                if time.monotonic()-last_launch>=10:
                    last_launch=time.monotonic()
                    # Older MiTV firmware may execute the launch without returning HTTP.
                    # Only the paired receiver's later rendered acknowledgement is success.
                    try:
                        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(self.launch_url,timeout=3) as response:
                            response.read(1024)
                    except OSError:
                        pass
                time.sleep(2)
                continue
            if not status['interactive']:
                time.sleep(2)
                continue
            try:
                self.request('/show',payload)
            except urllib.error.HTTPError as exc:
                if exc.code!=409:raise
                time.sleep(2)
                continue
            except OSError:
                time.sleep(2)
                continue
            for _ in range(15):
                status=self.request('/status')
                if status['rendered_id']==payload['id'] and status['foreground']:return status
                time.sleep(.2)
            # Do not reopen a presentation the family may have just dismissed.
            raise TimeoutError('Content accepted but display not confirmed; new consent required')
        raise TimeoutError('TV receiver did not become ready before the retry deadline')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path(__file__).parent/'build/client.json')
    p.add_argument('--wait',type=int,default=0,help='Retry for at most this many seconds while TV comes online')
    p.add_argument('--approved',action='store_true',help='Caller has permission to switch the TV foreground')
    p.add_argument('mode',choices=['status','text','image','clear']);p.add_argument('content',nargs='?');p.add_argument('--caption',default='')
    args=p.parse_args();receiver=Receiver(json.loads(args.config.read_text(encoding='utf-8')))
    if args.mode=='status':result=receiver.request('/status')
    elif args.mode=='clear':
        if not args.content:p.error('Expected content ID required')
        result=receiver.clear(args.content)
    else:
        if not args.approved:p.error('Show requires --approved after consent')
        if not args.content:p.error('Content is required')
        body={'type':args.mode,'approved':True,'text':args.content if args.mode=='text' else args.caption}
        if args.mode=='image':body['image_base64']=base64.b64encode(Path(args.content).read_bytes()).decode()
        result=receiver.show(body,max(0,args.wait))
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
