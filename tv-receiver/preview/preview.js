/* Isolated UI preview. No production API, model, microphone, or learning records. */
(()=>{'use strict';
if(new URLSearchParams(location.search).get('tv')==='1')document.body.classList.add('tv-only');
const $=id=>document.getElementById(id),recognition=['远近闻名','风景','山区','省份','秀丽','神奇','神仙','盘子','手指','巨石','伸手','都是','著名','抢走','形状','岩石'],words=['仙女','山顶','巨大','座位','闪光','小狗','形状'];
let state='waiting_start',phase='recognition',page=0,index=0,repeats=0,sequence=0,events=[],timer=null,lastPress=-Infinity;
const number=n=>['零','一','二','三','四','五','六','七','八','九','十'][n]||String(n),pageSize=()=>matchMedia('(max-width:800px)').matches?2:4;
let debug=false;try{debug=localStorage.getItem('lira-preview-debug')==='true';}catch{}$('debug-toggle').checked=debug;
function record(action){events.push(`#${++sequence} ${action} · ${state} · 播报 ${repeats}`);if(events.length>60)events.shift();render();}
function stop(){clearTimeout(timer);timer=null;}
function play(action='playback_started'){stop();phase='dictation';state='playing';record(action);timer=setTimeout(()=>{timer=null;repeats++;state='waiting_child';record('playback_completed');record('waiting_child');},1300);}
function finish(){stop();state='completed';record('task_completed');}
function guide(key,label){const box=document.createElement('div');box.className='guide';const strong=document.createElement('strong'),body=document.createElement('span');strong.textContent=key;body.textContent=label;box.append(strong,body);$('guides').append(box);}
function fitWords(){const node=$('words');if(node.hidden)return;const cellHeight=node.clientHeight/2-10;for(const cell of node.children)cell.style.fontSize=Math.max(32,Math.min(148,cellHeight*.85,cell.clientWidth/(cell.textContent.length*1.08)))+'px';}
function render(){
 const rec=phase==='recognition',active=state==='waiting_child'||state==='playing';let title=rec?'读一读，认识新朋友':`第${number(index+1)}个词，慢慢写`,subtitle=rec?'把这一屏的词语，大声读给自己听。':'写好了按右键。想再听一遍，就按确认键。',progress=rec?'词语认读小热身':`正在练习第${number(index+1)}个词 · 一共${number(words.length)}个词`;
 if(state==='waiting_start'){title='准备好小本子了吗？';subtitle='先读一读词语，再开始听写。';progress='读一读 · 听一听 · 写一写';}
 else if(state==='playing'){title='小耳朵，认真听';subtitle=`正在听写第${number(index+1)}个词，一共${number(words.length)}个词。`;}
 else if(state==='paused'){title='休息一下，等你回来';subtitle='准备好了，我们从刚才的地方继续。';}
 else if(state==='completed'){title='今天的练习完成啦！';subtitle='辛苦啦！把本子交给爸爸妈妈看看吧。';progress='读一读 · 听一听 · 写一写';}
 else if(state==='cancelled'){title='这次练习先到这里';subtitle='收好本子，想练习的时候再来找我。';progress='读一读 · 听一听 · 写一写';}
 $('title').textContent=title;$('subtitle').textContent=subtitle;$('progress').textContent=progress;$('stage').textContent=rec?'认读小热身':'听写小挑战';$('stage').classList.toggle('dictation',!rec);$('learning-card').classList.toggle('is-recognition',rec&&active);
 const showWords=rec&&active;$('words').hidden=!showWords;$('words').replaceChildren();if(showWords)for(const word of recognition.slice(page,page+pageSize())){const n=document.createElement('span');n.textContent=word;if(word.length>2)n.classList.add('long');$('words').append(n);}$('illustration').hidden=showWords;
 $('guides').replaceChildren();if(state==='waiting_start')guide(rec?'右键 →':'确认键','准备好，开始吧');else if(state==='paused')guide(rec?'右键 →':'确认键','准备好了，继续');else if(active){guide('← 左键',rec?'上一屏':'上一词，再听听');if(!rec)guide('确认键','没听清，再听一次');guide('右键 →',rec?(page+pageSize()>=recognition.length?'读完了，开始听写':'下一屏'):(index===words.length-1?'写好了，完成练习':'写好了，下一个'));}
 $('trail').replaceChildren();$('trail').hidden=rec||['waiting_start','completed','cancelled'].includes(state);if(!$('trail').hidden)for(let n=0;n<words.length;n++){const dot=document.createElement('i');if(n<=index)dot.classList.add('visited');if(n===index)dot.classList.add('current');$('trail').append(dot);}
 $('pause-hint').hidden=['completed','cancelled'].includes(state);$('debug-log').hidden=!debug;$('debug-log').textContent=debug?'调试模式 · 仅供家长排查\n'+events.slice(-3).join('\n'):'';$('center-button').disabled=rec||['completed','cancelled'].includes(state);
 requestAnimationFrame(fitWords);
}
function key(key,{held=false}={}){
 if(['completed','cancelled'].includes(state))return;
 if(key==='Escape'){stop();state=held?'cancelled':'paused';record(held?'cancelled':'paused');return;}
 if(phase==='recognition'){
  if(key==='Enter'||key==='ArrowUp'||key==='ArrowDown'||key==='ContextMenu')return;
  if(key==='ArrowRight'){
   if(state==='waiting_start'||state==='paused'){state='waiting_child';record('page_presented');return;}
   if(state==='waiting_child'){if(page+pageSize()<recognition.length){page+=pageSize();record('page_presented');}else{index=0;repeats=0;play();}}
  }else if(key==='ArrowLeft'&&state==='waiting_child'){page=Math.max(0,page-pageSize());record('navigated_previous');}
  return;
 }
 if(state==='paused'){if(key==='Enter'){record('resumed');play();}return;}
 if(key==='ArrowLeft'){index=Math.max(0,index-1);repeats=0;play('navigated_previous');}
 else if(key==='ArrowRight'){if(index===words.length-1)finish();else{index++;repeats=0;play();}}
 else if(key==='Enter')play('replayed');
}
function press(keyValue,held=false){if(['ArrowUp','ArrowDown','ContextMenu'].includes(keyValue)||phase==='recognition'&&keyValue==='Enter')return;const now=performance.now();if(keyValue!=='Escape'&&now-lastPress<350)return;lastPress=now;key(keyValue,{held});}
$('debug-toggle').addEventListener('change',()=>{debug=$('debug-toggle').checked;try{localStorage.setItem('lira-preview-debug',String(debug));}catch{}render();});
$('reset').onclick=()=>{stop();state='waiting_start';phase='recognition';page=0;index=0;repeats=0;sequence=0;events=[];lastPress=-Infinity;record('task_received');};
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>press(b.dataset.key));document.addEventListener('keydown',e=>{if(e.target.closest('textarea,select,input:not([type=checkbox])'))return;if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Enter','Escape','ContextMenu','F1'].includes(e.key)){e.preventDefault();if(e.repeat){if(e.key==='Escape')press('Escape',true);return;}press(e.key==='F1'?'ContextMenu':e.key);}});
window.addEventListener('storage',e=>{if(e.key==='lira-preview-debug'){debug=e.newValue==='true';$('debug-toggle').checked=debug;render();}});
new ResizeObserver(fitWords).observe($('learning-card'));window.preview={state:()=>({state,phase,page,index,repeats,sequence,debug,events:[...events]}),key:keyValue=>key(keyValue)};record('task_received');
})();
