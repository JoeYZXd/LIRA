import asyncio,time
from lira.config import load_config
from lira.llm.client import LlmClient
from lira.llm.prompts import VOICE_ASSISTANT_SYSTEM_PROMPT
async def main():
    llm=LlmClient(load_config().llm)
    start=time.monotonic(); chunks=[]
    def sentence(text):
        chunks.append(text)
        print('SENTENCE_READY',round(time.monotonic()-start,2),'chars',len(text),flush=True)
    try:
        answer=await llm.chat('请简单解释一下为什么天空是蓝色的，用三句话就好。',
            system=VOICE_ASSISTANT_SYSTEM_PROMPT,on_reply_chunk=sentence,on_wait_feedback=None)
        print('COMPLETE',round(time.monotonic()-start,2),'CHUNKS',len(chunks),'MATCH',answer==''.join(chunks))
    except Exception as exc:
        print('ERROR',type(exc).__name__)
    finally:
        await llm._client.close()
asyncio.run(main())
