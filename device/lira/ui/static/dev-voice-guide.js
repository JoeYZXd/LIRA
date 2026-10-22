'use strict';
let vgToken = null, vgTimer = null, vgBusy = false, vgRevision = 0, vgPolling = false;
let vgInitialTarget=null, vgResolutionKey=null, vgResolvePayload=null;
let vgLastState=null;
try { vgToken=sessionStorage.getItem('lira-voice-guide-token'); } catch (_) {}
function keepVoiceGuideToken(token) {
  vgToken=token;
  try {if(token) sessionStorage.setItem('lira-voice-guide-token',token); else sessionStorage.removeItem('lira-voice-guide-token');} catch (_) {}
}
function openVoiceGuide() {
  if (vgToken) {
    document.getElementById('voice-guide').showModal();
    voiceGuideAction('status');
    clearInterval(vgTimer); vgTimer=setInterval(pollVoiceGuide,1000);
    return;
  }
  const dialog = document.getElementById('voice-guide');
  document.getElementById('vg-details').hidden = false;
  document.getElementById('vg-reading').hidden = true;
  document.getElementById('vg-message').textContent = '';
  document.getElementById('voice-guide-title').textContent='添加声纹档案';
  vgInitialTarget=null; vgResolutionKey=null;
  dialog.showModal();
  document.getElementById('vg-name').focus();
}
function renderVoiceGuide(s) {
  vgLastState=s;
  document.getElementById('vg-details').hidden = true;
  document.getElementById('vg-reading').hidden = false;
  document.getElementById('vg-step').textContent = s.step < 4 ? `采集 ${s.step+1} / 4 · 已采集 ${s.seconds} 秒有效语音` : '最后一步 · 独立验证（不参与建档）';
  document.getElementById('vg-text').textContent = s.text;
  document.getElementById('vg-clock').textContent = s.recording ? `录音中 ${s.recording_seconds} 秒（包含停顿）；40 秒自动停止` : '';
  document.getElementById('vg-record').hidden = s.recording || Boolean(s.validation?.passed);
  document.getElementById('vg-record').textContent = s.message?.includes('未通过') || s.message?.includes('不足') ? '重新录制本段' : '准备好了，开始录音';
  document.getElementById('vg-stop').hidden = !s.recording;
  document.getElementById('vg-save').hidden = !s.validation?.passed;
  document.getElementById('vg-save').hidden ||= Boolean(s.conflicts?.length);
  document.getElementById('vg-save').textContent = s.replace_member_id ? '验证通过，替换并保存' : '验证通过，保存档案';
  document.getElementById('vg-diagnostics').hidden = !s.validation;
  if (s.validation) document.getElementById('vg-score').textContent = `匹配分数 ${s.validation.score} / 门槛 ${s.validation.threshold}；与其他档案分差 ${s.validation.margin ?? '无其他档案'}，要求 ${s.validation.required_margin}`;
  document.getElementById('vg-message').textContent = s.message;
  document.getElementById('vg-segments').textContent = (s.segment_checks || []).map(c =>
    `第 ${c.segment} 段：${c.status==='borderline' ? '临界，已保留待最终验证' : '已采集'}${c.score==null ? '' : `（综合模板分数 ${c.score.toFixed(4)}）`}`).join('；');
  document.getElementById('vg-resolution').hidden=s.step<4;
  document.getElementById('vg-apply').textContent=s.validation ? '确认修改并保存' : '应用修改，继续独立验证';
  if(s.step>=4) {
    const key=JSON.stringify([s.conflicts,s.replace_member_id,s.name,s.address]);
    document.getElementById('vg-conflicts').textContent=s.conflicts?.length ? '发现重复档案：'+s.conflicts.map(c=>`${c.name}（${c.reason}）`).join('；')+'。请选择替换，或修改姓名后应用。' : s.replace_member_id ? '已选择替换原档案，新录音尚未保存。' : '未发现重复，可以直接保存；也可在这里修改姓名和称呼。';
    if(key!==vgResolutionKey) {
      vgResolutionKey=key;
      document.getElementById('vg-save-name').value=s.name;
      document.getElementById('vg-save-address').value=s.address;
      const select=document.getElementById('vg-target'); select.replaceChildren(new Option('新建档案（不替换）',''));
      const choices=new Map((s.conflicts||[]).map(c=>[c.member_id,c.name]));
      if(s.replace_member_id) choices.set(s.replace_member_id,(window.voiceProfileMembers||[]).find(m=>m.member_id===s.replace_member_id)?.name || '原档案');
      choices.forEach((name,id)=>select.add(new Option('替换：'+name,String(id))));
      select.value=s.replace_member_id ? String(s.replace_member_id) : '';
    }
  }
}
async function voiceGuideAction(action) {
  if (vgBusy) return;
  vgRevision++;
  vgBusy = true;
  const dialog = document.getElementById('voice-guide');
  dialog.querySelectorAll('button').forEach(b => b.disabled = true);
  if(action==='save') document.getElementById('vg-message').textContent='正在保存声纹档案，请稍候…';
  if(action==='resolve') document.getElementById('vg-message').textContent='正在检查姓名、替换目标和验证结果，请稍候…';
  try {
    const payload={action, token:vgToken, replace_member_id:vgInitialTarget,
      name:document.getElementById('vg-name').value, address:document.getElementById('vg-address').value};
    if(action==='resolve') Object.assign(payload,vgResolvePayload);
    const r = await api('/dev/api/voice-guided', {method:'POST', body:JSON.stringify(payload)});
    if (r.status < 200 || r.status >= 300) {
      if (r.body.state?.token) renderVoiceGuide(r.body.state);
      document.getElementById('vg-message').textContent = r.body.error || '连接失败，请重试。';
      if ([403,409].includes(r.status)) {clearInterval(vgTimer); keepVoiceGuideToken(null);}
      return;
    }
    if (r.body.saved || r.body.cancelled) {
      clearInterval(vgTimer); keepVoiceGuideToken(null); vgLastState=null; dialog.close();
      await loadVoiceCandidates();
      out('voice-candidates-out', r.body.saved ? '独立验证通过，声纹档案已保存。' : '录入已取消，没有保存。', 'ok');
      return r.body;
    }
    keepVoiceGuideToken(r.body.token);
    renderVoiceGuide(r.body);
    if (action==='begin') {
      clearInterval(vgTimer);
      vgTimer=setInterval(pollVoiceGuide,1000);
    }
    return r.body;
  } finally {
    vgBusy=false;
    dialog.querySelectorAll('button').forEach(b => b.disabled=false);
  }
}
async function resolveVoiceGuide() {
  if(vgBusy) return;
  const id=document.getElementById('vg-target').value;
  const canValidate=Boolean(vgLastState?.validation);
  const name=document.getElementById('vg-save-name').value;
  if(id && !confirm(canValidate ? `确认以「${name}」替换所选档案并保存？系统将检查独立验证，通过后立即保存，原成员聊天记忆保留。` : '确认选择替换所选成员的声纹？接下来仍需完成独立验证，旧档案暂不修改。')) return;
  vgResolvePayload={name:document.getElementById('vg-save-name').value,address:document.getElementById('vg-save-address').value,replace_member_id:id ? Number(id) : null};
  const state=await voiceGuideAction('resolve');
  if(!state) return;
  if(state.validation?.passed && !state.conflicts?.length) {
    await voiceGuideAction('save');
  } else if(canValidate) {
    document.getElementById('vg-message').textContent='尚未保存：'+state.message;
    document.getElementById('vg-message').scrollIntoView({block:'nearest'});
  }
}
function rerecordVoiceProfile(id) {
  const member=(window.voiceProfileMembers||[]).find(m=>m.member_id===id);
  if(!member || vgToken) return;
  openVoiceGuide(); vgInitialTarget=id;
  document.getElementById('voice-guide-title').textContent='重新录制：'+member.name;
  document.getElementById('vg-name').value=member.name;
  document.getElementById('vg-address').value=member.address || member.name;
  document.getElementById('vg-message').textContent='新录音验证通过并确认替换后才更新；取消或失败会保留旧声纹。';
}
async function deleteVoiceProfile(id) {
  const member=(window.voiceProfileMembers||[]).find(m=>m.member_id===id);
  if(!member || !confirm(`删除「${member.name}」的声纹？此操作移除声纹，保留成员和聊天记忆；以后可重新录制。`)) return;
  const r=await api('/dev/api/voice-profiles/'+id+'/delete',{method:'POST',body:JSON.stringify({confirm:true,revision:member.revision})});
  if(r.status===200) {await loadVoiceCandidates(); out('voice-candidates-out','声纹已删除，成员和聊天记忆已保留。','ok');}
  else out('voice-candidates-out',r.body.error || '删除失败，请重试。','err');
}
async function pollVoiceGuide() {
  if (!vgToken || vgBusy || vgPolling) return;
  vgPolling=true;
  const revision=vgRevision;
  try {
    const r=await api('/dev/api/voice-guided', {method:'POST',body:JSON.stringify({action:'status',token:vgToken})});
    // A status request must neither disable controls nor overwrite a newer action.
    if (vgBusy || revision!==vgRevision) return;
    if (r.status===200) renderVoiceGuide(r.body);
    else {
      document.getElementById('vg-message').textContent=r.body.error || '状态查询失败，请检查连接。';
      if ([403,409].includes(r.status)) {clearInterval(vgTimer); keepVoiceGuideToken(null);}
    }
  } finally {vgPolling=false;}
}
async function closeVoiceGuide() {
  if (vgBusy) return;
  if(vgToken && vgLastState?.recording) await voiceGuideAction('stop');
  clearInterval(vgTimer);
  document.getElementById('voice-guide').close();
  if(vgToken) out('voice-candidates-out','录入进度暂存中，点击“添加”继续；开始录入后 10 分钟到期，隐私开启或服务重启也会清除。','muted');
}
async function discardVoiceGuide() {
  if(vgBusy) return;
  if(vgToken && !confirm('确定放弃本次录入？未保存的录音和验证结果会被清除，无法恢复。')) return;
  if(vgToken) await voiceGuideAction('cancel');
  else closeVoiceGuide();
}
document.getElementById('voice-guide').addEventListener('cancel', e => {e.preventDefault(); closeVoiceGuide();});
window.addEventListener('pagehide', () => {
  // Navigation must not discard a completed recording. Stop an in-progress
  // capture, while keeping the draft on the server until its existing timeout.
  if (vgToken && vgLastState?.recording && !vgBusy) fetch('/dev/api/voice-guided', {method:'POST', keepalive:true, headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'stop', token:vgToken})}).catch(()=>{});
});
window.addEventListener('DOMContentLoaded', () => {
  if(vgToken) openVoiceGuide();
});
