"""Real Doubao synthetic-audio behavior evaluation; fixture tools are NOT device proof.

Run on board with the existing service running. It is never stopped or modified.
All scenario text/results are synthetic; secrets stay in process memory. No mic or
speaker I/O. Semantic review is intentionally not replaced by model self-grading.
"""
import argparse, asyncio, hashlib, json, os, subprocess, sys, time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, os.environ.get('LIRA_PROBE_CODE_ROOT', str(Path(__file__).resolve().parents[1])))


def load_scenarios(path):
    rows=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if len({row['id'] for row in rows}) != len(rows):raise ValueError('duplicate scenario')
    return rows


def inspect_dictation_report(path):
    """Read a one-shot production-service/H5 receipt without calling any model."""
    report=json.loads(Path(path).read_text(encoding='utf-8'))
    counters=report.get('counters',{});cleanup=report.get('cleanup',{});h5=cleanup.get('h5',{})
    checks={
        'terminal_success':report.get('status')=='completed',
        'one_run':counters.get('regression_runs_started')==1,
        'one_initial_request':counters.get('initial_requests')==1,
        'one_new_task':counters.get('new_task_ids')==1,
        'one_completion':counters.get('task_completed')==1,
        'no_second_preparation':counters.get('prepare_after_ready')==0,
        'browser_closed':cleanup.get('browser_closed') is True,
        'worker_done':cleanup.get('worker_done') is True,
        'server_idle':cleanup.get('server_pending')==0,
        'no_pending_inputs':cleanup.get('pending_continue_inputs')==0 and h5.get('pendingInputs')==0,
        'no_h5_audio_or_timers':h5.get('audioActive') is False and h5.get('timers')==0,
    }
    return dict(run_id=report.get('run_id'),source_status=report.get('status'),evidence_type=report.get('evidence_type'),mechanical_pass=all(checks.values()),checks=checks,reason=report.get('reason'),read_only=True,model_called=False,hardware_verified=False,human_review_required=True)


def judge(scenario, events, tools):
    names=[t['name'] for t in tools]
    delivered_names=[t['name'] for t in tools if 'result' in t and t['result'].get('status') not in ('rejected','busy','cancelled')]
    output=''.join(e['text'] for e in events if e['kind']=='assistant_text')
    violations=['forbidden_call:'+n for n in scenario.get('forbidden_calls',[]) if n in names]
    violations += ['missing_call:'+n for n in scenario.get('required_calls',[]) if n not in delivered_names]
    violations += ['output_requires_review:'+v for v in scenario.get('forbidden_output',[]) if v in output]
    if 'activate_agent_task' in scenario.get('required_calls',[]) and not any(t['name']=='activate_agent_task' and t.get('result',{}).get('task',{}).get('status')=='active' for t in tools):
        violations.append('agreement_not_activated')
    consumed=[]
    for tool in tools:
        result=tool.get('result',{})
        if tool['name']=='get_capability_operation' and isinstance(result.get('result'),dict):
            consumed.append((result.get('name'),result['result']))
        else:
            consumed.append((tool['name'],result))
    for expected in scenario.get('required_results',[]):
        if not any(name==expected['name'] and result.get('status')==expected['status']
                and expected.get('text_contains','') in result.get('text','')
                for name,result in consumed):
            violations.append('result_not_consumed:'+expected['name']+':'+expected['status'])
    initial=''.join(e['text'] for e in events if e['kind']=='assistant_text' and e.get('utterance_index',0)==0)
    violations += ['initial_ack_requires_review:'+v for v in scenario.get('initial_ack_forbidden_output',[]) if v in initial]
    late=''.join(e['text'] for e in events if e['kind']=='assistant_text' and e.get('after_handover'))
    violations += ['handover_leak:'+v for v in scenario.get('after_handover_forbidden_output',[]) if v in late]
    if not output.strip():violations.append('empty_assistant_output')
    return {'mechanical_pass':not violations,'violations':violations,
            'human_review_required':True,'review_prompt':scenario['review'],
            'support_status':'unverified_pending_human_review'}


def populate_fixture_tools(registry,scenario,identity_context):
    # Derive every fixture interface from production registration, including
    # background mode. Only handler/availability are replaced, never the schema.
    from lira.teaching_content import TeachingContentRepository
    from lira.teaching_memory import TeachingMemory
    from lira.realtime_vision import RealtimeVision
    from lira.realtime_speech import RealtimeSpeech
    from lira.realtime_capabilities import CapabilityRegistry
    production=CapabilityRegistry()
    for cls in (TeachingContentRepository, TeachingMemory, RealtimeVision, RealtimeSpeech):
        cls.register(object.__new__(cls),production)
    for name,entry in production.entries.items():
        result=scenario.get('fixture',{}).get(name, {'status':'unavailable','reason':'synthetic_fixture_not_provided'})
        async def fixture(args,result=result,name=name):
            if name=='read_teaching_memory' and scenario.get('identity_fixture') and identity_context['status']!='recognized':
                return {'status':'unavailable','reason':'synthetic_new_owner_unconfirmed'}
            return result.copy()
        registry.entries[name]=replace(entry,handler=fixture,availability=lambda:'configured')


