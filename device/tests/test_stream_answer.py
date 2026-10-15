from types import SimpleNamespace
import asyncio
import pytest
from lira.llm.search_agent import stream_answer

async def test_sentence_delivered_before_stream_finishes_and_no_duplicate_text():
    seen=[]
    class Stream:
        closed=False
        def __aiter__(self):
            return self.iterate()
        async def iterate(self):
            for text in ['哦，','查到了。','今天最高二十度，最低十二度。','出门记得带伞。']:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])
            assert seen
        async def close(self):
            self.closed=True
    stream=Stream()
    async def create(**kwargs):
        assert kwargs['stream'] and kwargs['tool_choice']=='none'
        return stream
    sdk=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    text=await stream_answer(sdk,{},lambda:None,seen.append)
    assert ''.join(seen)==text
    assert len(seen[0]) >= 12
    assert stream.closed

async def test_cancel_closes_stream():
    entered=asyncio.Event()
    class Stream:
        closed=False
        def __aiter__(self): return self
        async def __anext__(self):
            entered.set()
            await asyncio.Event().wait()
        async def close(self): self.closed=True
    stream=Stream()
    async def create(**kwargs): return stream
    sdk=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    task=asyncio.create_task(stream_answer(sdk,{},lambda:None,lambda _:None))
    await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert stream.closed

async def test_streamed_reply_is_not_spoken_again_by_dialog():
    from test_state_machine import Recorder, T0
    from lira.dialog import DialogEngine,Router
    recorder=Recorder()
    async def handler(text, *, on_reply_chunk=None, **kwargs):
        on_reply_chunk('今天最高二十度。')
        on_reply_chunk('出门记得带伞。')
        return '今天最高二十度。出门记得带伞。'
    engine=DialogEngine(Router(remote_llm=handler,remote_available=lambda:True),recorder)
    await engine.on_wake()
    await engine.on_asr_text('天气怎么样')
    assert recorder.spoken.count('今天最高二十度。')==1
    assert '今天最高二十度。出门记得带伞。' not in recorder.spoken

async def test_on_delta_observes_every_chunk_without_changing_sentences():
    """U3/KTD7：on_delta 是纯旁路——观察到全部非空增量，分句（on_sentence）
    次数与内容与不加观测时完全一致。"""
    CHUNKS = ['哦，','查到了。','今天最高二十度，最低十二度。','出门记得带伞。']

    def make_sdk():
        class Stream:
            closed=False
            def __aiter__(self):
                return self.iterate()
            async def iterate(self):
                for text in CHUNKS:
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])
                assert seen
            async def close(self):
                self.closed=True
        seen=[]
        async def create(**kwargs):
            return Stream()
        sdk=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return sdk,seen

    sdk_a,seen_plain=make_sdk()
    text_plain=await stream_answer(sdk_a,{},lambda:None,seen_plain.append)

    sdk_b,seen_obs=make_sdk()
    deltas=[]
    text_obs=await stream_answer(sdk_b,{},lambda:None,seen_obs.append,deltas.append)

    assert seen_obs==seen_plain                      # 分句零变化
    assert text_plain==text_obs
    assert deltas==CHUNKS                            # 每个非空增量恰好观察一次
    assert ''.join(deltas)==text_obs
