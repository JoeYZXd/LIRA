/* LIRA 开发者控制台（U1 磁贴外壳 + U5 实时区三工作区）——原生 JS，无构建链、无框架、无 CDN。
 *
 * 页面分派：登录页（#dev-login-form）只绑口令提交；控制台页（#dev-console）
 * 装配全部诊断操作。与旧内嵌版行为一一对应：
 *   - api()：401 -> 统一回 /dev 登录页（僵尸页面自愈，只跳一次）；
 *     网络级失败 -> status=0 + 可见错误，不静默。
 *   - 录音状态机（独占设备）、预览 ~0.5fps 单帧轮询、整机回放等照旧。
 * U1 真实回读：音量/语速/日志级别/相机方向从 /dev/api/status 写入控件，
 *   提交前按真实范围客户端预检，失败保留输入并显示原因。
 * U5 实时区（KTD4 纪律）：
 *   - observe 客户端：250ms 节拍 + 最多一个在途请求（节拍到点在途则跳拍，
 *     不排队）；事件满页立即续拉；失败按 1/2/4/8s 指数退避（成功复位）；
 *     隐私 403 -> 清空敏感展示并停拉（手动"重新连接"或刷新恢复）；
 *     401 沿 api() 跳转；页面隐藏停节拍，恢复可见重新首拉建基线；
 *     实时区磁贴折叠即停订阅（KTD4"未展开实时区停订阅"）。
 *   - reset=true（实例不匹配/游标越界/区间淘汰）-> 清空曲线/轮卡/日志缓冲，
 *     用整窗快照整体重建，绝不凭旧数据补出声音或回复。
 *   - 声音曲线：Canvas 2D 画最近 10 秒真实包络（min~max 竖带 + RMS 折线），
 *     gap 画间断虚线并标原因；无帧/静音/停更/断连/隐私各态可辨——绘制停顿
 *     不伪造数据（没有新帧就明确显示停更，不延续旧曲线假装实时）。
 *   - 同轮交互：按 turn_id 归并 语句→路由→模型→播报，首段/总耗时由事件
 *     t_mono 差计算；无 turn_id 的 llm_* 事件归"未关联轮次"，不丢弃。
 *   - 日志区：语音事件 / 模型交互 / 系统日志三 tab，共用暂停滚动（默认暂停，
 *     跟随时仅在已滚到底时贴底）/ 类别与关键字筛选 / 清空当前展示（仅前端
 *     缓冲，服务端环不受影响）。旧 /dev/api/voice-log 1s 轮询移除，页面统一
 *     走 observe 事件流。
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

function vlEscape(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function hhmmss(wallMs) {
  return new Date(wallMs).toLocaleTimeString('zh-CN', {hour12: false});
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

/* ---------- 同步 / 整机回放磁贴 ---------- */

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

/* ---------- 实时区（U5）：observe 客户端（KTD4 纪律） ---------- */

const OBSERVE_TICK_MS = 250;                        // 默认节拍（KTD4）
const OBSERVE_BACKOFF_MS = [1000, 2000, 4000, 8000]; // 失败退避档位（成功复位）
const OBSERVE_AUDIO_WINDOW = 10;                    // 曲线窗口秒数（=遥测音频环）
const LOG_BUFFER_LIMIT = 500;                       // 日志缓冲上限（淘汰最旧）
const LOG_RENDER_LIMIT = 200;                       // 单次渲染上限（缓冲仍有界）
const TURN_CARD_LIMIT = 20;                         // 轮卡展示上限（新轮在前）

const observe = {
  timer: null,          // 节拍 setTimeout 句柄（单一定时器，随时可停）
  inFlight: false,      // 单飞：最多一个在途请求
  instanceId: null,     // 遥测实例（请求参数 + reset 判定）
  audioCursor: null, eventCursor: null,
  failCount: 0,         // 连续失败次数（退避档位）
  stopped: false,       // 隐私 403 后停拉（不自动重试）
  liveOpen: false,      // 实时区磁贴展开态（折叠即停订阅）
  idlePolls: 0          // 成功拉取但无新音频帧的连续次数（停更判定）
};