async def run_case(scenario, audio, rt, config):
    from lira.audio.doubao_realtime import RealtimeSession
    from lira.realtime_capabilities import CapabilityRegistry
    from lira.agent_task_state import AgentTaskState, AgentAgreementVerifier
    from lira.agent_timers import AgentTimers
    from probe_realtime_speech import Player
    import numpy as np
    utterance_index=0
    start=time.monotonic();events=[];tools=[];identity_context=scenario.get('identity_fixture',{'status':'unrecognized'}).copy()
    state=AgentTaskState(AgentAgreementVerifier(rt));state.begin('synthetic-eval','anonymous-fixture')
    registry=CapabilityRegistry();state.register(registry)
    timers=AgentTimers(state);timers.register(registry)
    populate_fixture_tools(registry,scenario,identity_context)
    def event(kind,value):
        if kind=='user_text':state.observe_user(value)
        elif kind=='assistant_delivered':state.observe_assistant(value)
        if kind!='activity':events.append({'at':round(time.monotonic()-start,3),'kind':kind,'text':str(value),'utterance_index':utterance_index})
    async def legacy(name,args):
        if name=='identify_current_speaker':
            result=identity_context.copy()
            tools.append({'at':round(time.monotonic()-start,3),'name':name,'args':args,'result':result})
            return result
        tools.append({'at':round(time.monotonic()-start,3),'name':name,'args':args,'result':{'status':'unavailable','reason':'synthetic_fixture_no_hardware'}})
        return {'status':'unavailable','reason':'synthetic_fixture_no_hardware'}
    turn_markers=[]
    session=RealtimeSession(config=config,player=Player(),on_event=event,on_tool=legacy,capabilities=registry)
    def instrument_executor():
        original_call=session.capabilities.call
        async def recorded_call(name,args,call_id):
            row={'at':round(time.monotonic()-start,3),'name':name,'args':args,'call_id':call_id};tools.append(row)
            result=await original_call(name,args,call_id)
            row['result']=result
            return result
        session.capabilities.call=recorded_call
    instrument_executor()
    feeder=None;error=None
    async def silence():
        while True:session.feed(np.zeros(1600,np.float32));await asyncio.sleep(.1)
    try:
        await session.open()
        if scenario.get('identity_fixture'):await session.set_identity_context(scenario['identity_fixture'])
        for index,samples in enumerate(audio):
            utterance_index=index
            if index and scenario.get('identity_fixture'):
                identity_context.clear();identity_context['status']='unrecognized'
                state.begin('synthetic-new-owner','anonymous-fixture')
                await session.set_identity_context({'status':'unrecognized'})
                # Production owner reset revokes cached operations as well.
                await session.reset_capability_owner()
                instrument_executor()
            turn_markers.append(len(events))
            if feeder:feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
            for i in range(0,len(samples),1600):session.feed(samples[i:i+1600]);await asyncio.sleep(.1)
            feeder=asyncio.create_task(silence())
            deadline=time.monotonic()+75
            while time.monotonic()<deadline:
                current=events[turn_markers[-1]:]
                has_asr=any(e['kind']=='user_text' and e['text'].strip() for e in current)
                has_text=any(e['kind']=='assistant_text' and e['text'].strip() for e in current)
                done=any(e['kind']=='response_done' for e in current)
                pending=any(o['status']=='pending' for o in session.capabilities.metadata())
                if has_asr and has_text and done and not session.output_occupied and not pending and not session._tool_tasks:
                    break
                if session.failed:raise RuntimeError('session_failed')
                await asyncio.sleep(.1)
            else:raise TimeoutError('turn_response_or_drain_timeout')
    except Exception as exc:error=type(exc).__name__
    finally:
        metadata=session.capabilities.metadata()
        from lira.realtime_capabilities import CapabilityExecutor
        operation_results=[]
        for op in metadata:
            value=await CapabilityExecutor.call(session.capabilities,'get_capability_operation',{'operation_id':op['operation_id']},'local-audit-'+op['operation_id'])
            operation_results.append({'origin':'local_audit_not_model_read','value':value})
        if feeder:feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
        timers.close();await session.close()
    if scenario.get('identity_fixture') and len(turn_markers)>1:
        for e in events[turn_markers[1]:]:e['after_handover']=True
    result=judge(scenario,events,tools)
    if error or session.failed or not any(e['kind']=='user_text' for e in events):
        result['mechanical_pass']=False;result['violations'].append('transport_or_no_asr:'+str(error))
    return dict(scenario=scenario['id'],events=events,tools=tools,operation_metadata=metadata,operation_results=operation_results,
                task=state.snapshot(),error=error,fixture_only=True,fixture_context=scenario.get('fixture',{}),identity_fixture=scenario.get('identity_fixture'),turn_markers=turn_markers,**result)


