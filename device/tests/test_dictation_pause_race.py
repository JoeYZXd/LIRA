import json
import httpx
import pytest
from lira.dictation_tools import DictationCoordinator
from test_dictation_service import service
from test_dictation_transport import event


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['audio_progress', 'other_item', 'pause_applied'])
async def test_pause_refreshes_audio_revision_once_without_following_another_item(service, race):
    result = await service.prepare_task({'scope_type': 'lesson', 'start': 2, 'proposals': {'2': {'words': ['尽头', '依靠']}}}, owner_id=1, session_id='pause-race', paired_device_id='tv', callback_registration_id='cb')
    task = result['task']; phase = task['phases'][1]; item = phase['items'][0]
    def row(n, action, state, **extra):
        return event(task, n, action, state, phase_id=phase['phase_id'], item_id=item['item_id'], **extra)
    outbox = [row(1, 'playback_started', 'playing')]; commands = []
    def receive(request):
        body = json.loads(request.content)
        if request.url.path.endswith('/events'):
            return httpx.Response(200, json={'events': [e for e in outbox if e['sequence'] > body['after']]})
        if request.url.path.endswith('/control'):
            commands.append(body)
            if len(commands) == 1:
                assert body['expected_state_revision'] == 2  # refreshed before send
                next_event = row(3, 'waiting_child', 'waiting_child')
                if race == 'other_item': next_event['item_id'] = phase['items'][1]['item_id']
                if race == 'pause_applied': next_event = row(3, 'paused', 'paused', command_id=body['command_id'])
                outbox.append(next_event)
                return httpx.Response(409, json={'error': 'stale revision'})
            assert body['expected_state_revision'] == 3
            outbox.append(row(4, 'paused', 'paused', command_id=body['command_id']))
        return httpx.Response(200, json={'ok': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        transport = service.attach_transport(client, base='http://tv', token='test', device_id='tv')
        await transport.poll(task['task_id'])
        coordinator = DictationCoordinator(service, lambda: 1); coordinator.bind(task['task_id'])
        coordinator.observe_user('刚才是哪个字？', turn=1)
        outbox.append(row(2, 'playback_completed', 'playing', actual_repeats=1))
        result = await coordinator.control('pause')
    assert result['status'] == ('context_changed' if race == 'other_item' else 'applied')
    assert len(commands) == (2 if race == 'audio_progress' else 1)
    assert service.repository.request_command(commands[0])['status'] == ('applied' if race == 'pause_applied' else 'rejected')
    assert coordinator.allow_output() is (race != 'other_item')
    assert sum(e['action'] == 'paused' for e in service.repository.events(task['task_id'])) == (0 if race == 'other_item' else 1)
