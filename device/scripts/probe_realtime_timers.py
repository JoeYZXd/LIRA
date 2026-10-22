"""Synthetic live timer/negotiation proof; no service mutation or hardware I/O."""
import asyncio,json,os,sys,time,argparse,subprocess,re,copy,hashlib
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,os.environ.get('LIRA_PROBE_CODE_ROOT',str(Path(__file__).resolve().parents[1])))
from probe_realtime_tool_protocol import config
from probe_realtime_speech import Player
from lira.audio.doubao_realtime import RealtimeSession
from lira.realtime_capabilities import CapabilityRegistry
from lira.agent_task_state import AgentTaskState,AgentAgreementVerifier
from lira.agent_timers import AgentTimers

async def run(name,audio,rt,checkpoint=None):
    start=time.monotonic();events=[];tools=[];result_calls=[];semantic_calls=[];waiting=asyncio.Event()
    state=AgentTaskState(AgentAgreementVerifier(rt));state.begin('synthetic-timer','anonymous-test')
    timers=AgentTimers(state);registry=CapabilityRegistry();state.register(registry);timers.register(registry)
    def event(kind,text):
        if kind=='user_text':state.observe_user(text)
        elif kind=='assistant_delivered':state.observe_assistant(text)
        if kind!='activity':events.append(dict(at=round(time.monotonic()-start,3),kind=kind,text=str(text)[:2000]))
    def diagnostic(direction,kind,data,turn):
        if kind=='response.function_call_arguments.done':
            for item in data.get('items',[]):
                tools.append(dict(at=round(time.monotonic()-start,3),name=item.get('name')))
                if item.get('name')=='wait_for_task_event':waiting.set()
    async def legacy(*args):return {'status':'unavailable'}
    session=RealtimeSession(config=config(),player=Player(),on_event=event,on_tool=legacy,capabilities=registry,on_diagnostic=diagnostic)
    original_call=session.capabilities.call
    async def recorded_call(tool_name,args,call_id):
        # Local audit reads are separate evidence, never model consumption.
        if call_id.startswith('local-read-'):
            return await original_call(tool_name,args,call_id)
        row=dict(at=round(time.monotonic()-start,3),name=tool_name,
                 args=copy.deepcopy(args),call_id=call_id)
        result_calls.append(row)
        result=await original_call(tool_name,args,call_id)
        row['result']=copy.deepcopy(result)
        return result
    session.capabilities.call=recorded_call
    original_request=rt._llm._client.chat.completions.create
    async def observed_verifier(**kwargs):
        row={'at':round(time.monotonic()-start,3)};semantic_calls.append(row)
        try:
            reply=await original_request(**kwargs)
            row.update(finish_reason=reply.choices[0].finish_reason,
                       content=reply.choices[0].message.content)
            return reply
        except BaseException as exc:
            row['error_type']=type(exc).__name__
            raise
        finally:
            row['duration_seconds']=round(time.monotonic()-start-row['at'],3)
    rt._llm._client.chat.completions.create=observed_verifier
    feeder=None
    async def silence():
        import numpy as np
        while True:session.feed(np.zeros(1600,np.float32));await asyncio.sleep(.1)
    async def feed(samples):
        nonlocal feeder
        if feeder:feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
        for i in range(0,len(samples),1600):session.feed(samples[i:i+1600]);await asyncio.sleep(.1)
        feeder=asyncio.create_task(silence())
    snapshots=[];error=None
    try:
        await session.open();await feed(audio['request'])
        await asyncio.wait_for(waiting.wait(),65)
        if name=='barge':await feed(audio['barge'])
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            ops=session.capabilities.metadata()
            if any(o['name']=='wait_for_task_event' and o['status']=='completed' for o in ops):break
            await asyncio.sleep(.1)
        await feed(audio['read'])
        await asyncio.sleep(18)
    except Exception as exc:error=type(exc).__name__
    finally:
        for op in session.capabilities.metadata():
            if op['name'] in ('wait_for_task_event','create_task_timer','activate_agent_task'):
                snapshots.append(dict(name=op['name'],value=await session.capabilities.call('get_capability_operation',{'operation_id':op['operation_id']},'local-read-'+op['operation_id'])))
        ids=[s['value'].get('result',{}).get('event',{}).get('event_id') for s in snapshots if s['name']=='wait_for_task_event' and s['value'].get('result',{}).get('event')]
        spoken=''.join(e['text'] for e in events if e['kind']=='assistant_text')
        compact=re.sub('[^0-9a-f]','',spoken.lower())
        passed=not error and len(ids)==1 and ids[0] in compact and len(timers.timers)==1 and state.phase=='active' and not session.failed
        root=Path(os.environ.get('LIRA_PROBE_CODE_ROOT',str(Path(__file__).resolve().parents[1])))
        source_hashes={rel:hashlib.sha256((root/rel).read_bytes()).hexdigest()
                       for rel in ('lira/audio/doubao_realtime.py','lira/realtime_capabilities.py','lira/agent_timers.py')}
        row=dict(case=name,passed=passed,error=error,events=events,tools=tools,
                 result_calls=result_calls,semantic_calls=semantic_calls,source_hashes=source_hashes,
                 script_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 operations=snapshots,event_ids=ids,agreement=state.snapshot(),timer_count=len(timers.timers),session_failed=session.failed)
        if checkpoint:
            Path(checkpoint).write_text(json.dumps({'cleanup_pending':True,'row':row},ensure_ascii=False,indent=2),encoding='utf-8')
        try:
            if feeder:feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
            timers.close();await session.close()
        finally:
            rt._llm._client.chat.completions.create=original_request
    if checkpoint:
        Path(checkpoint).write_text(json.dumps({'cleanup_pending':False,'row':row},ensure_ascii=False,indent=2),encoding='utf-8')
    return row

