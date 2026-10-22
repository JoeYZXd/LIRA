"""Isolated UI preview: no model calls, production tasks, or hardware access."""
from functools import partial
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import threading
import pytest

@pytest.fixture
def preview_server():
    root=Path(__file__).resolve().parents[3]/'tv-receiver'/'preview'
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(SimpleHTTPRequestHandler,directory=str(root)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port)
    finally:server.shutdown();server.server_close();thread.join(3)

@pytest.mark.parametrize('size',[(1280,720),(1920,1080)])
def test_tv_large_type_remote_only_no_scroll(preview_server,page,size):
    page.set_viewport_size(dict(width=size[0],height=size[1]));page.goto(preview_server+'/?tv=1')
    page.keyboard.press('Enter');assert page.evaluate('preview.state().state')=='waiting_start'
    page.keyboard.press('ArrowRight');page.wait_for_function("preview.state().state==='waiting_child'")
    page.wait_for_timeout(150)
    assert page.locator('#words span').first.evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')>=85
    assert page.evaluate('document.documentElement.scrollHeight<=innerHeight&&document.documentElement.scrollWidth<=innerWidth')
    initial=page.evaluate('preview.state().page')
    for key in ['Enter','ArrowUp','ArrowDown','ContextMenu']:page.keyboard.press(key)
    assert page.evaluate('preview.state().page')==initial
    page.wait_for_timeout(400);page.keyboard.down('ArrowRight');page.keyboard.down('ArrowRight');page.keyboard.up('ArrowRight')
    assert page.evaluate('preview.state().page')==4
    page.wait_for_timeout(400);page.keyboard.press('ArrowLeft');assert page.evaluate('preview.state().page')==0
    assert '确认键' not in page.locator('#guides').inner_text()

def test_preview_manual_audio_states_debug_and_pause(preview_server,page):
    page.set_viewport_size(dict(width=1920,height=1080));page.goto(preview_server)
    for _ in range(5):page.keyboard.press('ArrowRight');page.wait_for_timeout(400)
    page.wait_for_function("preview.state().phase==='dictation'&&preview.state().state==='waiting_child'")
    before=page.evaluate('preview.state()');page.wait_for_timeout(1800);after=page.evaluate('preview.state()')
    assert before['index']==after['index'] and before['repeats']==after['repeats']==1
    assert '第一个词' in page.locator('#title').inner_text()
    assert '/' not in page.locator('#progress').inner_text()
    assert not any(w in page.locator('.television').inner_text() for w in ['仙女','山顶','巨大','座位','闪光','小狗','形状'])
    page.locator('#debug-toggle').check();assert page.locator('#debug-log').is_visible()
    page.locator('#debug-toggle').uncheck();assert not page.locator('#debug-log').is_visible()
    page.keyboard.press('Enter');page.wait_for_function('preview.state().repeats===2')
    page.keyboard.press('ArrowRight');page.wait_for_function('preview.state().index===1');page.wait_for_timeout(400)
    page.keyboard.press('ArrowLeft');page.wait_for_function("preview.state().index===0&&preview.state().state==='waiting_child'")
    page.keyboard.press('Escape');assert page.evaluate('preview.state().state')=='paused'
    page.wait_for_timeout(400);page.keyboard.press('Enter');page.wait_for_function("preview.state().state==='waiting_child'")
    for _ in range(7):page.keyboard.press('ArrowRight');page.wait_for_timeout(400)
    assert page.evaluate('preview.state().state')=='completed'
    page.wait_for_timeout(1500);assert page.evaluate('preview.state().state')=='completed'
