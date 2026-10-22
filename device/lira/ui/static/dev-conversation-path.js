'use strict';

document.addEventListener('DOMContentLoaded', () => {
  const select = document.getElementById('conversation-path');
  if (!select) return;
  const current = document.getElementById('conversation-current');
  const config = document.getElementById('conversation-config');
  const message = document.getElementById('conversation-message');
  const apply = document.getElementById('conversation-switch');
  const start = document.getElementById('conversation-start');
  const stop = document.getElementById('conversation-stop');
  let busy = false, loading = false, dirty = false, snapshot = null;
  const names = {classic:'A · 现有语音链路', doubao_realtime:'B · 豆包实时语音'};
  const states = {standby:'待机', connecting:'连接中', listening:'已连接，正在交互',
    local_enrollment:'本地声纹录入中', error:'会话失败'};
  function render(s) {
    snapshot = s;
    if (!dirty) select.value = s.mode;
    current.textContent = '当前：' + (names[s.mode] || s.mode) + ' · ' + (states[s.state] || s.state);
    config.textContent = s.doubao_configured ? '豆包凭证与音色已填写；实际可用性以连接结果为准。' :
      '豆包配置未完成：' + (s.doubao_missing || []).join('、');
    if (s.error) { message.textContent = s.error; message.className = 'err'; }
    const timing = s.last_turn_metrics;
    const ms = value => typeof value === 'number' ? Math.round(value) + ' ms' : '等待';
    document.getElementById('conversation-metrics').textContent = timing ?
      '豆包本轮：判停事件到首帧 ' + ms(timing.input_end_to_output_ms) +
      ' · 播放 ' + ms(timing.playback_ms) + ' · 总计 ' + ms(timing.total_ms) :
      '豆包本轮耗时：等待语音交互。';
    start.disabled = busy || s.mode !== 'doubao_realtime' || !s.doubao_configured ||
      s.cloud_active || s.local_enrollment;
  }
  async function refresh() {
    if (loading || busy) return;
    loading = true;
    try {
      const r = await api('/dev/api/conversation-path');
      if (r.status === 200) render(r.body);
      else { current.textContent = r.body.error || '读取路径失败'; start.disabled = true; }
    } finally { loading = false; }
  }
  async function action(kind) {
    if (busy) return;
    busy = true;
    apply.disabled = start.disabled = stop.disabled = true;
    message.textContent = kind === 'start' ? '正在连接豆包，请等提示音后说话…' : '正在处理…';
    message.className = 'muted';
    try {
      const r = await api('/dev/api/conversation-path', {method:'POST',
        body:JSON.stringify({action:kind, mode:select.value})});
      if (r.status === 200) {
        dirty = false;
        render(r.body);
        message.textContent = kind === 'switch' ? '路径已切换；当前对话已结束，可以重新唤醒。' :
          kind === 'start' ? '豆包会话已开始，请对设备说话。' : '当前语音会话已结束。';
        message.className = 'ok';
      } else {
        message.textContent = r.body.error || '操作失败'; message.className = 'err';
      }
    } finally {
      busy = false; apply.disabled = stop.disabled = false;
      if (snapshot) render(snapshot);
    }
  }
  select.addEventListener('change', () => { dirty = true; });
  apply.addEventListener('click', () => action('switch'));
  start.addEventListener('click', () => action('start'));
  stop.addEventListener('click', () => action('stop'));
  document.getElementById('conversation-refresh').addEventListener('click', refresh);
  refresh();
  setInterval(() => {
    if (!document.hidden && !document.getElementById('tile-settings').hidden) refresh();
  }, 2000);
});