async def main(args):
    from lira.config import load_config
    from lira.llm.client import LlmClient
    from lira.audio.tts import TtsEngine
    from lira.audio.resample import to_16khz
    import numpy as np
    pid=subprocess.check_output(['systemctl','--user','show','lira','--property=MainPID','--value'],text=True).strip()
    env=dict(part.decode().split('=',1) for part in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in part)
    os.chdir('/home/orangepi/lira/device');cfg=load_config(env=env)
    llm=LlmClient(cfg.llm,privacy=lambda:False,network_ok=lambda:True);rt=NS(_llm=llm,_cfg=cfg,privacy=NS(is_on=False))
    tts=await asyncio.to_thread(TtsEngine);audio={}
    try:
        for key,text in {
            'request':'这是合成能力测试。我明确同意本次临时约定：创建一个十二秒计时，到期仅报告事件，不自动推进。请先用任务工具记录并激活这个约定，再创建十二秒计时，调用等待事件工具。',
            'barge':'另外问一句，你能听到吗？原来的计时和约定保持不变，不要新建，不要取消。',
            'read':'请读取刚才等待任务的真实结果，完整逐字读出事件编号，保留全部字母和数字。找不到就明确说找不到，不要猜测，不要重新创建计时。'
        }.items():
            samples=await asyncio.to_thread(tts.generate,text);pcm=to_16khz(samples,tts.sample_rate);audio[key]=np.pad(pcm,(0,(-len(pcm))%320))
    finally:await tts.close()
    rows=[]
    try:
        for repeat in range(args.repeat):
            for name in args.case:
                row=await run(name,audio,rt,args.output+'.checkpoint.json');row['repeat']=repeat+1;row['verifier_model']=cfg.llm.model;rows.append(row)
                Path(args.output).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(dict(case=name,repeat=repeat+1,passed=row['passed'],error=row['error'],event_ids=row['event_ids'])),flush=True)
    finally:await llm._client.close()
    return all(r['passed'] for r in rows)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--case',action='append',choices=['normal','barge']);p.add_argument('--repeat',type=int,choices=range(1,4),default=1);p.add_argument('--output',default='/tmp/lira-u8-cloud.json');args=p.parse_args();args.case=args.case or ['normal','barge'];sys.exit(0 if asyncio.run(main(args)) else 1)