/* 数据缓冲：reset / 隐私 403 时整体清空，再由整窗快照重建 */
const audioFrames = [];        // 音频帧（envelope/gap 混排，t_mono 时间轴）
const markerEvents = [];       // 曲线轴标记事件（wake/utterance/speak*）
const turnCards = new Map();   // turn_id -> 轮卡聚合
const unlinkedLlm = [];        // 无 turn_id 的 llm_* 事件（未关联轮次）
const logBuffers = { voice: [], model: [], sys: [] }; // 日志区三 tab 数据源
let logTab = 'voice';
let logDirty = false;
let lastKindSig = '';
let lastAudioAt = 0;           // 最近一次见到新音频帧的本地墙钟（ms）

const LOG_KIND_LABELS = {
  state: '状态', wake: '唤醒', asr: '识别', asr_partial: '识别中',
  audio: '收音', audio_gate: '输入门控', kws: '关键词', command: '指令',
  playback: '播放控制', llm: 'LLM', llm_reply: 'LLM', llm_fail: 'LLM',
  speak: '播报', privacy: '隐私',
  utterance: '语句', turn: '轮次', route: '路由',
  speak_done: '播报完成', speak_interrupt: '播报中断',
  llm_request: '模型请求', llm_first_delta: '模型首段',
  llm_text: '模型快照', llm_done: '模型完成', llm_cancel: '模型取消'
};
const MARKER_KINDS = { wake: '唤', utterance: '语', speak: '播', speak_interrupt: '断' };
const MARKER_COLORS = { wake: '#B45309', utterance: '#5F7A4E', speak: '#6F5B8E', speak_interrupt: '#A63D2F' };
const ROUTE_LABELS = {
  local_rule: '本地规则', local_llm: '本地模型',
  remote_llm: '远程模型', unavailable: '不可用'
};

function isModelKind(kind) { return kind.indexOf('llm_') === 0; }

/* ---- 状态机：节拍 / 单飞 / 退避 / 可见性 / 展开态 ---- */

function observeSchedule(delayMs) {
  clearTimeout(observe.timer);
  observe.timer = setTimeout(observeTick, delayMs);
}

function observeTick() {
  if (observe.stopped || !observe.liveOpen || document.hidden) { return; }
  if (observe.inFlight) { observeSchedule(OBSERVE_TICK_MS); return; } // 跳拍，不排队
  observePull();
}

async function observePull() {
  const q = [];
  if (observe.instanceId) { q.push('instance=' + encodeURIComponent(observe.instanceId)); }
  if (observe.audioCursor !== null) { q.push('audio_cursor=' + observe.audioCursor); }
  if (observe.eventCursor !== null) { q.push('event_cursor=' + observe.eventCursor); }
  observe.inFlight = true;
  let r;
  try {
    r = await api('/dev/api/observe' + (q.length ? '?' + q.join('&') : ''));
  } finally {
    observe.inFlight = false;
  }
  if (r.status === 200) {
    observe.failCount = 0;
    applyObserve(r.body);
    if (!observe.liveOpen || document.hidden || observe.stopped) { return; } // 态已变：不续排
    // 事件满页（==上限）可能还有续页：立即续拉（仍受单飞约束）
    if (r.body.events.items.length >= 100) { observeTick(); return; }
    observeSchedule(OBSERVE_TICK_MS);
    return;
  }
  if (r.status === 403) { observePrivacyStop(r.body.error || '隐私模式开启'); return; }
  if (r.status === 401) { return; } // api() 已统一跳登录页（僵尸页面自愈）
  // 网络失败（status=0）/ 503 / 5xx：指数退避后重试，状态行可见
  const delay = OBSERVE_BACKOFF_MS[Math.min(observe.failCount, OBSERVE_BACKOFF_MS.length - 1)];
  observe.failCount += 1;
  updateCurveMeta({text: '已断连（' + Math.round(delay / 1000) + ' 秒后重试）', cls: 'err'});
  observeSchedule(delay);
}

