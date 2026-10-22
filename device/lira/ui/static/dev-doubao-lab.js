'use strict';

document.addEventListener('DOMContentLoaded', () => {
  const tile = document.getElementById('tile-doubao-lab');
  if (!tile) return;
  const $ = id => document.getElementById(id);
  const cards = new Map();
  let snapshot = null, timer = null, loading = false, posting = false;
  const states = {idle:'尚未连接', connecting:'连接中', ready:'已连接，可以发送', busy:'正在交互',
    ended:'会话已结束，记录仍保留', error:'连接或交互失败'};
  const turnStates = {preparing:'正在生成输入语音', sending:'正在发送语音', waiting:'等待豆包回复',
    completed:'回复完成', failed:'本轮失败', cancelled:'本轮已取消'};
  const categories = {none:'未标记', recognition:'识别有误', reply:'回复不合适', tool:'工具判断有误',
    latency:'等待太久', voice:'语音表现', other:'其他问题'};
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function message(text, error = false) {
    $('lab-message').textContent = text; $('lab-message').className = error ? 'err' : 'muted';
  }
  function createCard(turn, index) {
    const root = el('article', undefined, 'lab-turn'); root.dataset.turnId = turn.id;
    const title = el('h3', '第 ' + index + ' 轮');
    const input = el('p', undefined, 'lab-content');
    const transcript = el('p', undefined, 'lab-content');
    const reply = el('p', undefined, 'lab-content');
    const status = el('p', undefined, 'muted');
    const timing = el('p', undefined, 'muted');
    const tools = el('pre');
    const toolDetails = el('details'); toolDetails.append(el('summary','工具请求与模拟结果'),tools);
    const audio = el('audio'); audio.controls = true; audio.preload = 'none'; audio.hidden = true;
    const detail = el('details'); detail.append(el('summary', '这一轮有什么问题？'));
    const form = el('div', undefined, 'lab-feedback');
    const category = el('select'); category.id = 'lab-category-' + turn.id;
    for (const [value, name] of Object.entries(categories)) {
      const option = el('option', name); option.value = value; category.append(option);
    }
    const note = el('textarea'); note.id = 'lab-note-' + turn.id; note.maxLength = 3000;
    note.placeholder = '例如：我只是问如何取消，它却取消了录入。';
    const expected = el('textarea'); expected.id = 'lab-expected-' + turn.id; expected.maxLength = 2000;
    expected.placeholder = '例如：应该先解释取消方法，等待我明确要求后再操作。';
    for (const [name, control] of [['问题类型', category], ['哪里不对', note], ['期望怎样', expected]]) {
      const label = el('label', name); label.htmlFor = control.id; form.append(label, control);
    }
    const saved = el('span', '反馈会自动保存', 'muted');
    const save = el('button', '保存反馈'); save.type = 'button';
    form.append(save, saved); detail.append(form);
    root.append(title, input, transcript, reply, status, timing, toolDetails, audio, detail);
    $('lab-turns').append(root);
    const feedback = turn.feedback || {};
    category.value = feedback.category || 'none'; note.value = feedback.note || ''; expected.value = feedback.expected || '';
    const card = {root,input,transcript,reply,status,timing,tools,toolDetails,audio,category,note,expected,saved,
      id:turn.id,version:0,savedVersion:0,saving:null,timer:null};
    const changed = () => {
      card.version++; saved.textContent = '反馈尚未保存';
      clearTimeout(card.timer); card.timer = setTimeout(() => saveCard(card), 400);
    };
    for (const control of [category,note,expected]) control.addEventListener('input', changed);
    save.addEventListener('click', () => saveCard(card));
    cards.set(turn.id, card);
    return card;
  }
  async function saveCard(card) {
    clearTimeout(card.timer);
    if (card.saving) { await card.saving; return saveCard(card); }
    if (card.savedVersion === card.version) return true;
    const version = card.version;
    card.saved.textContent = '正在保存…';
    card.saving = api('/dev/api/doubao-lab', {method:'POST', body:JSON.stringify({action:'feedback',
      turn_id:card.id, category:card.category.value, note:card.note.value, expected:card.expected.value})});
    const r = await card.saving; card.saving = null;
    if (r.status !== 200) {
      card.saved.textContent = r.body.error || '保存失败，请重试'; return false;
    }
    card.savedVersion = version;
    card.saved.textContent = card.version === version ? '反馈已保存' : '反馈尚未保存';
    if (card.version !== version) return saveCard(card);
    return true;
  }
  async function saveAll() { return (await Promise.all([...cards.values()].map(saveCard))).every(Boolean); }
  const ms = value => typeof value === 'number' ? (value/1000).toFixed(2) + ' 秒' : '—';
  const text = (node, value) => { if (node.textContent !== value) node.textContent = value; };
  function render(s) {
    snapshot = s;
    text($('lab-status'), (states[s.state] || s.state) + ' · ' + s.turns.length + '/' + s.limits.turns + ' 轮 · 工具模拟');
    $('doubao-lab-summary').textContent = states[s.state] || s.state;
    $('lab-start').disabled = posting || ['ready','busy','connecting'].includes(s.state) || !s.configured;
    $('lab-stop').disabled = posting || !['ready','busy','error'].includes(s.state);
    $('lab-text').disabled = s.state !== 'ready';
    $('lab-send').disabled = posting || s.state !== 'ready' || !s.configured;
    if (!s.configured) message('请先在设备服务配置豆包实时语音密钥和音色。', true);
    else if (s.error) message(s.error, true);
    $('lab-empty').hidden = s.turns.length !== 0;
    const ids = new Set(s.turns.map(t => t.id));
    for (const [id, card] of cards) if (!ids.has(id)) { disposeCard(card); cards.delete(id); }
    for (const [index, turn] of s.turns.entries()) {
      const card = cards.get(turn.id) || createCard(turn, index+1);
      text(card.input, '你输入：' + turn.input_text);
      text(card.transcript, '豆包听到：' + (turn.transcript || '等待转写…'));
      text(card.reply, '豆包回复：' + (turn.reply || '等待回复…'));
      text(card.status, (turnStates[turn.state] || turn.state) + (turn.error ? ' · ' + turn.error : ''));
      const m = turn.metrics || {};
      text(card.timing, '输入合成 ' + ms(m.tts_ms) + ' · 首包等待（近似）' + ms(m.response_wait_ms) +
        ' · 回复音频 ' + ms(m.reply_audio_ms) + ' · 本轮总计 ' + ms(m.total_ms));
      card.toolDetails.hidden = !turn.tools.length;
      if (card.toolCount !== turn.tools.length) {
        card.tools.textContent = '工具请求与模拟结果：\n' + JSON.stringify(turn.tools, null, 2);
        card.toolCount = turn.tools.length;
      }
      card.audio.hidden = !turn.audio_available;
      if (turn.audio_available && !card.audio.getAttribute('src')) {
        card.audio.src = '/dev/api/doubao-lab/audio/' + encodeURIComponent(turn.id);
      }
      if (!turn.audio_available && card.audio.getAttribute('src')) {
        card.audio.pause(); card.audio.removeAttribute('src'); card.audio.load();
      }
    }
  }
  function disposeCard(card) {
    clearTimeout(card.timer); card.audio.pause(); card.audio.removeAttribute('src'); card.audio.load(); card.root.remove();
  }
  function clearSensitive() {
    for (const card of cards.values()) disposeCard(card);
    cards.clear(); $('lab-turns').replaceChildren(); $('lab-text').value = ''; snapshot = null;
    $('lab-send').disabled = $('lab-text').disabled = true;
  }
  async function refresh() {
    clearTimeout(timer);
    if (loading || tile.hidden || document.hidden) return;
    loading = true;
    try {
      const r = await api('/dev/api/doubao-lab');
      if (r.status === 200) render(r.body);
      else {
        if (r.status === 403) clearSensitive();
        message(r.body.error || '读取测试记录失败', true);
      }
    } finally { loading = false; if (!tile.hidden && !document.hidden) timer = setTimeout(refresh, 1000); }
  }
  async function action(kind, fields = {}) {
    if (posting) return false;
    posting = true; if (snapshot) render(snapshot);
    message(kind === 'start' ? '正在建立豆包会话…' : '正在处理…');
    try {
      const r = await api('/dev/api/doubao-lab', {method:'POST',body:JSON.stringify({action:kind,...fields})});
      if (r.status !== 200) { if (r.status === 403) clearSensitive(); message(r.body.error || '操作失败', true); return false; }
      render(r.body); message(kind === 'start' ? '已连接，可以输入。此连接内的对话共享上下文。' :
        kind === 'clear' ? '记录已清空。' : kind === 'stop' ? '会话已结束，记录仍可导出。' : '已提交，正在转成语音。');
      return true;
    } finally { posting = false; if (snapshot) render(snapshot); }
  }
  $('lab-start').addEventListener('click', () => action('start'));
  $('lab-stop').addEventListener('click', () => action('stop'));
  $('lab-clear').addEventListener('click', async () => {
    if (await action('clear')) clearSensitive();
  });
  async function send() {
    const text = $('lab-text').value.trim();
    if (!text || $('lab-send').disabled) return;
    const request_id = globalThis.crypto?.randomUUID?.() || Date.now() + '-' + Math.random();
    if (await action('send', {text,request_id})) $('lab-text').value = '';
  }
  $('lab-send').addEventListener('click', send);
  $('lab-text').addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); send(); }
  });
  async function download(format) {
    if (!await saveAll()) return message('反馈尚未保存成功，未导出。请重试保存。', true);
    let r;
    try { r = await fetch('/dev/api/doubao-lab/export?format=' + format); }
    catch { return message('无法连接设备，未能导出。',true); }
    if (!r.ok) {
      if (r.status === 401) location.href = '/dev';
      if (r.status === 403) clearSensitive();
      const body = await r.json().catch(() => ({}));
      return message(body.error || '导出失败', true);
    }
    const url = URL.createObjectURL(await r.blob());
    const extension = {json:'.json',markdown:'.md',zip:'.zip'}[format];
    const link = el('a'); link.href = url; link.download = 'lira-doubao-' + (snapshot?.id || 'record') + extension;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url),1000);
    message('已导出，包含已保存的问题说明和期望表现。');
  }
  $('lab-export-md').addEventListener('click', () => download('markdown'));
  $('lab-export-json').addEventListener('click', () => download('json'));
  $('lab-export-zip').addEventListener('click', () => download('zip'));
  document.addEventListener('lira:tile-visibility', async event => {
    if (event.detail.id !== tile.id) return;
    if (event.detail.open) refresh();
    else { clearTimeout(timer); for (const c of cards.values()) c.audio.pause(); await saveAll(); await action('stop'); }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(timer); else refresh(); });
  window.addEventListener('pagehide', () => {
    navigator.sendBeacon('/dev/api/doubao-lab',new Blob([JSON.stringify({action:'stop'})],{type:'application/json'}));
  });
});
