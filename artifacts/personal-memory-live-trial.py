import asyncio,tempfile
from pathlib import Path
from openai import AsyncOpenAI
from lira.config import load_config
from personal_memory import PersonalMemory
from search_agent import AgentReply
async def main():
    cfg=load_config().llm
    with tempfile.TemporaryDirectory(prefix='lira-memory-check-') as folder:
        async with AsyncOpenAI(base_url=cfg.base_url,api_key=cfg.api_key,max_retries=0) as sdk:
            m=PersonalMemory(Path(folder)/'test.db',sdk,cfg.model,lambda:False)
            member=m.people.create_member('测试成员')['id']
            m.completed(member,'我喜欢简短的回答，查天气只想知道温度和会不会下雨。',AgentReply('好的，我记住了。',True))
            await asyncio.gather(*m.tasks)
            row=m.db.execute('SELECT summary FROM personal_memories WHERE member_id=?',(member,)).fetchone()
            print('SUMMARY_SAVED',bool(row),'WEATHER_COUNT',m.stats(member)['weather_queries_total'])
            await m.close()
asyncio.run(main())
