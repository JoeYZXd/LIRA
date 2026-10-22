'use strict';
let vtToken=null,vtMember=null,vtBusy=false,vtPolling=false,vtTimer=null,vtRevision=0,vtEvent=null;
function testVoiceProfile(id){
  const member=(window.voiceProfileMembers||[]).find(m=>m.member_id===id);
  if(!member||!member.has_profile)return;
  vtMember=id;vtEvent=null;
  document.getElementById('vt-feedback').hidden=true;
  const choices=document.getElementById('vt-actual-member');choices.replaceChildren();
  for(const m of window.voiceProfileMembers||[]){const o=document.createElement('option');o.value=m.member_id;o.textContent=m.name;choices.append(o);}
  document.getElementById('voice-test-title').textContent='测试声纹：'+member.name;
  document.getElementById('vt-status').textContent='准备好后点击开始录音，对 LIRA 说一句话。';
  document.getElementById('vt-result').textContent='';
  document.getElementById('vt-start').textContent='开始录音';
  renderVoiceTest({token:null,recording:false});
  document.getElementById('voice-profile-test').showModal();
}
function renderVoiceTest(state){
  vtToken=state.token;
  document.getElementById('vt-start').hidden=Boolean(state.recording);
  document.getElementById('vt-stop').hidden=!state.recording;
  document.getElementById('vt-clock').textContent=state.recording?`录音中 ${state.recording_seconds} 秒，读完后点击“说完了”` : '';
  if(state.message)document.getElementById('vt-status').textContent=state.message;
  if(state.result){
    const r=state.result;vtEvent=r.event_id;
    document.getElementById('vt-feedback').hidden=!vtEvent;
    const reason={below_threshold:'最高分未达到识别门槛',insufficient_margin:'与其他档案分差不足',empty_profiles:'没有登记声纹',incompatible_profiles:'档案数据维度或数值异常',invalid_query:'本次声音特征无效'};
    document.getElementById('vt-status').textContent=r.matched?'匹配成功：识别为这份档案。':r.matched_name?'未匹配这份档案，实际匹配到「'+r.matched_name+'」。':'未匹配：'+(reason[r.reason]||'本次未能确认身份')+'。';
    const score=v=>v==null?'无':v.toFixed(4);
    document.getElementById('vt-result').textContent=`本档案分数 ${score(r.selected_score)}；最高分 ${score(r.top_score)} / 门槛 ${r.threshold}；次高分 ${score(r.second_score)}，分差 ${score(r.score_gap)} / 要求 ${r.required_margin}；有效语音 ${r.speech_seconds} 秒。`;
    document.getElementById('vt-start').textContent='再测一次';
  }
}
async function voiceTestRequest(action){
  if(vtBusy)return;
  vtBusy=true;vtRevision++;
  const dialog=document.getElementById('voice-profile-test');
  dialog.querySelectorAll('button').forEach(b=>b.disabled=true);
  if(action==='begin'||action==='restart'){vtEvent=null;document.getElementById('vt-result').textContent='';document.getElementById('vt-feedback').hidden=true;}
  if(action==='stop')document.getElementById('vt-status').textContent='正在比对，请稍候…';
  try{
    const r=await api('/dev/api/voice-test',{method:'POST',body:JSON.stringify({action,token:vtToken,member_id:vtMember})});
    if(r.status!==200){
      if(r.body.state)renderVoiceTest(r.body.state);
      if([403,409,503].includes(r.status)){vtToken=null;clearInterval(vtTimer);renderVoiceTest({token:null,recording:false});}
      if(!vtToken)clearInterval(vtTimer);
      document.getElementById('vt-status').textContent=r.body.error||'连接失败，请重试。';return;
    }
    if(r.body.cancelled){vtToken=null;clearInterval(vtTimer);return;}
    renderVoiceTest(r.body);
    clearInterval(vtTimer);
    if(vtToken)vtTimer=setInterval(pollVoiceTest,1000);
  }finally{vtBusy=false;dialog.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
async function pollVoiceTest(){
  if(!vtToken||vtBusy||vtPolling)return;
  vtPolling=true;const revision=vtRevision;
  try{
    const r=await api('/dev/api/voice-test',{method:'POST',body:JSON.stringify({action:'status',token:vtToken})});
    if(vtBusy||revision!==vtRevision)return;
    if(r.status===200)renderVoiceTest(r.body);
    else{if([403,409,503].includes(r.status)){vtToken=null;clearInterval(vtTimer);renderVoiceTest({token:null,recording:false});}document.getElementById('vt-status').textContent=r.body.error||'连接失败，请重试。';}
  }finally{vtPolling=false;}
}
async function closeVoiceTest(){
  if(vtBusy)return;
  if(vtToken)await voiceTestRequest('cancel');
  if(vtToken)return;
  clearInterval(vtTimer);document.getElementById('voice-profile-test').close();
}
document.getElementById('voice-profile-test').addEventListener('cancel',e=>{e.preventDefault();closeVoiceTest();});
window.addEventListener('pagehide',()=>{
  if(vtToken)fetch('/dev/api/voice-test',{method:'POST',keepalive:true,headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'cancel',token:vtToken})}).catch(()=>{});
});