/* 隐私联锁（AE5）：清空敏感展示并停拉；恢复靠手动"重新连接"或刷新页面 */
function observePrivacyStop(msg) {
  observe.stopped = true;
  clearTimeout(observe.timer);
  clearLocalBuffers();
  drawCurve();
  renderTurns();
  renderLog(true);
  document.getElementById('privacy-banner-text').textContent =
    msg + '：已清空敏感展示并停止观察（关闭隐私后点击"重新连接"或刷新页面恢复）。';
  document.getElementById('privacy-banner').hidden = false;
}

/* 手动恢复入口：清游标重新首拉；隐私未关则再次 403 再停（诚实不绕闸） */
function observeResume() {
  observe.stopped = false;
  observe.failCount = 0;
  observe.audioCursor = null;
  observe.eventCursor = null;
  observe.instanceId = null;
  document.getElementById('privacy-banner').hidden = true;
  if (!observe.liveOpen || document.hidden) { return; }
  observeSchedule(0);
}

/* 重新首拉建基线（展开/恢复可见时）：清游标 → 服务端整窗 + reset 重建，
   防隐藏期间 10 秒窗外出现假连续 */
function observeRebaseline() {
  observe.audioCursor = null;
  observe.eventCursor = null;
  observe.failCount = 0;
  observeSchedule(0);
}

/* KTD4：未展开实时区停订阅——展开即首拉+节拍，折叠即停节拍 */
function observeLiveChange(open) {
  observe.liveOpen = open;
  clearTimeout(observe.timer);
  if (!open || observe.stopped || document.hidden) { return; }
  observeRebaseline();
}

function initObserveVisibility() {
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      clearTimeout(observe.timer); // 停节拍；在途请求自然完成、结果照常并入
    } else if (observe.liveOpen && !observe.stopped) {
      observeRebaseline(); // 恢复可见：重新首拉建基线
    }
  });
}

/* ---- 增量归并与 reset 重建 ---- */

function clearLocalBuffers() {
  audioFrames.length = 0;
  markerEvents.length = 0;
  turnCards.clear();
  unlinkedLlm.length = 0;
  logBuffers.voice.length = 0;
  logBuffers.model.length = 0;
  observe.idlePolls = 0;
  lastAudioAt = 0;
  logDirty = true;
  lastKindSig = '';
}

function applyObserve(body) {
  // reset（实例不匹配/游标越界/区间淘汰/首拉）：清空旧状态整体重建
  const didReset = body.audio.reset || body.events.reset;
  if (didReset) { clearLocalBuffers(); }
  observe.instanceId = body.instance_id;
  if (body.audio.items.length) {
    observe.idlePolls = 0;
    lastAudioAt = Date.now();
    for (const f of body.audio.items) { audioFrames.push(f); }
    trimAudioFrames();
  } else {
    observe.idlePolls += 1; // 有响应无新帧：停更判定计数（不假装实时）
  }
  observe.audioCursor = body.audio.next_cursor;
  for (const ev of body.events.items) { ingestEvent(ev); }
  observe.eventCursor = body.events.next_cursor;
  drawCurve();
  updateCurveMeta();
  // reset 后即使快照为空也必须重渲染（旧轮卡不得残留）
  if (didReset || body.events.items.length) {
    renderTurns();
    renderLog(didReset);
  }
}

function trimAudioFrames() {
  const newest = audioFrames[audioFrames.length - 1].t_mono;
  while (audioFrames.length &&
         audioFrames[0].t_mono < newest - OBSERVE_AUDIO_WINDOW - 0.5) {
    audioFrames.shift();
  }
}

