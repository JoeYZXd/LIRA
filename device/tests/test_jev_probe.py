"""Real probe state/TTS path with controlled decisions and numeric HAL events."""
import asyncio
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from lira.audio.voice_metrics import current_trace, mark
from lira.privacy import PrivacyState
from lira.ui.jev_probe import CUES, JevProbe, ProbeError, TypeSafeDecision
from test_audio_stack import playback_engine
from test_dev_console import make_client, login


class Player:
    def __init__(self):
        self.played = []

    async def play_stream(self, source):
        trace = current_trace.get()
        async for pcm in source:
            self.played.append(pcm)
            mark(trace, 'output_consumed')
            await asyncio.sleep(.005)
        mark(trace, 'output_drained')

    async def stop(self):
        pass

    async def close(self):
        pass


class Decision:
    configured = True

    def __init__(self):
        self.calls = []
        self.release = None

    async def choose(self, text, phase):
        self.calls.append((text, phase))
        if self.release:
            await self.release.wait()
        return {'choice': 'cancel' if '不录' in text else 'start', 'confidence': .9,
                'probabilities': {}, 'model': 'test-only'}


def setup_probe():
    player, generated = Player(), []

    def generate(text, speed):
        generated.append(text)
        return np.ones(80, dtype=np.float32) * .1

    tts = playback_engine(player, generate)
    rt = SimpleNamespace(tts=tts, privacy=PrivacyState(),
                         settings=SimpleNamespace(tts_speed=1., volume=.5))
    decision = Decision()
    probe = JevProbe(rt, asyncio.Lock(), decision)
    return probe, player, generated, decision


async def prepared():
    probe, player, generated, decision = setup_probe()
    await probe.prepare()
    assert len(generated) == len(CUES)
    assert not player.played
    return probe, player, generated, decision


async def test_cached_play_reuses_final_snapshot_and_reports_real_driver_stages():
    p, player, generated, decision = await prepared()
    r = await p.start('idle')
    p.submit(r['id'], 1, '帮我录入声纹')
    await p.run['task']
    assert not player.played
    p.finish(r['id'], 1, '帮我录入声纹')
    await p.play_task
    result = p.status()['run']
    assert result['state'] == 'complete'
    assert result['reused']
    assert result['total_ms'] >= result['playback_ms'] >= 0
    assert result['response_ms'] >= 0
    assert len(generated) == len(CUES)  # no TTS inference on the reply path
    assert len(decision.calls) == 1
    assert player.played and not p.mutex.locked()
    await p.runtime.tts.close()


async def test_final_correction_invalidates_previous_decision_and_duplicate_finish():
    p, player, _, _ = await prepared()
    r = await p.start('idle')
    p.submit(r['id'], 1, '帮我录入')
    await p.run['task']
    p.finish(r['id'], 2, '帮我录入，不过今天先不录')
    with pytest.raises(ProbeError):
        p.finish(r['id'], 2, '帮我录入，不过今天先不录')
    await p.play_task
    assert p.status()['run']['result']['choice'] == 'cancel'
    assert len(player.played) == 1
    assert not p.status()['run']['reused']
    await p.runtime.tts.close()


async def test_stale_partial_and_revised_same_number_are_rejected():
    p, _, _, _ = await prepared()
    r = await p.start('idle')
    p.submit(r['id'], 5, '录入')
    with pytest.raises(ProbeError):
        p.submit(r['id'], 4, '旧文本')
    with pytest.raises(ProbeError):
        p.submit(r['id'], 5, '另一文本')
    await p.cancel(r['id'])
    assert not p.mutex.locked()
    await p.runtime.tts.close()


async def test_cancel_and_privacy_prevent_late_playback():
    p, player, _, d = await prepared()
    d.release = asyncio.Event()
    r = await p.start('idle')
    p.finish(r['id'], 1, '录入')
    await asyncio.sleep(0)
    await p.cancel(r['id'])
    d.release.set()
    await asyncio.sleep(.01)
    assert not player.played and not p.mutex.locked()
    r = await p.start('idle')
    p.submit(r['id'], 1, '录入')
    await p.runtime.privacy.set_enabled(True, source='test')
    assert p.run is None and not p.mutex.locked()
    assert not player.played
    await p.runtime.tts.close()


async def test_failed_decision_does_not_play_or_report_zero_timings():
    p, player, _, d = await prepared()

    async def fail(*args):
        raise RuntimeError('secret provider payload must not appear')

    d.choose = fail
    r = await p.start('idle')
    p.finish(r['id'], 1, '录入')
    await p.play_task
    result = p.status()['run']
    assert result['state'] == 'failed'
    assert result['response_ms'] is None and result['total_ms'] is None
    assert 'secret' not in str(result)
    assert not player.played and not p.mutex.locked()
    await p.runtime.tts.close()


async def test_speed_change_requires_rewarm_and_active_test_holds_mutex():
    p, _, _, _ = await prepared()
    r = await p.start('idle')
    with pytest.raises(ProbeError):
        await p.start('idle')
    with pytest.raises(ProbeError):
        await p.prepare()
    await p.cancel(r['id'])
    p.runtime.settings.tts_speed = 1.2
    assert not p.ready
    with pytest.raises(ProbeError):
        await p.start('idle')
    await p.runtime.tts.close()


async def test_missing_key_is_explicit(monkeypatch):
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    with pytest.raises(ProbeError, match='TYPESAFE_API_KEY'):
        await TypeSafeDecision().choose('录入', 'idle')


