import asyncio
from openai import AsyncOpenAI, APIStatusError
from lira.config import load_config

async def main():
    cfg = load_config().llm
    async with AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url, max_retries=0) as sdk:
        try:
            result = await sdk.post('tools/search_pro', cast_to=dict,
                body={'text_query':'上海 2026年10月14日 天气', 'limit':2, 'timeout_seconds':20},
                options={'timeout':25})
            print('SEARCH_OK', list(result), len(result.get('search_results', [])))
        except APIStatusError as exc:
            print('SEARCH_HTTP', exc.status_code)
        except Exception as exc:
            print('SEARCH_ERROR', type(exc).__name__)
asyncio.run(main())
