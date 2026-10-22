import pytest
from test_dictation_service import service
from test_dictation_transport import event


@pytest.mark.asyncio
async def test_next_context_contains_completed_words_even_before_parent_review(service):
    prepared = await service.prepare_task(
        {'scope_type': 'lesson', 'start': 2, 'proposals': {'2': {'words': ['尽头', '依靠']}}},
        owner_id=1, session_id='history', paired_device_id='tv', callback_registration_id='cb')
    task = prepared['task']
    sequence = 1
    service.repository.receive(event(task, sequence), paired_device_id='tv')
    for phase in task['phases']:
        for item in phase['items']:
            sequence += 1
            service.repository.receive(event(task, sequence, 'item_confirmed', 'waiting_child', phase_id=phase['phase_id'], item_id=item['item_id']), paired_device_id='tv')
    service.repository.receive(event(task, sequence + 1, 'task_completed', 'completed'), paired_device_id='tv')
    assert service.results.get(1, task['task_id'])['status'] == 'unreviewed'
    context = service.get_context({'scope_type': 'lesson', 'start': 2}, owner_id=1, session_id='next-context-only')
    facts = context['lessons'][0]['facts']
    assert facts['word_history'][0]['expanded_words'] == ['尽头', '依靠']
    assert facts['reviews'] == []
    assert service.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0] == 1
