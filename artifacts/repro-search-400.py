import asyncio
from openai import AsyncOpenAI, APIStatusError
from lira.config import load_config
from search_agent import run_search_agent
async def main():
    cfg=load_config().llm
    async with AsyncOpenAI(base_url=cfg.base_url,api_key=cfg.api_key,max_retries=0) as sdk:
        try:
            await run_search_agent(sdk,cfg.model,[{'role':'user','content':'帮我查下最近无锡的天气'}],lambda:None,voice=True,on_progress=lambda _:None)
            print('SUCCESS')
        except APIStatusError as exc:
            body=exc.body if isinstance(exc.body,dict) else {}
            error=body.get('error',body)
            print('HTTP',exc.status_code,'TYPE',error.get('type'),'CODE',error.get('code'))
            # Only recognize protocol categories; never expose raw provider messages.
            msg=str(error.get('message','')).lower()
            for word in ('tool','messages','role','assistant','system','consecutive','immediately','order','reasoning','thinking'):
                if word in msg: print('CATEGORY',word)
asyncio.run(main())