function ingestEvent(ev) {
  if (MARKER_KINDS[ev.kind]) {
    markerEvents.push(ev);
    if (markerEvents.length > 60) { markerEvents.shift(); }
  }
  if (ev.turn_id !== null && ev.turn_id !== undefined) {
    applyEventToTurn(ev);
  } else if (isModelKind(ev.kind)) {
    // 无轮次关联的模型事件（如阅读白话）：归"未关联轮次"，不丢弃
    unlinkedLlm.push(ev);
    if (unlinkedLlm.length > 20) { unlinkedLlm.shift(); }
  }
  const bucket = isModelKind(ev.kind) ? logBuffers.model : logBuffers.voice;
  bucket.push(ev);
  if (bucket.length > LOG_BUFFER_LIMIT) { bucket.splice(0, bucket.length - LOG_BUFFER_LIMIT); }
  logDirty = true;
}

function applyEventToTurn(ev) {
  let card = turnCards.get(ev.turn_id);
  if (!card) {
    card = {turnId: ev.turn_id, userText: null, route: null, reply: null,
            replyFromDone: false, requestMono: null, firstDeltaMono: null,
            turnMono: null, endMono: null, speakResult: null,
            fail: null, cancelled: false, interrupted: false};
    turnCards.set(ev.turn_id, card);
  }
  switch (ev.kind) {
    case 'turn':
      card.userText = ev.text;
      if (card.turnMono === null) { card.turnMono = ev.t_mono; }
      break;
    case 'route': card.route = ev.text; break;
    case 'llm_request': card.requestMono = ev.t_mono; break;
    case 'llm_first_delta': card.firstDeltaMono = ev.t_mono; break;
    case 'llm_text': if (!card.replyFromDone) { card.reply = ev.text; } break;
    case 'llm_done': card.reply = ev.text; card.replyFromDone = true; break;
    case 'llm_fail': card.fail = ev.text; break;
    case 'llm_cancel': card.cancelled = true; break;
    case 'speak_done':
      card.endMono = ev.t_mono; card.speakResult = '播报完成'; break;
    case 'speak_interrupt':
      card.endMono = ev.t_mono; card.speakResult = '播报被打断';
      card.interrupted = true; break;
    default: break; // 其余镜像类不进轮卡（可在日志区查看）
  }
  if (turnCards.size > TURN_CARD_LIMIT * 2) {
    // 有界：超出展示两倍的旧轮先淘汰（新轮在前渲染时只取 TURN_CARD_LIMIT）
    const ids = Array.from(turnCards.keys()).sort((a, b) => a - b);
    while (turnCards.size > TURN_CARD_LIMIT * 2) { turnCards.delete(ids.shift()); }
  }
}

/* ---- 声音曲线（Canvas 2D；只在数据到达时重绘，无动画循环、不伪造数据） ---- */

