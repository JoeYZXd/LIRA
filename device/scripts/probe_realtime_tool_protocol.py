"""Synthetic board-only proof of actual capability result delivery.

No microphone, speaker, service mutation, or business-tool execution. Credentials
remain in memory; stdout contains only fixed synthetic conversation evidence.
Use --output for a JSON receipt. A separate code overlay can be supplied with
LIRA_PROBE_CODE_ROOT so service source need not be deployed for verification.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get('LIRA_PROBE_CODE_ROOT', str(Path(__file__).resolve().parents[1])))

class SilentPlayer:
    async def open(self): pass
    async def stop(self): pass
    async def close(self): pass
    async def play_stream(self, chunks):
        async for _ in chunks: pass


def config():
    from lira.audio.doubao_realtime import RealtimeConfig
    pid = subprocess.check_output(['systemctl', '--user', 'show', 'lira', '--property=MainPID', '--value'], text=True).strip()
    settings = {}
    for part in Path('/proc/' + pid + '/environ').read_bytes().split(b'\0'):
        if b'=' in part:
            key, value = part.split(b'=', 1)
            if key in (b'LIRA_DOUBAO_REALTIME_API_KEY', b'LIRA_DOUBAO_REALTIME_VOICE'):
                settings[key.decode()] = value.decode()
    return RealtimeConfig(api_key=settings['LIRA_DOUBAO_REALTIME_API_KEY'], voice=settings['LIRA_DOUBAO_REALTIME_VOICE'])


async def run_case(case, audio, marker):
    import numpy as np
    from lira.audio.doubao_realtime import RealtimeSession
    from lira.realtime_capabilities import CapabilityRegistry
    start = time.monotonic()
    events, calls, tools = [], [], []
    entered, completed = asyncio.Event(), asyncio.Event()
    cancelled = False
    completion_at = None
    count = 0

    def event(kind, value):
        if kind not in ('activity',):
            events.append({'at': round(time.monotonic()-start, 3), 'kind': kind, 'text': str(value)[:800]})

    async def query(args):
        nonlocal completion_at, count, cancelled
        count += 1
        calls.append({'at': round(time.monotonic()-start, 3), 'phase': 'started'})
        entered.set()
        try:
            await asyncio.sleep(12)
            completion_at = time.monotonic()-start
            calls.append({'at': round(completion_at, 3), 'phase': 'completed'})
            completed.set()
            return {'status': 'ok', 'probe_result': marker, 'source': 'synthetic_only'}
        except asyncio.CancelledError:
            cancelled = True
            calls.append({'at': round(time.monotonic()-start, 3), 'phase': 'cancelled'})
            raise

    registry = CapabilityRegistry()
    registry.register('query_synthetic_marker',
        '启动一个本地合成标记查询。调用后依真实状态说明等待；结果是合成测试数据，不是设备状态。',
        {'type': 'object', 'properties': {}, 'additionalProperties': False}, query,
        background=case in ('background-barge', 'cancel'), sensitive=True)

    async def no_business_tools(name, args):
        return {'status': 'unavailable', 'reason': 'isolated synthetic probe; no business operations'}

    def diagnostic(direction, kind, data, turn):
        if kind == 'response.function_call_arguments.done':
            for item in data.get('items', []):
                tools.append({'at': round(time.monotonic()-start, 3), 'name': item.get('name'), 'call_id': item.get('call_id')})

    session = RealtimeSession(config=config(), player=SilentPlayer(), on_event=event,
                              on_tool=no_business_tools, capabilities=registry, on_diagnostic=diagnostic)
    feeder = None

    async def speech(samples):
        for i in range(0, len(samples), 1600):
            session.feed(samples[i:i+1600])
            await asyncio.sleep(.1)

    async def silence():
        while True:
            session.feed(np.zeros(1600, np.float32))
            await asyncio.sleep(.1)

    async def play(samples):
        nonlocal feeder
        if feeder:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        await speech(samples)
        feeder = asyncio.create_task(silence())

    try:
        await session.open()
        await play(audio['request'])
        await asyncio.wait_for(entered.wait(), 25)
        if case == 'cancel':
            await play(audio['cancel'])
            await asyncio.sleep(16)
        else:
            if case != 'deferred-no-barge':
                await play(audio['barge'])
            await asyncio.wait_for(completed.wait(), 25)
            await asyncio.sleep(3)
            await play(audio['read'])
            await asyncio.sleep(14)
        replies = [e for e in events if e['kind'] == 'assistant_text']
        premature = [e for e in replies if completion_at is None or e['at'] < completion_at]
        claims = [e for e in premature if marker in e['text'] or re.search(r'(?:结果|标记).{0,4}(?:是|为)[^？?]|设备在线|服务正常|无异常', e['text'])]
        consumed = any(marker in e['text'] for e in replies if completion_at is not None and e['at'] >= completion_at)
        passed = (cancelled and not completed.is_set() and count == 1) if case == 'cancel' else (consumed and not claims and count == 1 and not session.failed)
        return {'case': case, 'passed': passed, 'marker': marker, 'executions': count,
                'completed': completed.is_set(), 'cancelled': cancelled,
                'premature_claims': claims, 'events': events, 'calls': calls, 'tools': tools, 'session_failed': session.failed}
    except Exception as exc:
        return {'case': case, 'passed': False, 'error_type': type(exc).__name__, 'events': events, 'calls': calls}
    finally:
        if feeder:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        await session.close()


async def main(args):
    import numpy as np
    from lira.audio.tts import TtsEngine
    from lira.audio.resample import to_16khz
    texts = {
        'request': '请调用合成标记查询工具，查询结束后告诉我工具返回的测试标记，不要猜。',
        'barge': '另外测试数字是七九三一，请先复述数字，之前的查询继续等待，不要取消。',
        'read': '请查询当前会话里的本地任务状态，读取已经完成的那个测试查询结果，告诉我它的标记。',
        'cancel': '请取消本次合成标记查询，不要再继续这个任务。',
    }
    tts = await asyncio.to_thread(TtsEngine)
    audio = {}
    try:
        for key, text in texts.items():
            samples = await asyncio.to_thread(tts.generate, text)
            pcm = to_16khz(samples, tts.sample_rate)
            audio[key] = np.pad(pcm, (0, (-len(pcm)) % 320))
    finally:
        await tts.close()
    rows = []
    markers = ['青色盒子', '橙色纸船', '紫色风筝']
    for repeat in range(args.repeat):
        for case in args.case:
            row = await run_case(case, audio, markers[repeat % len(markers)])
            row['repeat'] = repeat + 1
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if args.output:
                args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    return all(r['passed'] for r in rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', choices=['deferred-no-barge', 'deferred-barge', 'background-barge', 'cancel'])
    parser.add_argument('--repeat', type=int, choices=range(1, 4), default=1)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    options.case = options.case or ['deferred-no-barge', 'deferred-barge', 'background-barge', 'cancel']
    sys.exit(0 if asyncio.run(main(options)) else 1)