async function voiceTestFeedback(decision){
  if(vtBusy||!vtEvent)return;
  vtBusy=true;
  try{
    const r=await api('/dev/api/identity-matches',{method:'POST',body:JSON.stringify({confirm:true,event_id:vtEvent,decision,
      member_id:decision==='corrected'?Number(document.getElementById('vt-actual-member').value):vtMember})});
    document.getElementById('vt-status').textContent=r.status===200?'反馈已保存，声纹模板没有改动。':r.body.error||'提交失败，可重试。';
    if(r.status===200){vtEvent=null;document.getElementById('vt-feedback').hidden=true;}
  }finally{vtBusy=false;}
}
async function loadIdentityMatches(){
  const root=document.getElementById('identity-match-records');
  const r=await api('/dev/api/identity-matches');root.replaceChildren();
  if(r.status!==200){root.textContent=r.body.error||'读取失败';return;}
  const names=new Map(r.body.members.map(m=>[m.id,m.name]));
  for(const e of r.body.events){
    const p=document.createElement('p'),d=e.details,top=e.scores[0];
    const f=e.feedback;
    p.textContent=new Date(e.created*1000).toLocaleString()+' · '+(e.source==='profile_test'?'档案测试':e.source==='continuity'?'声音连续性':'真实聊天')+
      (e.source==='continuity'?' · 单段 '+Number(d.continuity_score).toFixed(4)+' · 加权 '+d.continuity_average:' · 最高分 '+(top?top.score.toFixed(4):'无')+' / 门槛 '+d.threshold+' · '+d.reason)+
      ' · '+(f?({confirmed:'用户确认',rejected:'用户否认',corrected:'用户纠正'}[f.decision])+(f.member_id?'：'+(names.get(f.member_id)||'已删除成员'):''):'未确认')+
      ' · '+e.id.slice(0,8);
    root.append(p);
  }
  if(!r.body.events.length)root.textContent='尚无匹配记录。';
}
document.getElementById('identity-match-details').addEventListener('toggle',e=>{if(e.target.open)loadIdentityMatches();});

let identityPrivacyPolling=false;
setInterval(async()=>{
  const details=document.getElementById('identity-match-details'),dialog=document.getElementById('voice-profile-test');
  if(identityPrivacyPolling||document.hidden||(!details.open&&!dialog.open))return;
  identityPrivacyPolling=true;
  try{
    const r=await api('/dev/api/status');
    if(r.status===200&&r.body.privacy_on){
      document.getElementById('identity-match-records').replaceChildren();
      document.getElementById('vt-result').textContent='';
      document.getElementById('vt-feedback').hidden=true;vtEvent=null;
    }
  }finally{identityPrivacyPolling=false;}
},2000);
