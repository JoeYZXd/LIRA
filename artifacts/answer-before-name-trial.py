import asyncio
from types import SimpleNamespace
from lira.main import DeviceRuntime
from lira.config import load_config
from lira.llm.client import LlmClient
from lira.llm.prompts import VOICE_ASSISTANT_SYSTEM_PROMPT
async def main():
    llm=LlmClient(load_config().llm)
    llm.memory=object()
    llm.voice_context_provider=lambda: DeviceRuntime.voice_identity_context(SimpleNamespace(speaker_identity=SimpleNamespace(pending={'asked':False})))
    try:
        result=await llm.chat('你是谁？',system=VOICE_ASSISTANT_SYSTEM_PROMPT,on_wait_feedback=None)
        print('TRIAL_REPLY',result)
    except Exception as exc:print('TRIAL_ERROR',type(exc).__name__)
    finally:await llm._client.close()
asyncio.run(main())
