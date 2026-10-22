'use strict';
document.addEventListener('DOMContentLoaded', () => {
  const tile = document.getElementById('tile-costs');
  if (!tile) return;
  const $ = id => document.getElementById(id);
  let loading = false, balance = null;
  const threshold = $('cost-threshold');
  try { threshold.value = localStorage.getItem('lira-balance-threshold') || '50'; } catch (_) {}
  function warning() {
    const amount = balance && balance.amounts && Number(balance.amounts.AvailableBalance);
    const limit = Number(threshold.value);
    const low = amount !== null && Number.isFinite(amount) && Number.isFinite(limit) && amount < limit;
    $('cost-warning').textContent = low ? '账户余额低于提醒阈值，请及时前往火山控制台充值。' : '';
    $('cost-warning').className = low ? 'err' : 'muted';
  }
  threshold.addEventListener('input', () => {
    if (threshold.validity.valid && threshold.value !== '') {
      try { localStorage.setItem('lira-balance-threshold', threshold.value); } catch (_) {}
      warning();
    }
  });
  const minutes = seconds => (Number(seconds || 0) / 60).toFixed(2);
  function render(data) {
    balance = data.balance;
    const amounts = balance.amounts;
    $('cost-balance').textContent = amounts ? amounts.AvailableBalance + ' 元' : '暂不可用';
    $('cost-balance-time').textContent = balance.updated_at ?
      (balance.stale ? '上次成功：' : '更新：') + new Date(balance.updated_at).toLocaleString('zh-CN') : '尚无成功查询';
    $('cost-status').textContent = !balance.configured ? '未配置火山 OpenAPI 凭据。' :
      balance.error || ('火山账户余额已获取，现金余额 ' + amounts.CashBalance + ' 元，欠费 ' + amounts.ArrearsBalance + ' 元。');
    $('cost-status').className = balance.error ? 'err' : 'muted';
    $('cost-status').hidden = balance.configured && !balance.error;
    warning();
    const usage = data.usage;
    if (!usage) { $('cost-since').textContent = '本机用量统计尚未装配。'; return; }
    const today = usage.days[usage.days.length - 1].total;
    $('cost-turns').textContent = today.sessions + ' / ' + today.turns;
    $('cost-input').textContent = minutes(today.input_seconds) + ' 分钟';
    $('cost-zero').textContent = '其中全零静音 ' + minutes(today.zero_seconds) + ' 分钟';
    $('cost-output').textContent = minutes(today.output_seconds) + ' 分钟';
    const fragment = document.createDocumentFragment();
    for (const day of [...usage.days].reverse()) {
      for (const [source, label] of [['normal', '日常'], ['test', '后台测试']]) {
        const v = day[source], tr = document.createElement('tr');
        for (const text of [day.date, label, v.sessions, v.turns, minutes(v.input_seconds), minutes(v.zero_seconds), minutes(v.output_seconds)]) {
          const td = document.createElement('td'); td.textContent = text; tr.append(td);
        }
        fragment.append(tr);
      }
    }
    $('cost-days').replaceChildren(fragment);
    $('cost-since').textContent = '统计开始于 ' + new Date(usage.started_at).toLocaleString('zh-CN') + '；此前的历史无法补算。阈值仅保存在当前浏览器。';
  }
  async function refresh(force = false) {
    if (loading || document.hidden || tile.hidden) return;
    loading = true; $('cost-refresh').disabled = true;
    try {
      const r = await api('/dev/api/costs' + (force ? '?refresh=1' : ''));
      if (r.status === 200) render(r.body);
      else $('cost-status').textContent = r.body.error || '查询失败，请稍后刷新。';
    } finally { loading = false; $('cost-refresh').disabled = false; }
  }
  $('cost-refresh').addEventListener('click', () => refresh(true));
  document.addEventListener('lira:tile-visibility', e => { if (e.detail.id === tile.id && e.detail.open) refresh(); });
  document.addEventListener('visibilitychange', () => refresh());
  setInterval(() => refresh(), 10000);
});
