"""Exercise the actual authenticated OCR route using saved camera bytes + RKNN.

Credentials and state are isolated in memory; no production credential changes.
"""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

from lira.appliances.store import ApplianceStore
from lira.privacy import PassphraseVault, PrivacyState
from lira.settings import DeviceSettings
from lira.ui.app import UiServices, create_app
from lira.ui.dev import DevHandle
from lira.vision.ocr_rknn import RknnOcrEngine

OUT = Path('/home/orangepi/lira/camera-experiments/ocr-ui-integration-20261006')
SOURCE = Path('/home/orangepi/lira/camera-experiments/ocr-live-20261006-150236/capture.jpg')


class ReplayCamera:
    async def capture(self):
        return SOURCE.read_bytes()


class CountDetector:
    def __init__(self, runtime):
        self.runtime, self.calls = runtime, 0

    def inference(self, *args, **kwargs):
        self.calls += 1
        return self.runtime.inference(*args, **kwargs)


async def request(app, path, data, cookie=None):
    messages = []
    consumed = False
    async def receive():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {'type': 'http.request', 'body': json.dumps(data).encode(), 'more_body': False}
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
    headers = [(b'content-type', b'application/json'), (b'host', b'test')]
    if cookie:
        headers.append((b'cookie', cookie))
    await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
               'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
               'query_string': b'', 'root_path': '', 'headers': headers,
               'server': ('test', 80), 'client': ('127.0.0.1', 12345)}, receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return start, body


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    store = ApplianceStore(':memory:')
    privacy, settings = PrivacyState(), DeviceSettings()
    vault = PassphraseVault(store)
    vault.set('isolated-route-test')
    ocr = RknnOcrEngine()
    detector = CountDetector(ocr._det)
    ocr._det = detector
    runtime = SimpleNamespace(store=store, privacy=privacy, vault=vault,
                              settings=settings, camera=ReplayCamera(), ocr=ocr)
    app = create_app(UiServices(privacy=privacy, vault=vault, store=store,
                               settings=settings, dev=DevHandle(runtime)))
    try:
        login, _ = await request(app, '/dev/login', {'passphrase': 'isolated-route-test'})
        assert login['status'] == 200
        cookie = b'; '.join(v.split(b';', 1)[0] for k, v in login['headers'] if k == b'set-cookie')
        response, body = await request(app, '/dev/api/vision', {}, cookie)
        assert response['status'] == 200, body
        if response['status'] == 200:
            result = json.loads(body)
            preview = base64.b64decode(result.pop('image').split(',', 1)[1])
            (OUT / 'preview.jpg').write_bytes(preview)
            (OUT / 'text.txt').write_text(result['text'], encoding='utf-8')
            result.update(status=response['status'], detector_calls=detector.calls,
                          capture_source=str(SOURCE), source_kind='saved live camera JPEG replay')
            assert detector.calls > 1, 'UI must use tiled detector'
            assert '李一婧设计' in result['text'], 'Small credit missing from UI route'
            (OUT / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        detector.runtime.release()
        ocr._rec.release()


asyncio.run(main())