function drawCurve() {
  const canvas = document.getElementById('curve-canvas');
  if (!canvas) { return; }
  const empty = document.getElementById('curve-empty');
  const gapsEl = document.getElementById('curve-gaps');
  const cssW = canvas.clientWidth || 900;
  if (canvas.width !== Math.round(cssW)) { canvas.width = Math.round(cssW); canvas.height = 150; }
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  if (!audioFrames.length) {
    empty.hidden = false;
    empty.textContent = observe.stopped
      ? '观察已停止（隐私开启中，展示已清空）'
      : '等待数据…（展开实时区后自动开始观察）';
    gapsEl.hidden = true;
    return;
  }
  empty.hidden = true;
  const tEnd = audioFrames[audioFrames.length - 1].t_mono;
  const tStart = tEnd - OBSERVE_AUDIO_WINDOW;
  const mid = H / 2, amp = H / 2 - 16;
  // 淡网格（每 2 秒）与零线
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(73,61,53,.10)';
  for (let i = 0; i <= 5; i++) {
    const x = Math.round(i / 5 * W) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, 12); ctx.lineTo(x, H - 12); ctx.stroke();
  }
  ctx.strokeStyle = 'rgba(73,61,53,.28)';
  ctx.beginPath(); ctx.moveTo(0, mid + 0.5); ctx.lineTo(W, mid + 0.5); ctx.stroke();
  // 真实包络：min~max 竖带 + RMS 折线（同一线性标度）
  const bucketW = Math.max(2, W / 100 - 1);
  ctx.fillStyle = 'rgba(95,122,78,.40)';
  ctx.strokeStyle = '#5F7A4E';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let rmsStarted = false;
  for (const f of audioFrames) {
    if (f.kind !== 'envelope' || f.t_mono < tStart) { continue; }
    const x = (f.t_mono - tStart) / OBSERVE_AUDIO_WINDOW * W;
    const lo = Math.max(-1, Math.min(1, f.min)), hi = Math.max(-1, Math.min(1, f.max));
    const yTop = mid - hi * amp, yBot = mid - lo * amp;
    ctx.fillRect(x - bucketW / 2, yTop, bucketW, Math.max(1, yBot - yTop));
    const yRms = mid - Math.max(0, Math.min(1, f.rms)) * amp;
    if (!rmsStarted) { ctx.moveTo(x, yRms); rmsStarted = true; } else { ctx.lineTo(x, yRms); }
  }
  ctx.stroke();
  // 间断：红色虚线 + 下方原因短语（gap 是真实标记，绝不画成静音）
  const recentGaps = [];
  for (const f of audioFrames) {
    if (f.kind !== 'gap' || f.t_mono < tStart) { continue; }
    const x = Math.round((f.t_mono - tStart) / OBSERVE_AUDIO_WINDOW * W) + 0.5;
    ctx.save();
    ctx.strokeStyle = '#A63D2F';
    ctx.setLineDash([4, 3]);
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, 6); ctx.lineTo(x, H - 6); ctx.stroke();
    ctx.restore();
    if (recentGaps.length < 3) { recentGaps.push(f); }
  }
  if (recentGaps.length) {
    gapsEl.hidden = false;
    gapsEl.textContent = '间断：' + recentGaps.slice().reverse().map(f =>
      f.reason + '（' + hhmmss(f.ts * 1000) + '）').join(' · ');
  } else {
    gapsEl.hidden = true;
  }
  // 激活标记：唤/语/播/断 落同一 t_mono 时间轴
  ctx.font = '11px system-ui, sans-serif';
  ctx.lineWidth = 2;
  for (const ev of markerEvents) {
    if (ev.t_mono < tStart) { continue; }
    const x = Math.round((ev.t_mono - tStart) / OBSERVE_AUDIO_WINDOW * W);
    ctx.fillStyle = MARKER_COLORS[ev.kind] || '#493D35';
    ctx.fillText(MARKER_KINDS[ev.kind], Math.max(1, x - 5), 11);
    ctx.fillRect(x, 14, 1.5, H * 0.25);
  }
}

/* 曲线状态行（优先级：外部覆盖 > 无帧 > 停更 > 间断 > 静音 > 实时） */
function updateCurveMeta(override) {
  let text, cls = 'ok';
  if (override) {
    text = override.text; cls = override.cls;
  } else if (!audioFrames.length) {
    text = '等待数据（未见到音频帧）'; cls = 'muted';
  } else if (observe.idlePolls >= 8) {
    text = '音频停更（可能独占录音/采集中断）——绘制停顿不代表静音'; cls = 'err';
  } else if (audioFrames[audioFrames.length - 1].kind === 'gap') {
    text = '间断：' + (audioFrames[audioFrames.length - 1].reason || '未知原因'); cls = 'err';
  } else if (audioFrames[audioFrames.length - 1].rms <= 0.001) {
    text = '静音中（有数据，电平≈0）'; cls = 'muted';
  } else {
    text = '实时';
  }
  document.getElementById('curve-status').textContent = text;
  document.getElementById('curve-status').className = cls;
  const lastText = '最后更新：' + (lastAudioAt ? hhmmss(lastAudioAt) : '--:--:--');
  document.getElementById('curve-last').textContent = lastText;
  document.getElementById('curve-canvas').setAttribute('aria-label',
    '实时声音曲线（最近 10 秒包络）。' + text + '。' + lastText + '。');
}

/* ---- 同轮交互（按 turn 合并；耗时从事件 t_mono 差计算） ---- */

