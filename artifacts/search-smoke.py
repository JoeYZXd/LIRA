import asyncio
from openai import AsyncOpenAI
from lira.config import load_config
from search_agent import run_search_agent

async def main():
    cfg = load_config().llm
    async with AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url, max_retries=0) as sdk:
        original_post = sdk.post
        async def counted_post(*args, **kwargs):
            result = await original_post(*args, **kwargs)
            if isinstance(result, dict):
                print('ACTUAL_SEARCH_RESULTS', len(result.get('search_results', [])))
            return result
        sdk.post = counted_post
        try:
            answer = await asyncio.wait_for(run_search_agent(sdk, cfg.model,
                [{'role':'user','content':'请联网查询上海明天的天气，分析是否适合老人户外散步，并附来源。'}],
                lambda: None, voice=False), 90)
            print('AGENT_ANSWER', answer)
        except Exception as exc:
            print('AGENT_ERROR', type(exc).__name__)
            import traceback
            for frame in traceback.extract_tb(exc.__traceback__):
                print('FRAME', frame.name, frame.lineno)
asyncio.run(main())
