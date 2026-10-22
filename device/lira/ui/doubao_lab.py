"""Bounded, in-memory, multi-turn synthetic-audio lab with exportable feedback."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import json
import time
import uuid
import wave
import zipfile

import numpy as np

from lira.audio.doubao_realtime import RealtimeSession, RealtimeError, REALTIME_MODEL, REALTIME_INSTRUCTIONS, TOOLS
from lira.audio.resample import to_16khz

ISSUES={'none','recognition','reply','tool','latency','voice','other'}
MAX_TURNS=20
MAX_TEXT=200
SESSION_SECONDS=180
IDLE_SECONDS=45
MAX_AUDIO_BYTES=8*1024*1024


class _CapturePlayer:
    def __init__(self, lab): self.lab=lab
    async def open(self): pass
    async def stop(self): pass
    async def close(self): pass
    async def play_stream(self, chunks):
        turn=self.lab._current
        async for chunk in chunks:
            if turn is None:
                continue
            if turn['_audio_bytes']+len(chunk)>24000*2*30:
                raise RuntimeError('回复音频过长，本轮已停止。')
            turn['_first_audio']=turn['_first_audio'] or time.monotonic()
            turn['_audio'].append(chunk)
            turn['_audio_bytes']+=len(chunk)


class DoubaoLab:
    def __init__(self, runtime, mutex, *, session_factory=RealtimeSession):
        self.rt, self.mutex, self.factory=runtime,mutex,session_factory
        self.id=uuid.uuid4().hex
        self.turns=[]
        self.state='idle'
        self.error=''
        self.session=None
        self._owns=False
        self._current=None
        self._turn_task=None
        self._watch_task=None
        self._feeding=None
        self._done=asyncio.Event()
        self._last_activity=0
        self._opened=0
        self._run_id=None
        self._operation=asyncio.Lock()
        self.rt.privacy.subscribe(self.apply_privacy)

    def guard(self):
        if self.rt.privacy.is_on:
            raise PermissionError('隐私模式开启，测试记录已清空。')

    def snapshot(self):
        self.guard()
        paths=getattr(self.rt,'conversation_paths',None)
        return {'id':self.id,'state':self.state,'error':self.error,
                'configured':bool(paths and paths.config.ready),'simulation':True,
                'busy':self._turn_task is not None and not self._turn_task.done(),
                'limits':{'turns':MAX_TURNS,'text_chars':MAX_TEXT,'session_seconds':SESSION_SECONDS,'idle_seconds':IDLE_SECONDS},
                'turns':[self._public(t) for t in self.turns]}

    @staticmethod
    def _public(turn):
        data={k:v for k,v in turn.items() if not k.startswith('_')}
        data['audio_available']=bool(turn['_audio'])
        data['audio_filename']=turn['id']+'.wav' if turn['_audio'] else None
        return data

    async def start(self):
        self.guard()
        async with self._operation:
            if self.session is not None:
                return self.snapshot()
            paths=getattr(self.rt,'conversation_paths',None)
            if paths is None or not paths.config.ready:
                raise ValueError('请先配置豆包实时语音密钥和音色。')
            if self.mutex.locked() or self.rt._mic_paused:
                raise RuntimeError('设备正被其他测试占用，请先结束该测试。')
            await self.mutex.acquire()
            self._owns=True
            self.state='connecting'
            self.error=''
            session=None
            try:
                await paths.stop()
                self.guard()
                paths.external_test=True
                session=self.factory(config=paths.config,player=_CapturePlayer(self),
                    on_event=self._event,on_tool=self._tool,
                    speed=self.rt.settings.tts_speed,volume=1,
                    usage=getattr(paths,'usage',None),usage_source='test')
                await session.open()
                self.guard()
                await session.send('input_audio_mute.commit')
                self.session=session
                self.state='ready'
                self._run_id=uuid.uuid4().hex
                self._opened=self._last_activity=time.monotonic()
                self._watch_task=asyncio.create_task(self._watch())
            except BaseException:
                try:
                    if session is not None:await session.close()
                finally:self._release()
                self.state='idle'
                raise
        return self.snapshot()

    def _release(self):
        paths=getattr(self.rt,'conversation_paths',None)
        if paths is not None:paths.external_test=False
        if self._owns:
            self._owns=False
            self.mutex.release()

    async def submit(self,text,request_id):
        self.guard()
        if not isinstance(text,str) or not 1<=len(text.strip())<=MAX_TEXT:
            raise ValueError('请输入 1～200 字的测试内容。')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=80:
            raise ValueError('请求标识无效。')
        if any(t['request_id']==request_id for t in self.turns):return self.snapshot()
        if self.session is None or self.state!='ready':
            raise RuntimeError('请先开始对话，或等待当前回复结束。')
        if len(self.turns)>=MAX_TURNS:
            raise ValueError('已达到 20 轮，请先导出记录，再清空开始。')
        turn={'id':uuid.uuid4().hex,'request_id':request_id,'session':self._run_id,
              'created_at':datetime.now(timezone.utc).isoformat(),'input_text':text.strip(),
              'transcript':'','reply':'','state':'preparing','tools':[],
              'feedback':{'category':'none','note':'','expected':''},'metrics':{},'error':'',
              '_audio':[],'_audio_bytes':0,'_first_audio':None,'_started':time.monotonic()}
        self.turns.append(turn)
        self._current=turn
        self._done=asyncio.Event()
        self.state='busy'
        self._last_activity=time.monotonic()
        self._turn_task=asyncio.create_task(self._run_turn(turn))
        return self.snapshot()

    async def _run_turn(self,turn):
        session=self.session
        async def feed(audio):
            next_tick=time.monotonic()
            for offset in range(0,len(audio),1600):
                self.guard()
                if self._done.is_set():return
                session.feed(audio[offset:offset+1600])
                if offset+1600>=len(audio):
                    turn['_input_end']=time.monotonic()+len(audio[offset:offset+1600])/16000
                next_tick+=.1
                await asyncio.sleep(max(0,next_tick-time.monotonic()))
            turn['state']='waiting'
            while not self._done.is_set():
                self.guard()
                session.feed(np.zeros(1600,np.float32))
                await asyncio.sleep(.1)
        try:
            prepared=time.monotonic()
            generated=await asyncio.to_thread(self.rt.tts.generate,turn['input_text'],self.rt.settings.tts_speed)
            self.guard()
            audio=to_16khz(generated,self.rt.tts.sample_rate)
            if not 0<len(audio)<=16000*30 or not np.isfinite(audio).all():
                raise ValueError('输入语音超过 30 秒或无法合成，请缩短文字。')
            audio=np.pad(audio,(0,(-len(audio))%320))
            turn['metrics'].update(tts_ms=(time.monotonic()-prepared)*1000,input_audio_ms=len(audio)/16)
            await session.send('input_audio_unmute.commit')
            turn['state']='sending'
            self._feeding=asyncio.create_task(feed(audio))
            await asyncio.wait_for(self._done.wait(),70)
            if turn['error']:raise RealtimeError(turn['error'])
            playback=getattr(session,'playback',None)
            if playback is not None:await asyncio.wait_for(asyncio.shield(playback),5)
            turn['state']='completed'
        except asyncio.CancelledError:
            turn['state']='cancelled'
            raise
        except Exception as exc:
            turn['state']='failed'
            turn['error']=str(exc) if isinstance(exc,(RealtimeError,ValueError,PermissionError)) else '本轮交互失败，请结束后重新连接。'
            self.error=turn['error']
        finally:
            if self._feeding is not None:
                self._feeding.cancel()
                await asyncio.gather(self._feeding,return_exceptions=True)
                self._feeding=None
            try:
                if self.session is session and turn['state']!='cancelled' and not self.rt.privacy.is_on:
                    if hasattr(session,'drain_input'):await session.drain_input()
                    await session.send('input_audio_mute.commit')
            except Exception:
                self.error='连接已中断，请结束后重新开始。'
            first,end=turn['_first_audio'],turn.get('_input_end')
            turn['metrics'].update(total_ms=(time.monotonic()-turn['_started'])*1000,
                response_wait_ms=None if first is None or end is None else (first-end)*1000,
                reply_audio_ms=turn['_audio_bytes']/48)
            self._trim_audio()
            self._last_activity=time.monotonic()
            self.state='ready' if not self.error and self.session is session else 'error'
            self._current=None

    def _trim_audio(self):
        total=sum(t['_audio_bytes'] for t in self.turns)
        for turn in self.turns:
            if total<=MAX_AUDIO_BYTES:break
            total-=turn['_audio_bytes']
            turn['_audio']=[]
            turn['_audio_bytes']=0

    def _event(self,kind,value):
        if kind=='error':
            self.error=value
            self.state='error'
        turn=self._current
        if turn is None:return
        if kind=='user_text':turn['transcript']=value[:2000]
        elif kind=='assistant_text':turn['reply']=value[:2000]
        elif kind=='response_done':self._done.set()
        elif kind=='error':
            turn['error']=value
            self._done.set()

    async def _tool(self,name,args):
        self.guard()
        result={'status':'simulated','reason':'仅模拟工具，没有实际执行，没有发送或学习红外码，也没有录制或保存声纹。'}
        if name=='get_device_status':
            result={'status':'ok','test_mode':True,'voice_enrollment_available':self.rt.voice_enrollment is not None} if hasattr(self.rt,'voice_enrollment') else {'status':'ok','test_mode':True}
        if self._current is not None:
            if len(self._current['tools'])>=16:raise ValueError('工具调用过多。')
            self._current['tools'].append({'name':name,'arguments':args,'result':result})
        return result

    def feedback(self,turn_id,category,note,expected):
        self.guard()
        if not isinstance(category,str) or category not in ISSUES or not isinstance(note,str) or len(note)>3000 or not isinstance(expected,str) or len(expected)>2000:
            raise ValueError('反馈类型或内容无效。')
        turn=next((t for t in self.turns if t['id']==turn_id),None)
        if turn is None:raise ValueError('未找到这一轮。')
        turn['feedback']={'category':category,'note':note,'expected':expected}
        return self.snapshot()

    def export(self,format):
        self.guard()
        data=self.snapshot()
        data.update(schema='lira-doubao-lab/v1',exported_at=datetime.now(timezone.utc).isoformat(),
            transport='text -> local TTS -> 16kHz realtime audio',tool_execution='simulated',
            configuration={'model':REALTIME_MODEL,'instructions':REALTIME_INSTRUCTIONS,'tools':TOOLS,
                'voice':getattr(getattr(self.rt,'conversation_paths',None),'config',None).voice,
                'input_sample_rate':16000,'output_sample_rate':24000},
            timing_note='response_wait_ms 从最后一块音频提交窗口结束近似计时；total_ms 包含输入 TTS 与实时送音，不是扬声器实测。')
        if format=='json':return json.dumps(data,ensure_ascii=False,indent=2)
        if format!='markdown':raise ValueError('不支持的导出格式。')
        lines=['# LIRA 豆包交互测试记录',f"记录 ID：{self.id}",'工具为模拟执行，没有登记声纹。',
            '配置：'+json.dumps(data['configuration'],ensure_ascii=False),
            data['timing_note'],'']
        for i,turn in enumerate(data['turns'],1):
            lines.extend([f"## 第 {i} 轮 · {turn['id']}",f"会话：{turn['session']}",
                f"输入：{turn['input_text']}",f"转写：{turn['transcript']}",f"回复：{turn['reply']}",
                f"状态：{turn['state']}；错误：{turn['error']}",
                '工具：'+json.dumps(turn['tools'],ensure_ascii=False),
                '计时：'+json.dumps(turn['metrics'],ensure_ascii=False),
                f"问题类型：{turn['feedback']['category']}",f"问题说明：{turn['feedback']['note']}",
                f"期望表现：{turn['feedback']['expected']}",''])
        return '\n\n'.join(lines)

    def audio(self,turn_id):
        self.guard()
        turn=next((t for t in self.turns if t['id']==turn_id),None)
        if turn is None or not turn['_audio']:raise ValueError('音频已释放或尚未生成。')
        output=io.BytesIO()
        with wave.open(output,'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(24000)
            stream.writeframes(b''.join(turn['_audio']))
        return output.getvalue()

    def bundle(self):
        self.guard()
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('conversation.json',self.export('json'))
            archive.writestr('conversation.md',self.export('markdown'))
            for turn in self.turns:
                if turn['_audio']:archive.writestr(turn['id']+'.wav',self.audio(turn['id']))
        return output.getvalue()

    async def _watch(self):
        while self.session is not None:
            await asyncio.sleep(1)
            if self.state=='error' or time.monotonic()-self._opened>=SESSION_SECONDS or (self.state=='ready' and time.monotonic()-self._last_activity>=IDLE_SECONDS):
                await self.stop()
                return

    async def stop(self):
        async with self._operation:
            tasks=[t for t in (self._watch_task,self._turn_task) if t is not None and t is not asyncio.current_task()]
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            session,self.session=self.session,None
            try:
                if session is not None:await session.close()
            finally:
                self._release()
                self.state='ended'

    async def clear(self):
        await self.stop()
        self.turns=[]
        self.id=uuid.uuid4().hex
        self.error=''

    async def apply_privacy(self,enabled):
        if enabled:await self.clear()

    async def close(self):await self.stop()
