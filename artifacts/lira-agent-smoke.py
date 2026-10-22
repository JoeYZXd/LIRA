import asyncio
from lira.config import load_config
from lira.llm.client import LlmClient
from lira.llm.prompts import CHAT_SYSTEM_PROMPT

async def main():
    cfg = load_config()
    print('SEARCH_ENABLED', cfg.llm.web_search_enabled)
    llm = LlmClient(cfg.llm)
    try:
        reply = await llm.chat('请联网查询上海明天的天气，用三句话分析是否需要带伞，附来源。',
            system=CHAT_SYSTEM_PROMPT, voice_log=False, on_wait_feedback=None)
        print('LIVE_REPLY', reply)
    except Exception as exc:
        print('LIVE_ERROR', type(exc).__name__)
    finally:
        await llm._client.close()
asyncio.run(main())
