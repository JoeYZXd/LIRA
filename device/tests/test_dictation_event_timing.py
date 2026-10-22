import pytest
from lira.dictation_repository import RepositoryError
from test_dictation_transport import fixture, event


def test_boot_scoped_monotonic_facts_are_optional_and_validated():
    _, repository, task = fixture()
    repository.receive(event(task, 1, elapsed_ms=50), paired_device_id='tv')
    assert repository.status(task['task_id'])['elapsed_ms'] == 50
    for invalid in (-1, True, 49):
        with pytest.raises(RepositoryError):
            repository.receive(event(task, 2, 'paused', 'paused', elapsed_ms=invalid), paired_device_id='tv')
    repository.receive(event(task, 2, 'paused', 'paused', elapsed_ms=51), paired_device_id='tv')
    repository.receive(event(task, 3, 'paused', 'paused', boot_id='new-boot', elapsed_ms=0), paired_device_id='tv')
    assert repository.events(task['task_id'])[-1]['elapsed_ms'] == 0
    # Existing v2 receivers can omit timing; absence remains unknown.
    repository.receive(event(task, 4, 'paused', 'paused', boot_id='new-boot'), paired_device_id='tv')
    assert 'elapsed_ms' not in repository.status(task['task_id'])