async def test_typesafe_protocol_validation_with_real_http_client(monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-key')
    real_client = httpx.AsyncClient

    def respond(request):
        import json
        payload = json.loads(request.content)
        assert payload['state']['user_text'] == '取消'
        assert set(payload['questions']['cue']['criteria']) == {'start', 'cancel', 'clarify'}
        return httpx.Response(200, json={'model': 'test-model', 'answers': {'cue': {
            'choice': 'cancel', 'confidence': .9,
            'probabilities': {'start': .05, 'cancel': .9, 'clarify': .05}}}})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond)))
    decision = TypeSafeDecision()
    assert (await decision.choose('取消', 'idle'))['choice'] == 'cancel'
    first_client = decision.client
    await decision.choose('取消', 'idle')
    assert decision.client is first_client
    await decision.close()


async def test_cancel_during_real_tts_output_stops_and_releases_test():
    p, player, _, _ = await prepared()
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_output(source):
        async for pcm in source:
            player.played.append(pcm)
            entered.set()
            await release.wait()

    async def stop():
        release.set()

    player.play_stream, player.stop = held_output, stop
    r = await p.start('idle')
    p.finish(r['id'], 1, '录入')
    await asyncio.wait_for(entered.wait(), 1)
    await p.cancel(r['id'])
    assert p.run['state'] == 'cancelled' and not p.mutex.locked()
    assert p.status()['run']['total_ms'] is None
    await p.runtime.tts.close()


async def test_cancel_before_play_task_first_runs_releases_mutex():
    p, player, _, _ = await prepared()
    r = await p.start('idle')
    p.finish(r['id'], 1, '录入')
    await p.cancel(r['id'])  # deliberately no scheduling yield before cancellation
    assert not p.mutex.locked() and not player.played
    r = await p.start('idle')
    await p.cancel(r['id'])
    await p.runtime.tts.close()


async def test_old_cleanup_does_not_cancel_successor_expiry():
    p, _, _, _ = await prepared()
    r = await p.start('idle')
    old = p.run
    await p.cancel(r['id'])
    r = await p.start('idle')
    expiry = p.expiry
    p._release(old)
    assert expiry.cancelling() == 0 and p.mutex.locked()
    await p.cancel(r['id'])
    await p.runtime.tts.close()


@pytest.mark.parametrize('answer', [
    {'choice': 'unknown', 'confidence': .9, 'probabilities': {'start': .05, 'cancel': .9, 'clarify': .05}},
    {'choice': 'cancel', 'confidence': 2, 'probabilities': {'start': .05, 'cancel': .9, 'clarify': .05}},
    {'choice': 'start', 'confidence': .9, 'probabilities': {'start': .05, 'cancel': .9, 'clarify': .05}},
    {'choice': 'cancel', 'confidence': .9, 'probabilities': {'cancel': 1}},
])
async def test_invalid_provider_choice_never_passes_validation(monkeypatch, answer):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-key')
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={'model': 'test-only', 'answers': {'cue': answer}}))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=transport))
    decision = TypeSafeDecision()
    with pytest.raises(ProbeError):
        await decision.choose('取消', 'idle')
    await decision.close()


def test_probe_route_auth_and_private_mode():
    _, client, rt = make_client()
    assert client.get('/dev/api/jev-probe').status_code == 401
    assert client.post('/dev/api/jev-probe', json={'action': 'start'}).status_code == 401
    login(client)
    assert client.get('/dev/api/jev-probe').status_code == 200
    assert client.post('/dev/api/jev-probe', json={'action': 'unknown'}).status_code == 422
    asyncio.run(rt.privacy.set_enabled(True, source='test'))
    assert client.get('/dev/api/jev-probe').status_code == 403
    assert client.post('/dev/api/jev-probe', json={'action': 'prepare'}).status_code == 403


async def test_region_denial_ends_typing_run_and_prevents_more_submissions():
    p, player, _, d = await prepared()

    async def denied(*args):
        raise ProbeError("TypeSafe 当前地区不可用（HTTP 451）。", 451)

    d.choose = denied
    r = await p.start('idle')
    p.submit(r['id'], 1, '帮我录入声纹')
    await p.run['task']
    result = p.status()['run']
    assert result['state'] == 'failed'
    assert '451' in result['error']
    assert not p.mutex.locked() and not player.played
    with pytest.raises(ProbeError):
        p.submit(r['id'], 2, '再试一次')
    await p.runtime.tts.close()


async def test_http_451_is_reported_as_region_denial(monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-key')
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(
        451, json={'title': 'Typesafe is not available in your region.', 'status': 451}))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=transport))
    decision = TypeSafeDecision()
    try:
        with pytest.raises(ProbeError, match='地区') as exc:
            await decision.choose('录入', 'idle')
        assert exc.value.status == 451
    finally:
        await decision.close()


async def test_typesafe_uses_explicit_proxy_and_reuses_connection(monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-key')
    monkeypatch.setenv('TYPESAFE_PROXY', 'http://192.168.101.24:7897')
    real_client = httpx.AsyncClient
    options = []
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'model': 'test-only', 'answers': {'cue': {'choice': 'start', 'confidence': .9,
        'probabilities': {'start': .9, 'cancel': .05, 'clarify': .05}}}}))

    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=transport)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    decision = TypeSafeDecision()
    try:
        await decision.choose('录入', 'idle')
        await decision.choose('录入', 'idle')
        assert len(options) == 1
        assert options[0]['proxy'] == 'http://192.168.101.24:7897'
        assert options[0]['trust_env'] is False
    finally:
        await decision.close()
