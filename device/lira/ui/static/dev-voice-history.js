'use strict';
document.addEventListener('DOMContentLoaded',()=>{
  const $=id=>document.getElementById(id), tile=$('tile-voice-history');
  if(!tile)return;
  let busy=false, members=[], current=null, privacyPolling=false;
  function error(r){$('vh-status').textContent=r.body.error || '请求失败，请重试。';
    if(r.status===403){$('vh-turns').replaceChildren();$('vh-export').hidden=true;}}
  function detailBlock(title,value){
    const d=document.createElement('details'),summary=document.createElement('summary');
    summary.textContent=title;d.className='vh-detail';d.append(summary);
    d.addEventListener('toggle',()=>{
      if(d.open&&!d.querySelector('pre')){
        const pre=document.createElement('pre');pre.textContent=JSON.stringify(value,null,2);d.append(pre);
      }
    });
    return d;
  }
  function render(data){
    current=data.session.id; const root=$('vh-turns'); root.replaceChildren();
    for(const turn of data.turns){
      const card=document.createElement('article');card.className='vc-card';
      const label=document.createElement('label'), check=document.createElement('input');
      check.type='checkbox';check.value=turn.id;check.className='vh-turn-check';
      const member=members.find(m=>m.id===data.links?.[turn.id]);
      label.append(check,document.createTextNode(new Date(turn.created*1000).toLocaleTimeString()+' · '+(turn.state==='completed'?'已完成':turn.state==='interrupted'?'被打断':'未完整结束')+' · '+(member?'已关联：'+member.name:'未关联')));
      const user=document.createElement('p'), assistant=document.createElement('p');
      user.textContent='用户：'+turn.user;assistant.textContent='小鹿：'+(turn.assistant||'（没有完整文字回复）');
      user.style.whiteSpace=assistant.style.whiteSpace='pre-wrap';
      user.style.overflowWrap=assistant.style.overflowWrap='anywhere';
      const diagnostic=(turn.diagnostics||[]).map(e=>({...e,local_time:new Date(e.created*1000).toLocaleString()})),matches=(turn.identity_matches||[]).map(e=>({...e,local_time:new Date(e.created*1000).toLocaleString(),scores:e.scores.map(r=>({...r,name:members.find(m=>m.id===r.member_id)?.name||'未知或已删除成员'}))}));
      const native=diagnostic.filter(e=>e.direction==='local'&&e.kind.startsWith('voiceprint.'));
      const progress=native.filter(e=>e.kind==='voiceprint.progress').at(-1)?.payload;
      const score=matches.filter(e=>e.source==='conversation').at(-1)?.details;
      const identityNote=document.createElement('p');identityNote.className='muted';
      identityNote.textContent='本轮声纹：'+(score?'最高分 '+(score.top_score==null?'无可用分数':Number(score.top_score).toFixed(4))+' / 门槛 '+score.threshold+'；'+score.reason:
        progress?progress.process.last_result:'本轮暂无已保存的声纹结果；展开查看已有证据')+
        '。记忆归属：'+(member?'人工关联给 '+member.name:'未关联，不自动认定为当前用户的个人记忆')+'。';
      const userDetail=detailBlock('用户明细 · 转写、声纹分数与识别过程',{
        transcript:turn.user,asr_confidence:'本地未计算；接口若提供，以transcription_events中的原始字段为准。声纹相似度不是转写置信度',
        voiceprint_matches:matches,local_process:native,
        transcription_events:diagnostic.filter(e=>e.kind.startsWith('conversation.item.input_audio_transcription.')),
        actual_speaker_note:'识别/口头确认状态只用于本次交流；与下方人工关联的记忆归属分别记录',
        missing_data:matches.length||native.length?null:'本轮没有已保存的声纹明细，不代表分数为0；旧记录可能未启用采集'
      });
      const contexts=new Map((data.context_diagnostics||[]).map(e=>[e.id,e]));
      const assistantDetail=detailBlock('小鹿明细 · 发送上下文、工具调用与豆包返回',{
        context_at_turn_start:(turn.context_event_ids||[]).map(id=>contexts.get(id)).filter(Boolean),
        context_note:turn.context_event_ids?.length?'引用已保存的会话配置；本轮新增上下文见sent_context_and_tool_results':'本轮之前的会话配置未采集或已超出保留范围，不代表没有发送',
        sent_context_and_tool_results:diagnostic.filter(e=>e.direction==='sent'),
        local_tool_decisions:diagnostic.filter(e=>e.direction==='local'&&e.kind.startsWith('tool.')),
        doubao_returned:diagnostic.filter(e=>e.direction==='received'&&!e.kind.startsWith('conversation.item.input_audio_transcription.')),
        transcript:turn.assistant,
        attribution:member?{source:'manual_link',member_id:member.id,name:member.name}:{source:'unassigned_device_history'},
        note:diagnostic.length?'展示LIRA实际发送和接口实际返回的数据；已发送不等于模型采用。云端内部上下文及推理不可见；不保存音频包，密钥和反馈凭据已脱敏。':'当时未采集原始接口明细，无法还原；后续新聊天开始记录。'
      });
      card.append(label,user,userDetail,assistant,identityNote,assistantDetail);root.append(card);
    }
    $('vh-export').hidden=false;$('vh-export').href='/dev/api/voice-history/'+current+'/export';
    $('vh-status').textContent=data.turns.length+' 轮对话，可展开每句明细。关联后可作为该成员的个人记忆；未经选择不会自动关联。';
  }
  async function detail(){
    const id=$('vh-session').value;if(!id){current=null;$('vh-turns').replaceChildren();$('vh-export').hidden=true;return;}
    const r=await api('/dev/api/voice-history/'+id);if(r.status===200)render(r.body);else error(r);
  }
  async function refresh(){
    if(busy)return;busy=true;
    try{
      const r=await api('/dev/api/voice-history');if(r.status!==200){error(r);return;}
      members=r.body.members||[];const old=$('vh-session').value;
      $('vh-session').replaceChildren(...r.body.sessions.map(s=>new Option(new Date(s.started*1000).toLocaleString()+` · ${s.turn_count} 轮`+(s.id===r.body.active_session_id?' · 进行中':''),s.id)));
      if([...$('vh-session').options].some(o=>o.value===old))$('vh-session').value=old;
      $('vh-member').replaceChildren(new Option('请选择成员',''),new Option('解除所选轮次的关联','unlink'),...members.map(m=>new Option(m.name,String(m.id))));
      if(!r.body.sessions.length)$('vh-status').textContent='还没有记录。上线前的旧聊天不会自动补齐。';
      await detail();
    }finally{busy=false;}
  }
  $('vh-refresh').addEventListener('click',refresh);
  $('vh-session').addEventListener('change',detail);
  $('vh-select-all').addEventListener('click',()=>{const boxes=[...document.querySelectorAll('.vh-turn-check')];const selected=boxes.every(b=>b.checked);boxes.forEach(b=>b.checked=!selected);});
  $('vh-link').addEventListener('click',async()=>{
    if(busy||!current)return;
    const value=$('vh-member').value,ids=[...document.querySelectorAll('.vh-turn-check:checked')].map(b=>Number(b.value));
    if(!value||!ids.length){$('vh-status').textContent='请先选择成员和需要关联的对话轮次。';return;}
    if(!confirm(value==='unlink'?'确认解除所选对话的个人记忆关联？':`确认将所选 ${ids.length} 轮对话作为「${$('vh-member').selectedOptions[0].text}」的个人记忆？请确认其中没有其他人的发言。`))return;
    busy=true;$('vh-link').disabled=true;
    try{const r=await api('/dev/api/voice-history/'+current,{method:'POST',body:JSON.stringify({confirm:true,member_id:value==='unlink'?null:Number(value),turn_ids:ids})});
      if(r.status===200){render(r.body);$('vh-status').textContent=value==='unlink'?'关联已解除。':'已关联。后续认出该成员并读取个人记忆时，可接续这些话题。';}else error(r);
    }finally{busy=false;$('vh-link').disabled=false;}
  });
  document.addEventListener('lira:tile-visibility',e=>{if(e.detail.id===tile.id&&e.detail.open)refresh();});
  setInterval(async()=>{
    if(tile.hidden||document.hidden||privacyPolling)return;
    privacyPolling=true;
    try{const r=await api('/dev/api/status');if(r.status===200&&r.body.privacy_on)error({status:403,body:{error:'隐私模式开启，已清空对话展示；关闭隐私后可刷新。'}});}
    finally{privacyPolling=false;}
  },2000);
});
