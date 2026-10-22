import pytest
from lira.dictation_tools import DictationTools
from test_dictation_dialogue import Service


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unavailable', 'completed', 'live'])
async def test_worker_renews_lease_only_after_receipts_confirm_live_task(monkeypatch, outcome):
    service=Service()
    tools=DictationTools(service,lambda:1,lambda:'lease-test')
    tools.coordinator.bind('t')
    calls=[]
    class Transport:
        def status(self, task_id):return dict(delivery_state='received')
        async def poll(self, task_id):
            calls.append('poll')
            if outcome=='unavailable':
                tools.coordinator.closed=True
                raise OSError('synthetic receipt channel failure')
            if outcome=='completed':service.repository.value['state']='completed'
        async def lease(self, task_id, lease_ms):
            calls.append('lease')
            tools.coordinator.closed=True
    service.transport=Transport()
    async def no_wait(_):pass
    monkeypatch.setattr('lira.dictation_tools.asyncio.sleep',no_wait)
    await tools._run()
    assert calls==(['poll','lease'] if outcome=='live' else ['poll'])
