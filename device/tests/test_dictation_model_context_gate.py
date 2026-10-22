import pytest
from pathlib import Path
from test_dictation_service import service

KW = dict(owner_id=1, session_id='model', paired_device_id='tv', callback_registration_id='cb')
ARGS = dict(scope_type='lesson', start=2, proposals={'2': {'words': ['尽头', '依靠']}})

@pytest.mark.asyncio
async def test_model_requires_successful_matching_context(service):
    assert (await service.handle_tool('prepare_dictation_task', ARGS, **KW))['status'] == 'context_required'
    assert not list(service.root.rglob('*.pcm'))
    assert service.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0] == 0
    service.get_context(ARGS, owner_id=1, session_id='model')  # Trusted reads do not count as model exposure.
    assert (await service.handle_tool('prepare_dictation_task', ARGS, **KW))['status'] == 'context_required'
    await service.handle_tool('get_dictation_context', {}, **KW)
    assert (await service.handle_tool('prepare_dictation_task', ARGS, **KW))['status'] == 'context_required'
    context = await service.handle_tool('get_dictation_context', ARGS, **KW)
    assert context['owner_id'] == 1 and context['skill'] and context['lessons'][0]['facts'] is not None
    for override in ({'start': 3}, {'book_id': 'other'}, {'scope_type': 'unit'}, {'unit': 2}):
        assert (await service.handle_tool('prepare_dictation_task', dict(ARGS, **override), **KW))['status'] == 'context_required'
    assert (await service.handle_tool('prepare_dictation_task', ARGS, **KW))['status'] == 'ready'
    service.member_exists=lambda n:n in (1,2)
    service.profiles.member_exists=service.member_exists
    with pytest.raises(ValueError):
        await service.handle_tool('prepare_dictation_task', ARGS, **dict(KW, owner_id=2))
    assert (await service.handle_tool('prepare_dictation_task', ARGS, **dict(KW, owner_id=2, session_id='child-two')))['status']=='context_required'

@pytest.mark.asyncio
async def test_failed_read_revokes_previous_model_context(service):
    await service.handle_tool('get_dictation_context', ARGS, **KW)
    assert (await service.handle_tool('get_dictation_context', {}, **KW))['status']=='needs_input'
    assert (await service.handle_tool('prepare_dictation_task', ARGS, **KW))['status']=='context_required'

@pytest.mark.asyncio
async def test_existing_resource_folder_is_never_deleted(service, monkeypatch):
    original=Path.mkdir
    collision=[]
    def create_collision(path, *args, **kwargs):
        if kwargs.get('exist_ok') is False:
            original(path)
            (path/'unrelated').write_bytes(b'keep')
            collision.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', create_collision)
    with pytest.raises(FileExistsError):
        await service.prepare_task(ARGS, **KW)
    assert (collision[0]/'unrelated').read_bytes()==b'keep'
    assert service.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0]==0

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['pcm', 'manifest', 'submit', 'persist'])
async def test_failed_resource_persistence_cleans_only_new_folder(service, monkeypatch, failure):
    previous = await service.prepare_task(ARGS, **dict(KW, session_id='previous'))
    previous_folder = Path(previous['folder'])
    unrelated = service.root / 'unrelated'
    unrelated.mkdir()
    (unrelated / 'keep').write_bytes(b'keep')
    before = set(service.root.iterdir())
    if failure in ('pcm', 'manifest'):
        method = 'write_bytes' if failure == 'pcm' else 'write_text'
        original = getattr(Path, method)
        def fail(path, *args, **kwargs):
            if path.parent != previous_folder and (path.suffix == '.pcm' if failure == 'pcm' else path.name == 'manifest.json'):
                raise OSError('synthetic write failure')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, method, fail)
    else:
        submit = service.repository.submit
        def fail_submit(*args, **kwargs):
            submit(*args, **kwargs)
            if failure == 'submit':
                raise RuntimeError('synthetic submit failure')
        monkeypatch.setattr(service.repository, 'submit', fail_submit)
        if failure == 'persist':
            service.db.execute("CREATE TRIGGER reject_context BEFORE INSERT ON dictation_preparation_context BEGIN SELECT RAISE(ABORT, 'synthetic persist failure'); END")
    with pytest.raises(Exception):
        await service.prepare_task(ARGS, **KW)
    assert set(service.root.iterdir()) == before
    assert (unrelated / 'keep').read_bytes() == b'keep'
    assert service.db.execute('SELECT count(*) FROM dictation_tasks').fetchone()[0] == 1
    assert service.db.execute('SELECT count(*) FROM dictation_preparation_context').fetchone()[0] == 1
