/* Card-local navigation. Existing authenticated editors remain isolated from
   console IDs/styles; no model capability is enabled by this navigation. */
(() => {
  'use strict';
  const root=document.getElementById('teaching-hub');
  if(!root)return;
  const $=id=>root.querySelector('#'+id), frame=$('teach-frame');
  let subject=null;
  const subjects={
    chinese:{name:'语文',intro:'整理课本与原文，再选择想练习的内容。',features:[
      ['books','课本列表与导入','查看现有课本、上传课本并对照原页核对。','可用','/dev/textbooks?embedded=1'],
      ['content','课文与诗词','手动录入或从课本选页建立原文，核对后确认版本。','可用','/dev/teaching?embedded=1&section=content'],
      ['dictation','默写与听写','二会字认读、四会字扩词默写，管理任务、流程与纸面核对。','可用','/dev/dictation?embedded=1'],
      ['recitation','背诵课文与诗词','孩子背完再反馈，主动求助时再给提示。','待验收',null,'使用家长确认的原文比对听到的内容，识别不确定时保留待核对状态。当前新增背诵能力尚未开放。']
    ]},
    math:{name:'数学',intro:'围绕知识点找讲解、做练习，也可以准备纸面练习。',features:[
      ['knowledge','知识点','按教材或主题整理知识点与关联内容。','待建设',null,'规划将知识点关联到教材章节、讲解和练习；年级、教材版本与内容来源仍需补齐。'],
      ['video','视频教学','找到适合当前知识点的讲解。','待建设',null,'规划提供有来源的视频与电视播放能力。视频资源、播放设备和内容核对尚未接入。'],
      ['exercises','课外题与练习','围绕知识点选择练习，并记录需要帮助的地方。','待建设',null,'规划整理有来源的练习题与参考解答；不凭一次答题自动认定掌握情况。题目来源和核对能力尚未建设。'],
      ['print','打印练习','把选好的题目交给家里的打印机。','待建设',null,'规划先预览题目，确认打印机、份数与页数后再打印。打印机发现、连接及打印结果查询尚未接入。']
    ]},
    english:{name:'英语',intro:'用孩子能理解的简单表达学词汇，并在对话中尝试使用。',features:[
      ['conversation','在线英文对话','围绕熟悉的话题练习简单英文。','待建设',null,'已有语音对话链路可作为基础；适龄话题、英文理解与纠错、内容边界仍需验证，当前没有开放专门的英语教学模式。'],
      ['word','每日一个单词','用最简单的话解释单词，再举一个生活例子。','待建设',null,'规划支持孩子选择学习一个词；每日学习是否提醒、何时提醒需要协商，当前不会自动安排或推送。'],
      ['review','复习最近的单词','回顾近期学过的词，并在简单对话中用一用。','待建设',null,'规划关联真实学习记录，区分学过、待复习和已经核对的结果；不将一次跟读自动记为掌握。词汇记录与复习证据尚未建设。']
    ]}
  };
  const common={reviews:['reviews','家长核对与修正','查看家庭成员的记录，保存纸面核对或背诵修正附注。','可用','/dev/teaching?embedded=1&section=reviews'],status:['status','能力状态','查看设备报告的可用状态、当前约定与实际结果。','可用','/dev/teaching?embedded=1&section=status']};
  function canLeave(){
    const state=frame.contentWindow?.liraTeachingEditState?.();
    if(state?.busy){$('teach-notice').textContent='正在保存或处理，请完成后再切换。';return false;}
    return !state?.dirty||window.confirm('有尚未保存的编辑，仍要离开吗？');
  }
  function clearFrame(){frame.hidden=true;frame.removeAttribute('src');$('teach-loading').hidden=true;}
  function focus(id){$(id).focus({preventScroll:true});}
  function home(){if(!canLeave())return;clearFrame();subject=null;$('teach-subjects').hidden=false;$('teach-subject-panel').hidden=true;$('teach-detail').hidden=true;$('teach-location').textContent='';$('teach-notice').textContent='';focus('teach-heading');}
  function select(id){
    if(!canLeave())return;
    clearFrame();subject=id;const data=subjects[id];
    $('teach-subjects').hidden=true;$('teach-subject-panel').hidden=false;$('teach-detail').hidden=true;$('teach-location').textContent=' / '+data.name;
    $('teach-subject-title').textContent=data.name;$('teach-subject-intro').textContent=data.intro;
    $('teach-features').replaceChildren(...data.features.map(feature=>{
      const b=document.createElement('button');b.type='button';b.className='teach-feature';
      for(const [tag,text,cls] of [['strong',feature[1],''],['span',feature[2],''],['span',feature[3],'teach-badge']]){const n=document.createElement(tag);n.textContent=text;n.className=cls;b.append(n);}
      b.addEventListener('click',()=>detail(feature));return b;
    }));$('teach-notice').textContent='';focus('teach-subject-title');
  }
  function detail(feature){
    if(!canLeave())return;clearFrame();$('teach-subjects').hidden=true;$('teach-subject-panel').hidden=true;$('teach-detail').hidden=false;
    $('teach-location').textContent=' / '+(subject?subjects[subject].name+' / ':'')+feature[1];
    $('teach-detail-title').textContent=feature[1];$('teach-description').textContent=feature[2];
    $('teach-planned').hidden=!!feature[4];$('teach-planned').textContent=feature[3]+' · '+(feature[5]||'');
    $('teach-back').textContent=subject?'返回'+subjects[subject].name:'返回全部科目';
    if(feature[4]){frame.title=feature[1];frame.src=feature[4];frame.hidden=false;$('teach-loading').hidden=false;}
    $('teach-notice').textContent='';focus('teach-detail-title');
  }
  frame.addEventListener('load',()=>{
    if(!frame.hasAttribute('src'))return;
    $('teach-loading').hidden=true;
    try{if(!['/dev/teaching','/dev/textbooks','/dev/dictation'].includes(frame.contentWindow.location.pathname)){$('teach-notice').textContent='登录可能已过期，请重新登录设备控制台。';}}
    catch{ $('teach-notice').textContent='暂时无法读取，请返回科目后重试。'; }
  });
  window.addEventListener('message',e=>{
    if(e.origin!==location.origin||e.source!==frame.contentWindow||e.data?.type!=='lira:teaching-size')return;
    const height=Number(e.data.height);if(Number.isFinite(height))frame.style.height=Math.min(16000,Math.max(400,height))+'px';
  });
  $('teach-home').addEventListener('click',home);$('teach-back').addEventListener('click',()=>subject?select(subject):home());
  root.querySelectorAll('[data-subject]').forEach(b=>b.addEventListener('click',()=>select(b.dataset.subject)));
  root.querySelectorAll('[data-common]').forEach(b=>b.addEventListener('click',()=>detail(common[b.dataset.common])));
  window.addEventListener('beforeunload',e=>{const state=frame.contentWindow?.liraTeachingEditState?.();if(state?.dirty||state?.busy){e.preventDefault();e.returnValue='';}});
})();