function fmtMs(ms) {
  return ms >= 1000 ? (ms / 1000).toFixed(2) + ' s' : Math.round(ms) + ' ms';
}

function routeText(card) {
  if (!card.route) { return '（尚未知实际路由）'; }
  return (ROUTE_LABELS[card.route] || card.route) + '（' + card.route + '）';
}

function turnCardHtml(c) {
  const first = (c.requestMono !== null && c.firstDeltaMono !== null)
    ? fmtMs((c.firstDeltaMono - c.requestMono) * 1000) : '—';
  const total = (c.turnMono !== null && c.endMono !== null)
    ? fmtMs((c.endMono - c.turnMono) * 1000) : '—';
  let reply;
  if (c.reply !== null) { reply = vlEscape(c.reply); }
  else if (c.fail !== null) { reply = '<span class="err">模型失败：' + vlEscape(c.fail) + '</span>'; }
  else if (c.cancelled) { reply = '<span class="muted">已取消</span>'; }
  else { reply = '<span class="muted">（等待回复…）</span>'; }
  const speak = c.speakResult ? vlEscape(c.speakResult) : '（等待播报结束）';
  return '<div class="turn-card' + (c.interrupted ? ' is-interrupted' : '') +
    '" data-turn="' + c.turnId + '">' +
    '<div class="turn-line"><span class="turn-role">用户</span>' +
    vlEscape(c.userText || '（未捕获语句文本）') + '</div>' +
    '<div class="turn-line"><span class="turn-role">路由</span>' + vlEscape(routeText(c)) + '</div>' +
    '<div class="turn-line"><span class="turn-role">回复</span>' + reply + '</div>' +
    '<div class="turn-meta">首段 ' + first + ' · 语句→播报结束 ' + total +
    ' · ' + speak + '</div></div>';
}

function renderTurns() {
  const view = document.getElementById('turns-view');
  const ids = Array.from(turnCards.keys()).sort((a, b) => b - a).slice(0, TURN_CARD_LIMIT);
  view.innerHTML = ids.length
    ? ids.map(id => turnCardHtml(turnCards.get(id))).join('')
    : '<p class="muted">（暂无语音轮次，与 LIRA 说一句话试试）</p>';
  const unlinkedBlock = document.getElementById('unlinked-block');
  const unlinkedView = document.getElementById('unlinked-view');
  if (unlinkedLlm.length) {
    unlinkedBlock.hidden = false;
    unlinkedView.innerHTML = unlinkedLlm.slice().reverse().map(ev => rowHtml(ev)).join('');
  } else {
    unlinkedBlock.hidden = true;
    unlinkedView.innerHTML = '';
  }
}

/* ---- 日志区（语音事件 / 模型交互 / 系统日志） ---- */

function rowHtml(ev) {
  const label = LOG_KIND_LABELS[ev.kind] || ev.kind;
  return '<div class="vl-row ' + rowClass(ev.kind) + '" data-kind="' + vlEscape(ev.kind) +
    '"><span class="vl-ts">' + hhmmss(ev.ts * 1000) +
    '</span> <span class="vl-kind">[' + label + ']</span> ' + vlEscape(ev.text) + '</div>';
}

function rowClass(kind) {
  if (kind === 'llm_fail' || kind === 'llm_cancel' || kind === 'speak_interrupt' || kind === 'privacy') {
    return 'vl-bad';
  }
  if (kind === 'wake') { return 'vl-wake'; }
  if (isModelKind(kind) || kind === 'llm' || kind === 'llm_reply') { return 'vl-llm'; }
  if (kind === 'speak' || kind === 'speak_done') { return 'vl-speak'; }
  return '';
}

function logRowMatches(item) {
  const kw = document.getElementById('log-filter').value.trim();
  if (logTab === 'sys') { return !kw || item.indexOf(kw) !== -1; }
  const kindSel = document.getElementById('log-kind').value;
  if (kindSel && item.kind !== kindSel) { return false; }
  return !kw || item.text.indexOf(kw) !== -1;
}

