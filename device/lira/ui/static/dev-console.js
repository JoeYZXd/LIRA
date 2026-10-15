/* LIRA 开发者控制台（U1 磁贴外壳）——原生 JS，无构建链、无框架、无 CDN。
 *
 * 页面分派：登录页（#dev-login-form）只绑口令提交；控制台页（#dev-console）
 * 装配全部诊断操作。与旧内嵌版行为一一对应：
 *   - api()：401 -> 统一回 /dev 登录页（僵尸页面自愈，只跳一次）；
 *     网络级失败 -> status=0 + 可见错误，不静默。
 *   - 实时区：语音交互日志 1s 轮询（可暂停/清空）；实时声音曲线为诚实
 *     占位（后续单元接入，不画假波形）。
 *   - 录音状态机（独占设备）、预览 ~0.5fps 单帧轮询、整机回放等照旧。
 * U1 修复与补齐：
 *   - 运行参数真实回读：音量/语速/日志级别/相机方向从 /dev/api/status
 *     写入控件（旧版音量/语速恒 undefined，从未回显）；提交前按 status
 *     返回的真实范围做客户端校验，非法值保留输入并显示字段错误；
 *     网络/服务失败同样保留输入。
 *   - 「水平翻转」按钮补实现（旧页按钮存在但无函数，点击报错）。
 *   - 预览取帧提升为 fetchFrame()：方向变更后刷新预览的旧意图（引用了
 *     闭包内不存在的 frame）现在真正可用。
 *   - 磁贴：aria-expanded 磁贴头点击原位展开/收起；Escape 收起并把焦点
 *     还给磁贴头按钮（Enter/Space 为按钮原生行为）。
 */

'use strict';

/* ---------- 登录页（口令提交；Enter 由表单原生触发 submit） ---------- */

async function doLogin() {
  const r = await fetch('/dev/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({passphrase: document.getElementById('pass').value})
  });
  if (r.ok) { location.href = '/dev'; return; }
  const j = await r.json().catch(() => ({error: 'HTTP ' + r.status}));
  document.getElementById('err').textContent = j.error || ('HTTP ' + r.status);
}

/* ---------- 控制台页：共享状态与工具 ---------- */

let recording = false, previewOn = false, previewTimer = null, fetching = false;
let recTimer = null, recLeft = 0, flipped = false, authRedirecting = false;
let vlTimer = null;
/* 运行参数允许范围（/dev/api/status 的 volume_range / speed_range 回读） */
const ranges = { volume: null, speed: null };

async function api(path, opts) {
  let r;
  try {
    r = await fetch(path, Object.assign({headers: {'Content-Type': 'application/json'}}, opts||{}));
  } catch (e) {
    // 网络级失败（服务重启/断连）：不再静默，调用方面板直接显示错误
    return {status: 0, body: {error: '无法连接设备服务（服务可能已重启，请刷新页面重新登录）'}};
  }
  if (r.status === 401 && !authRedirecting) {
    // 会话缺失/过期/签名失效：统一回登录页，不停留在僵尸页面
    authRedirecting = true;
    location.href = '/dev';
    return {status: 401, body: {error: '会话已失效，正在返回登录页…'}};
  }
  const t = await r.text();
  let j; try { j = JSON.parse(t); } catch { j = {raw: t}; }
  return {status: r.status, body: j};
}

function out(id, msg, cls) {
  const el = document.getElementById(id);
  el.textContent = msg; el.className = cls || 'muted';
}

/* ---------- 概览磁贴 ---------- */

async function loadStatus() {
  const r = await api('/dev/api/status');
  if (r.status !== 200) {
    const msg = r.body.error || ('加载失败: ' + r.status);
    document.getElementById('status-view').textContent = '（' + msg + '）';
    return out('status-out', msg, 'err');
  }
  const s = r.body;
  document.getElementById('status-view').textContent = JSON.stringify(s, null, 2);
  applyStatusReadback(s);
  fillOverviewSummary(s);
  out('status-out', '引擎=' + s.engine + ' 路由=' + s.route +
    (s.privacy_on ? ' [隐私开启：采集类测试将被拒绝]' : ''), 'ok');
}