async def main(args):
    from probe_realtime_tool_protocol import config
    from lira.config import load_config
    from lira.llm.client import LlmClient
    from lira.audio.tts import TtsEngine
    from lira.audio.resample import to_16khz
    import numpy as np
    args.scenarios=str(Path(args.scenarios).resolve())
    args.output=str(Path(args.output).resolve())
    scenarios=load_scenarios(args.scenarios)
    if args.case:scenarios=[s for s in scenarios if s['id'] in args.case]
    if not scenarios:raise ValueError('no matching scenarios')
    if any(s.get('evidence_only') for s in scenarios):raise ValueError('v2 uses formal service/H5 runner, never fixture tools; inspect with --dictation-report')
    sources={}
    root=Path(os.environ.get('LIRA_PROBE_CODE_ROOT',str(Path(__file__).resolve().parents[1])))
    for rel in ['lira/audio/doubao_realtime.py','lira/tv_voice.py','lira/agent_timers.py','lira/realtime_capabilities.py','lira/teaching_content.py','lira/teaching_memory.py','lira/realtime_vision.py','lira/realtime_speech.py']:
        p=root/rel
        if p.exists():sources[rel]=hashlib.sha256(p.read_bytes()).hexdigest()
    pid=subprocess.check_output(['systemctl','--user','show','lira','--property=MainPID','--value'],text=True).strip()
    env=dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)
    os.chdir('/home/orangepi/lira/device');cfg=load_config(env=env)
    llm=LlmClient(cfg.llm,privacy=lambda:False,network_ok=lambda:True)
    rt=NS(_llm=llm,_cfg=cfg,privacy=NS(is_on=False))
    tts=await asyncio.to_thread(TtsEngine);audios={}
    try:
        for scenario in scenarios:
            chunks=[]
            for text in scenario['utterances']:
                samples=await asyncio.to_thread(tts.generate,text)
                pcm=to_16khz(samples,tts.sample_rate);chunks.append(np.pad(pcm,(0,(-len(pcm))%320)))
            audios[scenario['id']]=chunks
    finally:await tts.close()
    from lira.audio.doubao_realtime import ENDPOINT, REALTIME_MODEL
    receipt={'schema':1,'endpoint':ENDPOINT,'model':REALTIME_MODEL,'semantic_verifier_model':cfg.llm.model,
        'source_hashes':sources,'script_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'scenario_hash':hashlib.sha256(Path(args.scenarios).read_bytes()).hexdigest(),
        'repeat':args.repeat,'fixture_only':True,'hardware_verified':False,'rows':[]}
    try:
        for repetition in range(1,args.repeat+1):
            for scenario in scenarios:
                row=await run_case(scenario,audios[scenario['id']],rt,config());row['repeat']=repetition
                receipt['rows'].append(row)
                Path(args.output).write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'scenario':row['scenario'],'repeat':repetition,'mechanical_pass':row['mechanical_pass'],'violations':row['violations']},ensure_ascii=False),flush=True)
    finally:await llm._client.close()
    return all(row['mechanical_pass'] for row in receipt['rows'])


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--scenarios',default=str(Path(__file__).resolve().parents[1]/'tests/agent_scenarios/teaching_capabilities.json'))
    p.add_argument('--case',action='append');p.add_argument('--repeat',type=int,choices=range(1,4),default=3)
    p.add_argument('--output',default='/tmp/lira-agent-evaluation.json')
    p.add_argument('--replay',help='Rejudge saved receipt locally; no network, credentials or model')
    p.add_argument('--dictation-report',help='Inspect saved one-shot v2 report only; no model calls or retries')
    args=p.parse_args()
    if args.dictation_report:
        result=inspect_dictation_report(args.dictation_report)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        sys.exit(0 if result['mechanical_pass'] else 1)
    elif args.replay:
        scenarios={s['id']:s for s in load_scenarios(args.scenarios)}
        receipt=json.loads(Path(args.replay).read_text(encoding='utf-8'))
        print(json.dumps([judge(scenarios[r['scenario']],r['events'],r['tools']) for r in receipt['rows']],ensure_ascii=False,indent=2))
    else:sys.exit(0 if asyncio.run(main(args)) else 1)
