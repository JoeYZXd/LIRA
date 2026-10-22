"""Finite cloud dictation assets. No fallback, playback, or household context."""
import asyncio
import base64
import copy
import json
import re
import uuid
import httpx

DEFAULT_TTS = dict(provider='doubao',resource_id='seed-tts-2.0',speaker='zh_female_vv_uranus_bigtts',pitch=0,speech_rate=-20,loudness_rate=30,instruction='朗读 播音 拉长音 着重强调 标准普通话')
ENDPOINT='https://openspeech.bytedance.com/api/v3/tts/unidirectional'

def validate_tts(value):
    if type(value) is not dict or set(value)!=set(DEFAULT_TTS):raise ValueError('tts: 参数缺失或包含未知字段')
    if value['provider']!='doubao' or value['resource_id']!='seed-tts-2.0':raise ValueError('tts: 请选择豆包语音合成2.0')
    if type(value['speaker']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value['speaker']):raise ValueError('tts: 音色编号无效')
    for key,lo,hi in [('pitch',-12,12),('speech_rate',-50,100),('loudness_rate',-50,100)]:
        if type(value[key]) is not int or not lo<=value[key]<=hi:raise ValueError('tts: '+key+' 超出范围')
    if type(value['instruction']) is not str or len(value['instruction'])>2000 or any(ord(c)<32 and c not in '\n\t' for c in value['instruction']):raise ValueError('tts: 语音指令无效')
    return copy.deepcopy(value)

async def synthesize_cloud(text,settings,api_key,*,allowed,client=None):
    settings=validate_tts(settings)
    if not api_key:raise ValueError('豆包语音合成 API 密钥未配置')
    def guard():
        if not allowed():raise ValueError('语音合成已撤销')
    guard()
    params=dict(text=text+'。',speaker=settings['speaker'],audio_params=dict(format='pcm',sample_rate=24000,speech_rate=settings['speech_rate'],loudness_rate=settings['loudness_rate']),additions=json.dumps(dict(post_process=dict(pitch=settings['pitch']),context_texts=[settings['instruction']]),ensure_ascii=False))
    async def request(c):
        data=bytearray();terminal=False
        try:
            async with c.stream('POST',ENDPOINT,headers={'X-Api-Key':api_key,'X-Api-Resource-Id':settings['resource_id'],'X-Api-Request-Id':uuid.uuid4().hex},json=dict(user={'uid':'lira-dictation'},req_params=params)) as response:
                if response.status_code!=200:raise ValueError('豆包语音合成 HTTP '+str(response.status_code))
                async for line in response.aiter_lines():
                    guard()
                    if not line:continue
                    if line.startswith('data:'):line=line[5:].strip()
                    row=json.loads(line)
                    if type(row) is not dict or type(row.get('code')) is not int:raise ValueError('豆包语音合成响应格式无效')
                    code=row['code']
                    if code not in (0,20000000):raise ValueError('豆包语音合成错误码 '+str(code))
                    if terminal:raise ValueError('豆包语音合成结束后仍有数据')
                    if row.get('data'):data.extend(base64.b64decode(row['data'],validate=True))
                    if len(data)>24000*2*120:raise ValueError('豆包语音资源过大')
                    if code==20000000:terminal=True
            guard()
            if not terminal or not data or len(data)%2:raise ValueError('豆包语音资源不完整')
            return bytes(data)
        except (httpx.HTTPError,json.JSONDecodeError,TypeError,KeyError) as exc:
            # Transport errors must never expose headers, keys or echoed prompts.
            raise ValueError('豆包语音合成连接或响应失败') from None
    async def run():
        if client is not None:return await request(client)
        async with httpx.AsyncClient(timeout=40,trust_env=False) as c:return await request(c)
    try:return await asyncio.wait_for(run(),45)
    except asyncio.TimeoutError:raise ValueError('豆包语音合成超时') from None