/* 真实回读：只在字段存在时写入控件（相机缺失时服务端省略字段） */
function applyStatusReadback(s) {
  if (typeof s.volume === 'number') {
    document.getElementById('vol').value = s.volume;
  }
  if (typeof s.tts_speed === 'number') {
    document.getElementById('spd').value = s.tts_speed;
  }
  if (Array.isArray(s.volume_range) && s.volume_range.length === 2) {
    ranges.volume = s.volume_range;
    document.getElementById('vol').min = s.volume_range[0];
    document.getElementById('vol').max = s.volume_range[1];
  }
  if (Array.isArray(s.speed_range) && s.speed_range.length === 2) {
    ranges.speed = s.speed_range;
    document.getElementById('spd').min = s.speed_range[0];
    document.getElementById('spd').max = s.speed_range[1];
  }
  if (typeof s.loglevel === 'string') {
    const sel = document.getElementById('lvl');
    for (const opt of sel.options) {
      if (opt.value === s.loglevel) { sel.value = opt.value; break; }
    }
  }
  if (typeof s.camera_rotation === 'number') {
    flipped = !!s.camera_hflip;
    out('orient-out', '当前方向: ' + s.camera_rotation + '°' +
      (flipped ? '（已翻转）' : ''), 'muted');
  }
}

/* 概览磁贴头摘要行：折叠时也能看到引擎/路由/隐私/同步 */
function fillOverviewSummary(s) {
  const el = document.getElementById('overview-summary');
  if (!el) { return; }
  const parts = ['引擎=' + s.engine, '路由=' + s.route,
    '隐私' + (s.privacy_on ? '开' : '关')];
  if (s.sync && s.sync.session) { parts.push('同步=' + s.sync.session); }
  el.textContent = parts.join(' · ');
}

/* ---------- 运行参数磁贴：客户端范围校验 + 既有端点提交 ---------- */

/* 范围来自 status 真实能力；非法值不发起请求、输入保留、字段错误可见
   （服务端 /volume /speed 校验仍兜底）。 */
function settingProblem(input, range, label) {
  const v = Number(input.value);
  if (input.value.trim() === '' || Number.isNaN(v)) {
    return label + '数值无效';
  }
  if (range && (v < range[0] || v > range[1])) {
    return label + '须在 ' + range[0] + ' ~ ' + range[1] + ' 之间';
  }
  return null;
}

async function postForm(path, inputId, label, rangeKey, outId) {
  const input = document.getElementById(inputId);
  const problem = settingProblem(input, ranges[rangeKey], label);
  if (problem) { return out(outId, problem, 'err'); }
  const fd = new FormData();
  fd.append('value', input.value);
  try {
    const r = await fetch(path, {method: 'POST', body: fd});
    out(outId, r.ok ? '已提交' : '失败: ' + r.status, r.ok ? 'ok' : 'err');
  } catch (e) {
    // 网络/服务失败：错误可见，输入值原样保留
    out(outId, '无法连接设备服务（请刷新页面重新登录）', 'err');
  }
}

