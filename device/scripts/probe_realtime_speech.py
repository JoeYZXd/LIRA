"""Synthetic cloud-only exact speech proof; silent paced sink, no hardware I/O."""
import asyncio,json,os,sys,time,argparse
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,os.environ.get('LIRA_PROBE_CODE_ROOT',str(Path(__file__).resolve().parents[1])))
from probe_realtime_tool_protocol import config
from lira.audio.doubao_realtime import RealtimeSession
from lira.realtime_capabilities import CapabilityRegistry
from lira.realtime_speech import RealtimeSpeech
from lira.agent_task_state import AgentTaskState
class Player:
    def __init__(self):self.frames=0;self.drains=[]
    async def open(self):pass
    async def stop(self):pass
    async def close(self):pass
    async def play_stream(self,chunks):
        async for pcm in chunks:
            self.frames+=len(pcm)//2
            await asyncio.sleep(len(pcm)/48000)
        await asyncio.sleep(.2)
        self.drains.append(time.monotonic())
async def case(name,audio):
    start=time.monotonic();events=[];tools=[];protocol=[];holder={};began=asyncio.Event()
    registry=CapabilityRegistry();player=Player()
    rt=NS(privacy=NS(is_on=False));state=AgentTaskState()
    state.begin('synthetic-speech','anonymous-test')
    RealtimeSpeech(rt,lambda:holder.get('session'),state).register(registry)
    def event(kind,text):
        if kind!='activity':events.append(dict(at=round(time.monotonic()-start,3),kind=kind,text=str(text)[:500]))
        if kind=='assistant_prompt':began.set()
    def diagnostic(direction,kind,data,turn):
        if direction=='received':
            protocol.append(dict(at=round(time.monotonic()-start,3),kind=kind))
        if kind=='response.function_call_arguments.done':
            tools.extend(dict(at=round(time.monotonic()-start,3),name=i.get('name')) for i in data.get('items',[]))
    async def legacy(*args):return {'status':'unavailable'}
    session=holder['session']=RealtimeSession(config=config(),player=player,on_event=event,on_tool=legacy,capabilities=registry,on_diagnostic=diagnostic)
    feeder=None
    async def silence():
        import numpy as np
        while True:
            session.feed(np.zeros(1600,np.float32));await asyncio.sleep(.1)
    async def feed(samples):
        nonlocal feeder
        if feeder:feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
        for i in range(0,len(samples),1600):session.feed(samples[i:i+1600]);await asyncio.sleep(.1)
        feeder=asyncio.create_task(silence())
    try:
        await session.open();await feed(audio['request'])
        await asyncio.wait_for(began.wait(),25)
        if name=='interrupt':await feed(audio['interrupt'])
        elif name=='cancel':
            ops=session.capabilities.metadata()
            op=next(o for o in ops if o['name']=='start_exact_speech')
            await session.capabilities.call('cancel_capability_operation',{'operation_id':op['operation_id']},'probe-local-cancel')
        deadline=time.monotonic()+28
        while time.monotonic()<deadline:
            ops=session.capabilities.metadata()
            if any(o['name']=='start_exact_speech' and o['status']!='pending' for o in ops):break
            await asyncio.sleep(.1)
        snapshots=[]
        for op in session.capabilities.metadata():
            if op['name']=='start_exact_speech':
                snapshots.append(await session.capabilities.call('get_capability_operation',{'operation_id':op['operation_id']},'probe-read-'+op['operation_id']))
        await asyncio.sleep(2)
        expected='completed' if name=='complete' else ('cancelled' if name=='cancel' else 'interrupted')
        actual=[s.get('result',{}).get('status',s['status']) for s in snapshots]
        prompts=[e for e in events if e['kind']=='assistant_prompt']
        passed=len(snapshots)==1 and len(prompts)==1 and actual==[expected] and not session.failed
        if name=='complete':passed=passed and bool(player.drains) and player.frames>0
        return dict(case=name,passed=passed,expected=expected,actual=actual,pcm_frames=player.frames,drains=[round(t-start,3) for t in player.drains],events=events,tools=tools,protocol=protocol,operations=snapshots,session_failed=session.failed)
    except Exception as exc:
        snapshots=[]
        for op in session.capabilities.metadata():
            if op['name']=='start_exact_speech':
                snapshots.append(await session.capabilities.call('get_capability_operation',{'operation_id':op['operation_id']},'probe-failure-read-'+op['operation_id']))
        task_states=[dict(done=t.done(),cancelled=t.cancelled(),error_type=type(t.exception()).__name__ if t.done() and not t.cancelled() and t.exception() else None) for t in session.tasks]
        return dict(case=name,passed=False,error_type=type(exc).__name__,events=events,tools=tools,protocol=protocol,operations=snapshots,barriers=session._speech_barriers,session_failed=session.failed,task_states=task_states,response_busy=session._response_busy,output_occupied=session.output_occupied,speech_requested=bool(session._speech_request))
    finally:
        if feeder:feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
        await session.close()
async def main(args):
    import numpy as np
    from lira.audio.tts import TtsEngine
    from lira.audio.resample import to_16khz
    tts=await asyncio.to_thread(TtsEngine);audio={}
    try:
        for key,text in {'request':'请调用精确播报工具，逐字播报：青色盒子，橙色纸船，紫色风筝。这是一段合成播报测试。','interrupt':'请先停一下，暂停这次播报。'}.items():
            samples=await asyncio.to_thread(tts.generate,text);pcm=to_16khz(samples,tts.sample_rate);audio[key]=np.pad(pcm,(0,(-len(pcm))%320))
    finally:await tts.close()
    rows=[]
    for repeat in range(args.repeat):
        for name in args.case:
            row=await case(name,audio);row['repeat']=repeat+1;rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
            Path(args.output).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    return all(r['passed'] for r in rows)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--case',action='append',choices=['complete','interrupt','cancel']);p.add_argument('--repeat',type=int,choices=range(1,4),default=1);p.add_argument('--output',default='/tmp/lira-u5-cloud.json');args=p.parse_args();args.case=args.case or ['complete','interrupt','cancel'];sys.exit(0 if asyncio.run(main(args)) else 1)
