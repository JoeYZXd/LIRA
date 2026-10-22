/* Typed snapshots only; no ASR, no speculative playback or enrollment writes. */
'use strict';
(() => {
  const el = id => document.getElementById('jev-' + id);
  if (!el('text')) return;
  let run = null, revision = 0, timer = null, pollTimer = null;
  let composing = false, polling = false, lastSubmitted = null, finished = false;
  let configured = false, cacheReady = false, busy = false, clickTime = null;
  let cueSignature = '', decisionSignature = '';
  let panelVisible = true;
  const ms = value => value == null ? '—' : value.toFixed(1) + ' ms';
  function message(text) { el('message').textContent = text; }
  function controls() {
    const active = run && ['typing', 'deciding', 'playing'].includes(run.state);
    el('prepare').disabled = busy || active;
    el('start').disabled = busy || active || !configured || !cacheReady;
    el('phase').disabled = busy || active;
    el('text').disabled = !active || finished;
    el('finish').disabled = !active || finished || composing || !el('text').value.trim();
    el('cancel').disabled = !active || busy;
  }
  async function request(body) {
    return api('/dev/api/jev-probe', body ? {method: 'POST', body: JSON.stringify(body)} : undefined);
  }
  function render(r) {
    run = r;
    if (!['typing', 'deciding', 'playing'].includes(r.state)) {
      clearTimeout(timer); timer = null; finished = true;
    }
    el('response').textContent = ms(r.response_ms);
    el('playback').textContent = ms(r.playback_ms);
    el('total').textContent = ms(r.total_ms);
    el('reply').textContent = r.reply ? '已选语料：' + r.reply : '尚未选择播报语料。';
    el('timeline').textContent = '相对开始测试：输入完成 ' + ms(r.finish_ms) +
      ' / 首帧 ' + ms(r.first_output_ms) + ' / 排空 ' + ms(r.end_output_ms) +
      (r.finish_ms == null ? '' : '；复用已完成预判：' + (r.reused ? '是' : '否'));
    const signature = JSON.stringify(r.decisions);
    if (signature !== decisionSignature) {
      decisionSignature = signature;
      const rows = r.decisions.map(row => {
      const tr = document.createElement('tr');
      [row.revision + ' / ' + row.chars, ms(row.started_ms), ms(row.elapsed_ms),
        row.status + (row.choice ? ' / ' + row.choice : '') + (row.error ? ' / ' + row.error : ''),
        row.confidence == null ? '—' : row.confidence.toFixed(3)].forEach(value => {
        const td = document.createElement('td'); td.textContent = value; tr.appendChild(td);
      });
      return tr;
    });
      el('decisions').replaceChildren(...rows);
    }
    if (r.error) message(r.error);
    else if (r.state === 'complete') message('播放完成。' +
      (clickTime == null ? '' : '浏览器点击完成至收到完成状态约 ' + ms(performance.now() - clickTime) + '（含网络及轮询等待）。'));
    else if (r.state === 'playing') message('正在播放设备缓存语音…');
    else if (r.state === 'deciding') message('输入已结束，正在核对最终版本…');
    else if (r.state === 'cancelled') message('测试已取消。');
    controls();
  }
  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      const response = await request();
      if (response.status !== 200) {
        if (response.status === 403) {
          run = null; el('text').value = ''; el('decisions').replaceChildren();
          ['response', 'playback', 'total'].forEach(id => el(id).textContent = '—');
          el('reply').textContent = ''; el('timeline').textContent = '';
        }
        message(response.body.error); clearInterval(pollTimer); controls(); return;
      }
      const data = response.body; configured = data.configured; cacheReady = data.cache_ready;
      el('ready').textContent = (configured ? 'TypeSafe Jev 已配置' : '未配置 TYPESAFE_API_KEY') +
        ' · 缓存 ' + data.cache_count + ' 句' + (cacheReady ? '（可用）' : '（需预热）');
      if (cueSignature !== JSON.stringify(data.cues)) {
        cueSignature = JSON.stringify(data.cues);
        el('cues').replaceChildren(...Object.entries(data.cues).map(([key, text]) => {
          const li = document.createElement('li'); li.textContent = key + '：' + text; return li;
        }));
      }
      if (run && data.run && data.run.id === run.id) render(data.run);
      if (run && !['typing', 'deciding', 'playing'].includes(run.state)) clearInterval(pollTimer);
      controls();
    } finally { polling = false; }
  }
  el('prepare').onclick = async () => {
    busy = true; controls(); message('正在合成并缓存固定语音，预热期间不播放…');
    try {
      const r = await request({action: 'prepare'});
      message(r.status === 200 ? '已缓存 ' + r.body.cache_count + ' 句；预热耗时 ' + ms(r.body.prepare_ms) : r.body.error);
      await refresh();
    } finally { busy = false; controls(); }
  };
  el('start').onclick = async () => {
    busy = true; controls();
    try {
      const r = await request({action: 'start', phase: el('phase').value});
      if (r.status !== 200) { message(r.body.error); return; }
      if (!panelVisible) {
        run = r.body;
        await request({action: 'cancel', id: run.id});
        run.state = 'cancelled'; finished = true;
        return;
      }
      clearTimeout(timer); timer = null;
      revision = 0; finished = false; lastSubmitted = null; clickTime = null;
      el('text').value = ''; render(r.body); message('可以输入了；输入过程中自动分段预判，不会提前播报。');
      clearInterval(pollTimer); pollTimer = setInterval(refresh, 300);
    } finally { busy = false; controls(); if (run?.state === 'typing') el('text').focus(); }
  };
  async function partial() {
    timer = null;
    if (!panelVisible || !run || run.state !== 'typing' || finished || composing || !el('text').value.trim()) return;
    const text = el('text').value, id = run.id, rev = revision;
    if (lastSubmitted === text) return;
    const response = await request({action: 'partial', id, revision: rev, text});
    if (response.status !== 200) { message(response.body.error); return; }
    if (run?.id === id && revision === rev && !finished) lastSubmitted = text;
    await refresh();
  }
  el('text').addEventListener('input', () => {
    revision++; controls();
    if (!composing && timer == null) timer = setTimeout(partial, 450);
  });
  el('text').addEventListener('compositionstart', () => { composing = true; clearTimeout(timer); timer = null; controls(); });
  el('text').addEventListener('compositionend', () => { composing = false; controls(); if (timer == null) timer = setTimeout(partial, 450); });
  el('finish').onclick = async () => {
    if (finished || composing || !run) return;
    finished = true; clearTimeout(timer); timer = null; clickTime = performance.now(); controls();
    const r = await request({action: 'finish', id: run.id, revision, text: el('text').value});
    if (r.status === 200) {
      if (panelVisible && run?.id === r.body.id && !['cancelled', 'complete', 'failed'].includes(run.state)) render(r.body);
    }
    else { finished = false; message(r.body.error); controls(); }
  };
  el('cancel').onclick = async () => {
    clearTimeout(timer); timer = null; busy = true; controls();
    try {
      const r = await request({action: 'cancel', id: run.id});
      if (r.status !== 200) message(r.body.error);
      await refresh();
    } finally { busy = false; controls(); }
  };
  window.addEventListener('pagehide', () => { clearTimeout(timer); clearInterval(pollTimer); });
  document.addEventListener('lira:tile-visibility', event => {
    if (event.detail.id !== 'tile-jev') return;
    panelVisible = event.detail.open;
    if (!event.detail.open) {
      clearTimeout(timer); timer = null;
      if (run && ['typing', 'deciding', 'playing'].includes(run.state)) el('cancel').click();
      else clearInterval(pollTimer);
    } else refresh();
  });
  refresh();
})();