function rebuildKindOptions(buf) {
  const sel = document.getElementById('log-kind');
  if (logTab === 'sys') {
    sel.innerHTML = '<option value="">全部</option>';
    sel.disabled = true;
    lastKindSig = '';
    return;
  }
  sel.disabled = false;
  const kinds = [];
  for (const ev of buf) { if (kinds.indexOf(ev.kind) === -1) { kinds.push(ev.kind); } }
  const sig = kinds.join(',');
  if (sig === lastKindSig) { return; } // 种类未变不重建，避免打断选择
  lastKindSig = sig;
  const current = sel.value;
  sel.innerHTML = '<option value="">全部</option>' + kinds.map(k =>
    '<option value="' + vlEscape(k) + '">' + (LOG_KIND_LABELS[k] || k) + '</option>').join('');
  sel.value = (current === '' || kinds.indexOf(current) !== -1) ? current : '';
}

function renderLog(force) {
  if (!force && !logDirty) { return; }
  logDirty = false;
  const view = document.getElementById('log-view');
  const buf = logBuffers[logTab];
  document.getElementById('log-refresh-btn').hidden = logTab !== 'sys';
  rebuildKindOptions(buf);
  // 暂停滚动（默认勾选）时绝不自动滚；跟随时仅当已滚到底才贴底
  const follow = !document.getElementById('log-pause').checked &&
    view.scrollTop + view.clientHeight >= view.scrollHeight - 8;
  const rows = [];
  for (let i = buf.length - 1; i >= 0 && rows.length < LOG_RENDER_LIMIT; i--) {
    if (logRowMatches(buf[i])) {
      rows.push(logTab === 'sys'
        ? '<div class="vl-row"><span class="vl-sys-line">' + vlEscape(buf[i]) + '</span></div>'
        : rowHtml(buf[i]));
    }
  }
  view.innerHTML = rows.length ? rows.join('') : '<p class="muted">（暂无匹配事件）</p>';
  if (follow) { view.scrollTop = view.scrollHeight; }
  out('log-out', buf.length + ' 条缓冲' +
    (rows.length < buf.length ? '（显示最近 ' + rows.length + ' 条）' : ''), 'muted');
}

function setLogTab(tab) {
  logTab = tab;
  lastKindSig = '';
  for (const t of ['voice', 'model', 'sys']) {
    document.getElementById('tab-' + t).setAttribute('aria-selected', t === tab ? 'true' : 'false');
  }
  renderLog(true);
}

/* vlClear 语义并入：只清前端展示缓冲（服务端环不受影响） */
function clearActiveLog() {
  logBuffers[logTab].length = 0;
  renderLog(true);
}

async function loadLogs() {
  const r = await api('/dev/api/logs');
  if (r.status !== 200) {
    const msg = r.body.error || ('加载失败: ' + r.status);
    logBuffers.sys = ['（' + msg + '）'];
    renderLog(true);
    return out('log-out', msg, 'err');
  }
  logBuffers.sys = r.body.lines || [];
  renderLog(true);
  out('log-out', logBuffers.sys.length + ' 条', 'ok');
}

function initLogControls() {
  document.getElementById('log-filter').addEventListener('input', function () { renderLog(true); });
  document.getElementById('log-kind').addEventListener('change', function () { renderLog(true); });
}

/* ---------- 磁贴展开/收起（键盘语义见文件头注释） ---------- */

function setTileOpen(tile, open) {
  const head = tile.querySelector('.tile-head');
  const body = tile.querySelector('.tile-body');
  if (!head || !body) { return; }
  head.setAttribute('aria-expanded', open ? 'true' : 'false');
  body.hidden = !open;
  if (tile.id === 'tile-voicelog') { observeLiveChange(open); }
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
  initObserveVisibility();
  initLogControls();
  loadStatus();
  // 实时区默认折叠：展开磁贴才开始 observe 节拍（KTD4 停订阅纪律）
}

document.addEventListener('DOMContentLoaded', function () {
  if (document.getElementById('dev-login-form')) { initLogin(); }
  else if (document.getElementById('dev-console')) { initConsole(); }
});