async function setLevel() {
  const r = await api('/dev/api/loglevel', {method: 'POST', body: JSON.stringify({level: document.getElementById('lvl').value})});
  out('lvl-out', r.status === 200 ? '已调整' : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}

/* ---------- 语音链路测试磁贴 ---------- */

async function ttsSpeak() {
  const text = document.getElementById('tts-text').value;
  const r = await api('/dev/api/tts', {method: 'POST', body: JSON.stringify({text})});
  out('tts-out', r.status === 200 ? ('已播报 ' + r.body.elapsed_ms + 'ms') : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}

async function recordToggle() {
  const btn = document.getElementById('rec-btn');
  if (!recording) {
    const r = await api('/dev/api/record/start', {method: 'POST', body: '{}'});
    if (r.status !== 200) return out('rec-out', r.body.error || r.status, 'err');
    recording = true;
    btn.textContent = '结束录音';
    recLeft = 0;
    recTimer = setInterval(() => { recLeft += 1; out('rec-out', '录音中… ' + recLeft + 's（上限 60s）'); }, 1000);
    out('rec-out', '录音中…', 'ok');
  } else {
    clearInterval(recTimer);
    const r = await api('/dev/api/record/stop', {method: 'POST', body: '{}'});
    recording = false;
    btn.textContent = '开始录音';
    if (r.status !== 200) return out('rec-out', r.body.error || r.status, 'err');
    if (r.body.discarded) return out('rec-out', '已按隐私联锁丢弃本次录音', 'err');
    out('rec-out', '完成 ' + r.body.seconds + 's / ' + r.body.bytes + 'B', 'ok');
    const clip = document.getElementById('clip');
    clip.src = '/dev/api/clip?ts=' + Date.now(); clip.style.display = 'block'; clip.play();
  }
}

async function asrTest() {
  const r = await api('/dev/api/asr', {method: 'POST', body: '{}'});
  out('asr-out', r.status === 200 ? ('文本: ' + r.body.text + ' | 意图: ' + JSON.stringify(r.body.intent)) : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}

/* ---------- 视觉链路磁贴 ---------- */

async function orientBy(delta) {
  // delta=0 复位；90/-90 相对当前方向累加（可多次点击循环）
  const payload = delta === 0 ? {rotation: 0} : {rotate_by: delta};
  const r = await api('/dev/api/camera/orientation', {method: 'POST', body: JSON.stringify(payload)});
  if (r.status !== 200) return out('orient-out', r.body.error || r.status, 'err');
  flipped = !!r.body.hflip;
  out('orient-out', '方向: ' + r.body.rotation + '°' + (flipped ? '（已翻转）' : ''), 'ok');
  if (previewOn) { await fetchFrame(); }
}

async function flip() {
  // 水平翻转（U1 补实现）：同一方向端点，hflip 取反
  const r = await api('/dev/api/camera/orientation', {method: 'POST', body: JSON.stringify({hflip: !flipped})});
  if (r.status !== 200) return out('orient-out', r.body.error || r.status, 'err');
  flipped = !!r.body.hflip;
  out('orient-out', '方向: ' + r.body.rotation + '°' + (flipped ? '（已翻转）' : ''), 'ok');
  if (previewOn) { await fetchFrame(); }
}

async function visionTest() {
  out('vis-out', '拍摄识别中…（慢速单帧，约数秒）');
  const r = await api('/dev/api/vision', {method: 'POST', body: '{}'});
  if (r.status !== 200) return out('vis-out', r.body.error || r.status, 'err');
  const img = document.getElementById('vis-img');
  img.src = r.body.image; img.style.display = 'block';
  document.getElementById('vis-view').textContent = r.body.text || '（无文字）';
  out('vis-out', '耗时: ' + JSON.stringify(r.body.timings_ms), 'ok');
}

/* 预览取帧（提升为顶层：预览轮询与方向变更后的刷新共用） */
async function fetchFrame() {
  let r;
  try {
    r = await fetch('/dev/api/vision/frame?ts=' + Date.now());
  } catch (e) {
    return stopPreview('无法连接设备服务，预览已停止', 'err');
  }
  if (r.status === 200) {
    const blob = await r.blob();
    const img = document.getElementById('preview-img');
    img.src = URL.createObjectURL(blob); img.style.display = 'block';
    out('preview-out', '预览中…');
  } else {
    stopPreview('取帧失败已停止: ' + r.status, 'err');
  }
}

function stopPreview(msg, cls) {
  previewOn = false;
  clearInterval(previewTimer);
  document.getElementById('preview-btn').textContent = '开启实时预览（~0.5fps）';
  document.getElementById('preview-img').style.display = 'none';
  if (msg) { out('preview-out', msg, cls); }
}

async function previewToggle() {
  if (previewOn) { return stopPreview('预览已停止'); }
  previewOn = true;
  document.getElementById('preview-btn').textContent = '停止预览';
  await fetchFrame();
  previewTimer = setInterval(async () => {
    if (!previewOn || fetching) return;
    fetching = true;
    try { await fetchFrame(); } finally { fetching = false; }
  }, 2200);
}

/* ---------- 同步 / 整机回放 / 运行日志磁贴 ---------- */

async function syncPull() {
  const r = await api('/dev/api/sync/pull', {method: 'POST', body: '{}'});
  out('sync-out', r.status === 200 ? ('状态: ' + JSON.stringify(r.body.sync)) : (r.body.error || r.status), r.status === 200 ? 'ok' : 'err');
}

async function replay(confirm) {
  const text = document.getElementById('replay-text').value;
  const r = await api('/dev/api/replay', {method: 'POST', body: JSON.stringify({text, confirm})});
  if (r.status !== 200) return out('replay-out', r.body.error || r.status, 'err');
  document.getElementById('replay-view').textContent =
    '迁移: ' + (r.body.transitions || []).join(' -> ') + '\n播报: ' + (r.body.spoken || []).join(' | ');
  out('replay-out', '完成', 'ok');
}

async function loadLogs() {
  const r = await api('/dev/api/logs');
  if (r.status !== 200) return out('log-out', '加载失败: ' + r.status, 'err');
  document.getElementById('log-view').textContent = (r.body.lines || []).join('\n') || '（空）';
  out('log-out', r.body.lines.length + ' 条', 'ok');
}

/* ---------- 实时区磁贴：语音交互日志（1s 轮询 / 暂停 / 清空） ---------- */

function vlAuto(cb) {
  clearInterval(vlTimer);
  if (cb.checked) vlTimer = setInterval(loadVoiceLog, 1000);
}

function vlEscape(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function loadVoiceLog() {
  const r = await api('/dev/api/voice-log');
  if (r.status !== 200) {
    const msg = r.body.error || ('加载失败: ' + r.status);
    document.getElementById('vl-view').textContent = '（' + msg + '）';
    return out('vl-out', msg, 'err');
  }
  const es = r.body.entries || [];
  const view = document.getElementById('vl-view');
  if (es.length === 0) { view.innerHTML = '（暂无语音交互，说一句“小鹿”试试）'; }
  else {
    const tag = {state:'', audio:'收音', kws:'关键词', audio_gate:'输入门控', asr_partial:'识别中', wake:'唤醒', asr:'识别', playback:'播放控制', llm:'LLM', llm_reply:'LLM', llm_fail:'LLM', speak:'播报', privacy:'隐私'};
    const cls = {wake:'vl-wake', llm:'vl-llm', llm_reply:'vl-llm', speak:'vl-speak', llm_fail:'vl-bad', privacy:'vl-bad'};
    view.innerHTML = es.slice().reverse().map(e => {
      const t = new Date(e.ts * 1000).toLocaleTimeString('zh-CN', {hour12: false});
      return '<div class="vl-row ' + (cls[e.kind]||'') + '"><span class="vl-ts">' + t +
        '</span> <span class="vl-kind">[' + (tag[e.kind]||e.kind) + ']</span> ' + vlEscape(e.text) + '</div>';
    }).join('');
  }
  out('vl-out', es.length + ' 条 · 引擎=' + r.body.state, 'ok');
}

async function vlClear() {
  await api('/dev/api/voice-log/clear', {method: 'POST', body: '{}'});
  loadVoiceLog();
}

/* ---------- 磁贴展开/收起（键盘语义见文件头注释） ---------- */

function setTileOpen(tile, open) {
  const head = tile.querySelector('.tile-head');
  const body = tile.querySelector('.tile-body');
  if (!head || !body) { return; }
  head.setAttribute('aria-expanded', open ? 'true' : 'false');
  body.hidden = !open;
}

function initTiles() {
  for (const head of document.querySelectorAll('.tile-head')) {
    head.addEventListener('click', function () {
      const tile = head.closest('.tile');
      setTileOpen(tile, head.getAttribute('aria-expanded') !== 'true');
    });
  }
  // Escape：收起焦点所在磁贴并把焦点还给磁贴头按钮
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') { return; }
    const tile = (e.target && e.target.closest) ? e.target.closest('.tile') : null;
    if (!tile) { return; }
    const head = tile.querySelector('.tile-head');
    if (head && head.getAttribute('aria-expanded') === 'true') {
      setTileOpen(tile, false);
      head.focus();
    }
  });
}

/* ---------- 装配：按页面分派 ---------- */

function initLogin() {
  const form = document.getElementById('dev-login-form');
  if (form) {
    form.addEventListener('submit', function (e) { e.preventDefault(); doLogin(); });
  }
}

function initConsole() {
  initTiles();
  loadStatus();
  vlTimer = setInterval(loadVoiceLog, 1000);
}

document.addEventListener('DOMContentLoaded', function () {
  if (document.getElementById('dev-login-form')) { initLogin(); }
  else if (document.getElementById('dev-console')) { initConsole(); }
});
