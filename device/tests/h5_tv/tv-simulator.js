/* Test-only TV terminal. Pedagogy, words, facts, and task creation stay server-side. */
(() => {
'use strict';
let s=JSON.parse(localStorage.getItem('tc1-tv')||'null'), boot=crypto.randomUUID(), session=crypto.randomUUID();
let ctx,source,cancelAudio,generation=0,leaseUntil=0,remoteLock=!!s?.remote_pause_lock,interval=null,poller=null,watcher=null,queue=Promise.resolve(),busy=false,disposed=false;
let faultItem=null,clockOffset=0,waitSince=0,reminded=false,answerDrains=0,confirmAfter=0;
const logs=[],audioEvidence=[];
const current=()=>s?.task.phases[s.phase]?.items[s.index];
const phase=()=>s?.task.phases[s.phase];
function save(){if(s)localStorage.setItem('tc1-tv',JSON.stringify(s));}
function log(action){logs.push(`${s?.seq||0} · ${action} · ${s?.state||'idle'}`);if(logs.length>60)logs.shift();document.querySelector('#logs').textContent=logs.slice(-8).join('\n');}
function pages(){
 const box=document.querySelector('#recognition'),width=box.clientWidth-28,height=box.clientHeight-28;
 const font=Math.max(60,Math.min(104,Math.floor(width/3)-14));
 const columns=Math.max(1,Math.floor(width/(font+12))),rows=Math.max(1,Math.floor(height/(font*1.15+12)));
 return {font,count:columns*rows};
}
function render(){
 const node=document.querySelector('#recognition');node.replaceChildren();
 document.querySelector('#state').textContent=s?`任务 ${s.task.task_id.slice(0,12)} · ${phase()?.type==='recognition'?'认读':'默写'} · ${s.state}`:'等待连接';
 if(s&&phase()?.type==='recognition'){
  const layout=pages();s.pageEnd=Math.min(phase().items.length,s.index+layout.count);
  for(const item of phase().items.slice(s.index,s.pageEnd)){const n=document.createElement('span');n.textContent=item.text;n.style.fontSize=layout.font+'px';node.append(n);}
 }
 document.querySelector('#counter').textContent=s?`当前项 ${s.index+1} / ${phase()?.items.length||0} · 实际播报 ${s.repeats} 次`:'';
 save();
}
async function emit(action,command=null){
 s.seq++;s.revision++;
 const e={schema_version:2,task_id:s.task.task_id,content_hash:s.task.content_hash,event_id:`${boot}:${s.seq}`,run_epoch:s.epoch,sequence:s.seq,state_revision:s.revision,command_id:command,phase_id:phase()?.phase_id||null,item_id:current()?.item_id||null,action,state:s.state,actual_repeats:s.repeats,boot_id:boot,session_id:session,occurred_at:new Date().toISOString(),elapsed_ms:Math.floor(performance.now())};
 s.outbox.push(e);save();render();log(action);await flush();return e;
}
async function flush(){
 while(s?.outbox.length){const r=await fetch('/sim/event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(s.outbox[0])});if(!r.ok)throw Error('event persistence rejected');s.outbox.shift();save();}
}
function stop(){generation++;if(interval){clearTimeout(interval);interval=null;}if(source){source.onended=null;try{source.stop();}catch{}source=null;}if(cancelAudio){cancelAudio(false);cancelAudio=null;}}
async function audio(raw,kind,item){
 if(!ctx||ctx.state!=='running')throw Error('audio context not running');
 const view=new DataView(raw),buffer=ctx.createBuffer(1,raw.byteLength/2,24000),data=buffer.getChannelData(0);
 for(let i=0;i<data.length;i++)data[i]=view.getInt16(i*2,true)/32768;
 const local=generation;return await new Promise((resolve,reject)=>{
  const n=ctx.createBufferSource();source=n;cancelAudio=resolve;n.buffer=buffer;n.connect(ctx.destination);
  n.onended=()=>{cancelAudio=null;if(local!==generation)return resolve(false);source=null;audioEvidence.push({kind,item,callback:'AudioBufferSourceNode.onended',frames:buffer.length,at:performance.now()});resolve(true);};
  try{n.start();}catch(e){reject(e);}
 });
}
async function play(count,command=null){
 stop();const local=generation;s.state='playing';await emit('playback_started',command);
 const item=current();
 try{
  for(let n=0;n<count;n++){
   if(local!==generation)return;
   const response=await fetch(faultItem===item.item_id?'/sim/asset/intentional-missing':`/sim/asset/${encodeURIComponent(item.audio_asset_id)}`);
   if(!response.ok)throw Error('asset fetch failed');
   const ended=await audio(await response.arrayBuffer(),'dictation',item.item_id);
   if(!ended||local!==generation)return;
   s.repeats++;await emit('playback_completed');
   if(n<count-1)await new Promise(resolve=>{interval=setTimeout(()=>{interval=null;resolve();},s.task.playback.interval_ms);});
  }
  if(local!==generation)return;s.state='waiting_child';confirmAfter=performance.now()+350;waitSince=Date.now()+clockOffset;reminded=false;await emit('waiting_child');
 }catch(e){if(local!==generation)return;stop();s.state='failed_item';await emit('item_failed');}
}
async function enter(command=null){
 if(s.phase>=s.task.phases.length){s.phase=s.task.phases.length-1;s.index=s.task.phases[s.phase].items.length-1;s.state='completed';await emit('task_completed',command);return;}
 if(phase().type==='recognition'){
  s.state='presenting';render();await emit('page_presented',command);s.state='waiting_child';confirmAfter=performance.now()+350;waitSince=Date.now()+clockOffset;reminded=false;await emit('waiting_child');
 }else void play(s.task.playback.repeat_count,command);
}
async function act(action,command=null,external=false){
 if(!s||['completed','cancelled'].includes(s.state))return {ok:false};
 if(['start','resume','replay','confirm'].includes(action)&&Date.now()>=leaseUntil){if(s.state!=='paused'){stop();s.state='paused';await emit('paused');}return {ok:false,reason:'fresh lease required'};}
 if(action==='pause'){
  stop();remoteLock=remoteLock||external;s.remote_pause_lock=remoteLock;s.state='paused';await emit('paused',command);return {ok:true};
 }
 if(action==='resume'&&remoteLock&&!external){await emit('resume_requested');return {ok:true};}
 if(action==='start'&&s.state==='waiting_start'||action==='resume'&&s.state==='paused'){
  stop();remoteLock=false;s.remote_pause_lock=false;if(action==='resume'){s.state='presenting';await emit('resumed',command);}await enter(action==='start'?command:null);return {ok:true};
 }
 if(action==='replay'&&['waiting_child','failed_item'].includes(s.state)&&phase().type==='dictation'){
  const count=s.state==='failed_item'?s.task.playback.repeat_count:1;s.state='presenting';await emit('replayed',command);void play(count);return {ok:true};
 }
 if(action==='confirm'&&s.state==='waiting_child'){
  if(performance.now()<confirmAfter)return {ok:false,reason:'duplicate confirmation guard'};
  const end=phase().type==='recognition'?s.pageEnd:s.index+1;
  while(s.index<end){await emit('item_confirmed',command);s.confirmed.push(current().item_id);s.index++;}
  if(s.index>=phase().items.length){s.phase++;s.index=0;}
  s.repeats=0;await enter();return {ok:true};
 }
 if(action==='cancel'){stop();s.state='cancelled';await emit('cancelled',command);return {ok:true};}
 return {ok:false,state:s.state};
}
async function dispatch(row){
 let result={ok:true};
 if(row.action==='start'){
  if(!s){const p=row.payload;leaseUntil=Date.now()+15000;s={task:p.task,epoch:p.run_epoch,state:'waiting_start',phase:0,index:0,seq:0,revision:0,repeats:0,confirmed:[],outbox:[]};await emit('task_received');}
  else if(s.task.content_hash!==row.payload.task.content_hash)throw Error('immutable snapshot mismatch');
 }else if(row.action==='lease')leaseUntil=Date.now()+row.payload.lease_ms;
 else if(row.action==='control'){
  const p=row.payload;if(p.expected_state_revision!==s.revision)result={ok:false,state:'stale'};
  else result=await act(p.action,p.command_id,true);
 }
 await fetch(`/sim/reply/${row.id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(result)});
}
document.querySelector('#unlock').onclick=async()=>{ctx=ctx||new AudioContext({sampleRate:24000});await ctx.resume();document.querySelector('#unlock').textContent='测试音频已启用';};
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>{queue=queue.then(()=>act(b.dataset.action)).catch(e=>log('操作失败'));});
poller=setInterval(async()=>{if(busy||disposed)return;busy=true;try{const p=await (await fetch('/sim/poll')).json();for(const row of p.commands)await dispatch(row);}catch{}finally{busy=false;}},80);
watcher=setInterval(()=>{
 if(!s||disposed)return;
 if(leaseUntil&&Date.now()>leaseUntil&&!['paused','waiting_start','completed','cancelled'].includes(s.state)){leaseUntil=0;queue=queue.then(()=>act('pause'));}
 if(s.state==='waiting_child'&&!reminded&&Date.now()+clockOffset-waitSince>=60000){reminded=true;log('等待确认提醒');}
},100);
window.addEventListener('pagehide',()=>{stop();save();});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&s&&!['completed','cancelled','waiting_start'].includes(s.state))queue=queue.then(()=>act('pause'));});
window.tv={
 state:()=>s?{state:s.state,item_id:current()?.item_id,phase_type:phase()?.type,index:s.index,phase:s.phase,repeats:s.repeats,confirmed:[...s.confirmed],seq:s.seq,logs:[...logs],audioEvidence:[...audioEvidence],answerDrains,reminded,pageEnd:s.pageEnd,recognitionPageSize:pages().count,remoteLock,canConfirm:performance.now()>=confirmAfter}:null,
 action:(a)=>{queue=queue.then(()=>act(a));return queue;},
 failItem:(id)=>{faultItem=id;},advanceIdle:(ms)=>{clockOffset+=ms;},
 answer:async(base64)=>{const raw=Uint8Array.from(atob(base64),c=>c.charCodeAt(0)).buffer;const ok=await audio(raw,'device_answer',current()?.item_id);if(ok)answerDrains++;return ok;},
 shutdown:async()=>{disposed=true;stop();clearInterval(poller);clearInterval(watcher);if(ctx)await ctx.close();return {timers:0,audioActive:!!source,pendingInputs:0};}
};
render();
if(s&&!['completed','cancelled'].includes(s.state)){s.state='paused';queue=queue.then(async()=>{await flush();await emit('paused');});}
})();
