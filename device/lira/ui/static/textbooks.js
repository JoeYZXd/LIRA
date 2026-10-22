(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const base = '/dev/api/textbooks';
  let draft = null, dirty = false, busy = false, timer = null, generation = 0;
  let selectedLesson = 0;
  let activeTab = 'results';
  let returnPoint = null;
  window.liraTeachingEditState = () => ({dirty: dirty, busy});
  const states = {uploaded: '已上传，等待识别', recognizing: '正在识别，请稍候…', review: '待核对', failed: '识别未完成，可重试或手动整理', published: '已保存为教材'};
  function message(text, error = false) { $('notice').textContent = text; $('notice').classList.toggle('error', error); }
  async function api(path, options = {}) {
    const response = await fetch(base + path, {credentials: 'same-origin', ...options});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401) $('login-link').hidden = false;
      const standard = {401: '登录已过期，请重新登录。当前编辑内容仍保留在此页。', 403: '操作被拒绝。请检查登录状态及设备隐私模式。', 409: '草稿已被其他操作更新。当前编辑内容仍保留，请先复制需要保留的内容，再重新打开草稿。'};
      throw new Error(standard[response.status] || (typeof body.error === 'string' ? body.error : typeof body.detail === 'string' ? body.detail : '操作失败，请稍后重试。'));
    }
    return body;
  }
  const json = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  function changed() { dirty = true; renderDirectory(); renderPageLinks(); controls(); }
  function controls() {
    const locked = busy || draft?.status === 'recognizing' || draft?.status === 'published';
    $('edit-fields').disabled = Boolean(busy || draft?.status === 'recognizing');
    $('edit-fields').querySelectorAll('input,textarea,select').forEach(input => { input.disabled = Boolean(locked && !['source-page','cover-page'].includes(input.id)); });
    for (const id of ['save','recognize','add-lesson']) $(id).disabled=Boolean(locked);
    $('lessons').querySelectorAll('button').forEach(button=>{button.disabled=Boolean(locked);});
    $('upload-button').disabled = busy;
    const reviewed = draft?.lessons?.filter(row => row.reviewed).length || 0;
    $('review-count').textContent = `已核对 ${reviewed} / ${draft?.lessons?.length || 0} 课；保存核对的课次即可用于默写；修改后需重新核对。`;
    $('publish').disabled = Boolean(locked || !draft?.title?.trim() || !draft?.lessons?.length || reviewed !== draft.lessons.length);
    $('dirty-state').textContent = dirty ? '有未保存的修改' : '草稿已同步';
    const order = lessonOrder(), position = order.indexOf(selectedLesson);
    $('previous-lesson').disabled = busy || position <= 0;
    $('next-lesson').disabled = busy || position < 0 || position >= order.length - 1;
    $('review-lesson').disabled = Boolean(locked || !draft?.lessons?.length);
    $('revise-book').disabled = busy;
    $('lesson-directory').querySelectorAll('button').forEach(button=>{button.disabled=busy || draft?.status==='recognizing';});
    const coverLocked=busy || draft?.status==='recognizing' || !draft?.pages?.length;
    for(const id of ['set-page-cover','save-cover']) $(id).disabled=Boolean(coverLocked || (draft?.status==='published' && !Number.isInteger(draft.source_revision)));
    $('save-page-links').disabled=Boolean(locked || !draft?.pages?.length);
    const number=Number($('source-page').value);
    $('previous-page').disabled=busy || number<=1;
    $('next-page').disabled=busy || number>=draft?.pages?.length || !draft?.pages?.length;
    $('open-upload').disabled=busy;
    $('return-source-page').hidden=!returnPoint || returnPoint.bookId!==draft?.id;
    $('return-source-page').textContent=returnPoint?`返回原书页 · 第 ${returnPoint.number} 页`:'返回原书页';
    $('return-source-page').disabled=busy;
    for(const button of document.querySelectorAll('#page-thumbnails button,#page-linked-lessons button')) button.disabled=busy || draft?.status==='recognizing';
    $('review-summary').replaceChildren(...(draft?.lessons||[]).map((row,index)=>{
      const button=node('button',`${row.unit ?? '?'} 单元 · 第 ${row.number ?? '?'} 课 · ${row.title || '未填写标题'}`,'review-result-row');
      button.append(reviewBadge(row.reviewed || draft.status==='published'));
      button.type='button';button.disabled=busy;button.onclick=()=>{selectLesson(index);showTab('lessons',true);};return button;
    }));
  }
  async function action(fn) {
    if (busy) return;
    busy = true; controls();
    try { await fn(); } catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  }
  function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
  function reviewBadge(reviewed, extraClass = '') {
    return node('span', reviewed ? '已核对' : '待核对', `review-status review-status--${reviewed ? 'reviewed' : 'pending'} ${extraClass}`.trim());
  }
  async function library() {
    const data = await api(''); $('library').replaceChildren();
    if (!data.books.length && !data.drafts.length) $('library').textContent = '还没有教材。上传课本后即可开始。';
    const published = new Set(data.books.map(book => book.book_id));
    const drafts = new Map(data.drafts.map(item => [item.id, item]));
    function bookCard(item, isBook) {
      const id = isBook ? item.book_id : item.id;
      const meta = isBook ? drafts.get(id) : item;
      const button = node('button', undefined, 'book-card'); button.type = 'button';
      const cover = node('span', undefined, 'book-cover');
      const fallback = node('strong', item.title || '未命名课本', 'book-cover-name');
      cover.append(fallback);
      if (Number.isInteger(meta?.cover_page) && meta.cover_page > 0) {
        const image = document.createElement('img'); image.alt = '';
        image.loading = 'lazy'; image.src = base + '/drafts/' + encodeURIComponent(id) + '/pages/' + meta.cover_page;
        image.onload = () => { fallback.hidden = true; }; image.onerror = () => { image.remove(); fallback.hidden = false; };
        cover.append(image);
      }
      button.setAttribute('aria-label',(item.title || '未命名课本')+' · '+(isBook?'已核对':'继续核对'));
      cover.append(reviewBadge(isBook,'book-state'));
      button.append(cover);
      button.onclick = () => {
        if (busy || (dirty && !window.confirm('当前草稿有未保存的修改，确定放弃并打开另一份课本吗？'))) return;
        action(async () => {
          selectedLesson = 0;
          activeTab = 'results';
          if (isBook) {
            const result = await api('/books/' + encodeURIComponent(id));
            setDraft({...result.book, lessons:result.book.lessons.map(row=>({...row,reviewed:true})), status: 'published', revision: result.book.source_revision ?? 0});
          } else setDraft((await api('/drafts/' + encodeURIComponent(id))).draft);
          $('editor-heading').focus({preventScroll:true});
        });
      };
      $('library').append(button);
    }
    data.books.forEach(book => bookCard(book, true));
    data.drafts.filter(item => !published.has(item.id)).forEach(item => bookCard(item, false));
  }
  function showPage() {
    const page = draft?.pages.find(p => p.number === Number($('source-page').value));
    $('page-image').hidden = !page; $('page-link').hidden = !page;
    $('empty-pages').hidden=!!page;
    document.querySelector('.page-browser').hidden=!page;
    for(const button of $('page-thumbnails').querySelectorAll('button')) button.setAttribute('aria-current',Number(button.dataset.page)===page?.number?'page':'false');
    renderPageLinks();
    if (!page) { $('page-text').textContent = ''; return; }
    // Construct the authenticated URL locally; never trust an OCR/model supplied URL.
    const url = base + '/drafts/' + encodeURIComponent(draft.id) + '/pages/' + page.number;
    if ($('page-image').getAttribute('src') !== url) $('page-image').src = url;
    $('page-image').alt = `课本原始第 ${page.number} 页`;
    $('page-link').href = url; $('page-text').textContent = page.text || '此页暂无识别文字。';
    $('page-unit').value=page.unit ?? '';
    $('page-lessons').replaceChildren(...lessonOrder().map(index=>{
      const row=draft.lessons[index], option=new Option(`${row.unit ?? '?'} 单元 | 第 ${row.number ?? '?'} 课 ${row.title || ''}`,String(index));
      option.selected=(row.source_pages||[]).includes(page.number);return option;
    }));
    $('page-cover-state').textContent=draft.cover_page===page.number?'当前封面':'';
    $('page-association-state').textContent=draft.status==='published'?'修改关联请先建立修订草稿。':'';
  }
  function renderThumbnails(reset=false) {
    const list=$('page-thumbnails'),top=reset?0:list.scrollTop,left=reset?0:list.scrollLeft;
    list.replaceChildren(...draft.pages.map(page=>{
      const button=node('button',undefined,'page-thumbnail');button.type='button';button.dataset.page=page.number;
      button.setAttribute('aria-label',`查看第 ${page.number} 页原页`);
      const image=document.createElement('img');image.alt='';image.loading='lazy';image.decoding='async';
      image.src=base+'/drafts/'+encodeURIComponent(draft.id)+'/pages/'+page.number+'/thumbnail';
      image.onerror=()=>{image.hidden=true;button.classList.add('thumbnail-unavailable');};
      button.append(image,node('span',`第 ${page.number} 页`));button.onclick=()=>choosePage(page.number);
      button.onkeydown=event=>{
        const offset=['ArrowRight','ArrowDown'].includes(event.key)?1:['ArrowLeft','ArrowUp'].includes(event.key)?-1:0;
        if(!offset&&event.key!=='Home'&&event.key!=='End')return;
        event.preventDefault();const index=draft.pages.findIndex(row=>row.number===page.number);
        const next=event.key==='Home'?draft.pages[0]:event.key==='End'?draft.pages.at(-1):draft.pages[Math.max(0,Math.min(draft.pages.length-1,index+offset))];
        choosePage(next.number);const target=list.querySelector(`[data-page="${next.number}"]`);target.focus({preventScroll:true});target.scrollIntoView({block:'nearest',inline:'nearest'});
      };
      return button;
    }));list.scrollTop=top;list.scrollLeft=left;
  }
  function choosePage(number) {
    if(busy||!draft?.pages.some(page=>page.number===number))return;
    returnPoint=null;$('source-page').value=number;showPage();controls();
  }
  function renderPageLinks() {
    const number=Number($('source-page').value),page=draft?.pages.find(row=>row.number===number);
    const linked=lessonOrder().filter(index=>(draft.lessons[index].source_pages||[]).includes(number));
    const units=[...new Set([page?.unit,...linked.map(index=>draft.lessons[index].unit)].filter(value=>Number.isInteger(value)))];
    $('page-linked-units').textContent=units.length?units.map(unit=>`第 ${unit} 单元`).join(' · '):'未关联单元';
    $('page-linked-lessons').replaceChildren(...linked.map(index=>{
      const row=draft.lessons[index],button=node('button',`${row.unit ?? '?'} 单元 · 第 ${row.number ?? '?'} 课 ${row.title || '未填写标题'}`);
      button.type='button';button.onclick=()=>{
        if(busy)return;
        returnPoint={bookId:draft.id,number,scrollTop:$('page-thumbnails').scrollTop,scrollLeft:$('page-thumbnails').scrollLeft};
        selectLesson(index,true);showTab('lessons',true);controls();
      };return button;
    }));
    if(!linked.length)$('page-linked-lessons').append(node('p','未关联课次','muted'));
  }
  function renderLessons() {
    $('lessons').replaceChildren();
    selectedLesson = Math.max(0,Math.min(selectedLesson, draft.lessons.length - 1));
    renderDirectory();
    if (!draft.lessons.length) { $('lessons').append(node('p', '还没有课次，可手动添加课文或识别课本。')); controls(); return; }
    const index = selectedLesson, row = draft.lessons[index];
      const card = node('article', undefined, 'lesson'); card.append(node('h4', `第 ${row.number ?? '待填写'} 课 · ${row.title || '未填写标题'}`));
      const checkLabel = node('label', undefined, 'review-check');
      const check = document.createElement('input'); check.type = 'checkbox'; check.checked = Boolean(row.reviewed);
      check.onchange = () => { row.reviewed = check.checked; changed(); };
      checkLabel.append(check, node('span', '我已对照原页核对本课字表和分类'));
      function edit(key, title, type = 'text') {
        const label = node('label', title), input = document.createElement(type === 'textarea' ? 'textarea' : 'input');
        if (type !== 'textarea') input.type = type;
        if (key==='title') input.maxLength=160;
        if (key==='expansions') input.maxLength=10000;
        if (key==='text') { input.maxLength=100000; input.rows=6; }
        if (type === 'number') { input.min = '1'; input.step = '1'; }
        input.value = Array.isArray(row[key]) ? row[key].join(' ') : (row[key] ?? '');
        input.oninput = () => {
          if (key === 'four' || key === 'two') row[key] = Array.from(input.value.replace(/[\s,，、;；]/g, ''));
          else row[key] = type === 'number' ? (input.value === '' ? null : Number(input.value)) : input.value;
          row.reviewed = false; check.checked = false; changed();
        };
        label.append(input); return label;
      }
      const numbers = node('div', undefined, 'lesson-numbers'); numbers.append(edit('unit', '单元', 'number'), edit('number', '课次', 'number')); card.append(numbers);
      card.append(edit('title', '课文标题'), edit('four', '四会字', 'textarea'), edit('two', '二会字', 'textarea'));
      card.append(edit('expansions', '扩词', 'textarea'), edit('text', '背诵内容（选填）', 'textarea'));
      const pagesLabel = node('label', '原书来源页（可按 Ctrl / Command 多选）'), select = document.createElement('select'); select.multiple = true; select.size = Math.min(4, Math.max(2, draft.pages.length));
      draft.pages.forEach(page => { const option = node('option', `第 ${page.number} 页`); option.value = page.number; option.selected = (row.source_pages || []).includes(page.number); select.append(option); });
      select.onchange = () => { row.source_pages = Array.from(select.selectedOptions, o => Number(o.value)); row.reviewed = false; check.checked = false; changed(); if (row.source_pages.length) { $('source-page').value = row.source_pages[0]; showPage(); } };
      pagesLabel.append(select); card.append(pagesLabel, checkLabel);
      const remove = node('button', '删除本课', 'small'); remove.type = 'button';
      remove.onclick = () => { if (!window.confirm(`确定删除第 ${row.number || index + 1} 课吗？`)) return; draft.lessons.splice(index, 1); selectedLesson = Math.max(0,index - 1); changed(); renderLessons(); };
      card.append(remove); $('lessons').append(card);
    controls();
  }
  function lessonOrder() { return (draft?.lessons || []).map((_,i)=>i).sort((a,b)=>(draft.lessons[a].unit ?? 1000)-(draft.lessons[b].unit ?? 1000)||(draft.lessons[a].number ?? 1000)-(draft.lessons[b].number ?? 1000)); }
  function renderDirectory() {
    $('lesson-directory').replaceChildren(); let group=null, unit;
    for (const index of lessonOrder()) {
      const row=draft.lessons[index];
      if (!group || unit!==row.unit) { unit=row.unit; group=node('div',undefined,'lesson-unit'); group.append(node('h4',unit==null?'待填写单元':`第 ${unit} 单元`)); $('lesson-directory').append(group); }
      const button=node('button',`第 ${row.number ?? '?'} 课 · ${row.title || '未填写标题'}`);
      button.append(reviewBadge(row.reviewed || draft.status==='published'));
      button.type='button'; button.disabled=busy; button.setAttribute('aria-current',String(index===selectedLesson)); button.onclick=()=>selectLesson(index); group.append(button);
    }
  }
  function selectLesson(index,preserveSource=false) { selectedLesson=index; const source=draft.lessons[index]?.source_pages?.[0]; if(source&&!preserveSource){$('source-page').value=source;showPage();} renderLessons(); }
  function setDraft(value) {
    const sameBook=draft?.id===value.id;
    if(!sameBook)returnPoint=null;
    clearTimeout(timer); generation++; draft = value; draft.lessons ||= []; draft.pages ||= []; dirty = false;
    $('editor').hidden = false; $('book-title').value = draft.title || '';
    document.querySelector('.source-pane').hidden = !draft.pages.length;
    $('library-section').hidden = true; $('upload-panel').hidden = true;
    $('editor-heading').textContent = draft.title || '课本目录与核对';
    $('revise-book').hidden = draft.status !== 'published';
    $('draft-state').textContent = `${states[draft.status] || draft.status} · ${draft.pages.length} 页`;
    $('warnings').replaceChildren(); (draft.warnings || []).forEach(w => $('warnings').append(node('li', w)));
    if (draft.error) $('warnings').append(node('li', draft.error));
    const selected = sameBook?$('source-page').value:''; $('source-page').replaceChildren();
    draft.pages.forEach(page => { const option = node('option', `第 ${page.number} 页`); option.value = page.number; $('source-page').append(option); });
    if (draft.pages.some(p => String(p.number) === selected)) $('source-page').value = selected;
    $('cover-page').replaceChildren(new Option('无封面，显示课本名称',''), ...draft.pages.map(page=>new Option(`第 ${page.number} 页`,page.number)));
    $('cover-page').value = draft.cover_page ?? '';
    renderThumbnails(!sameBook);showPage(); renderLessons(); renderCover(); showTab(activeTab); controls();
    if (draft.status === 'recognizing') schedulePoll();
  }
  function schedulePoll() {
    const id = draft.id, version = generation;
    timer = setTimeout(async () => {
      try {
        const result = await api('/drafts/' + encodeURIComponent(id));
        if (draft.id !== id || generation !== version || dirty) return;
        if (busy || result.draft.revision === draft.revision) { schedulePoll(); return; }
        setDraft(result.draft);
        if (draft.status !== 'recognizing') { message(draft.status === 'failed' ? '识别未完成，已保留草稿，请查看原因。' : '识别完成，请逐课核对后保存。', draft.status === 'failed'); await library(); }
      } catch (error) { message(error.message, true); /* Leave an explicit refresh escape; avoid infinite authorization polls. */ $('draft-state').textContent = '状态读取失败；请从草稿列表重新打开以继续。'; }
    }, 2000);
  }
  async function saveDraft() {
    const result = await api('/drafts/' + encodeURIComponent(draft.id), json('PUT', {revision: draft.revision, title: draft.title, lessons: draft.lessons, cover_page: draft.cover_page ?? null,page_units:draft.pages.map(page=>({number:page.number,unit:page.unit ?? null}))}));
    setDraft(result.draft);
  }
  $('book-title').oninput = () => { draft.title = $('book-title').value; changed(); };
  $('source-page').onchange = () => choosePage(Number($('source-page').value));
  $('cover-page').onchange = () => { draft.cover_page = $('cover-page').value ? Number($('cover-page').value) : null; dirty=true;renderCover();showPage();controls(); };
  function showTab(name,focus=false) {
    const returning=name==='pages'&&activeTab==='lessons'&&returnPoint?.bookId===draft?.id;
    if(returning){$('source-page').value=returnPoint.number;showPage();}
    activeTab=name;
    for(const button of document.querySelectorAll('[data-book-tab]')){
      const selected=button.dataset.bookTab===name;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;
      $(button.getAttribute('aria-controls')).hidden=!selected;
    }
    if(focus)$('book-tab-'+name).focus();
    if(returning){
      controls();const point=returnPoint;
      requestAnimationFrame(()=>{if(returnPoint===point){$('page-thumbnails').scrollTop=point.scrollTop;$('page-thumbnails').scrollLeft=point.scrollLeft;}});
    }
  }
  const tabs=[...document.querySelectorAll('[data-book-tab]')];
  tabs.forEach((button,index)=>{
    button.onclick=()=>showTab(button.dataset.bookTab);
    button.onkeydown=event=>{
      const offset=event.key==='ArrowRight'?1:event.key==='ArrowLeft'?-1:0;
      if(offset||event.key==='Home'||event.key==='End'){
        event.preventDefault();const next=event.key==='Home'?tabs[0]:event.key==='End'?tabs.at(-1):tabs[(index+offset+tabs.length)%tabs.length];showTab(next.dataset.bookTab,true);
      }
    };
  });
  function renderCover(){
    const box=$('info-cover-preview');box.replaceChildren();
    if(draft.cover_page){const image=document.createElement('img');image.alt='课本封面';image.src=base+'/drafts/'+encodeURIComponent(draft.id)+'/pages/'+draft.cover_page;image.onerror=()=>box.replaceChildren(node('strong',draft.title||'未命名课本'));box.append(image);}
    else box.append(node('strong',draft.title||'未命名课本'));
  }
  async function saveCover(number){
    if(draft.status==='published'){
      const result=await api('/books/'+encodeURIComponent(draft.id)+'/cover',json('PUT',{revision:draft.revision,cover_page:number}));
      setDraft({...result.book,lessons:result.book.lessons.map(row=>({...row,reviewed:true})),status:'published',revision:result.book.source_revision});
    }else{draft.cover_page=number;dirty=true;await saveDraft();}
    message(number?'封面已保存。':'已移除封面。');await library();
  }
  $('set-page-cover').onclick=()=>action(()=>saveCover(Number($('source-page').value)));
  $('save-cover').onclick=()=>action(()=>saveCover($('cover-page').value?Number($('cover-page').value):null));
  $('previous-page').onclick=()=>choosePage(Number($('source-page').value)-1);
  $('next-page').onclick=()=>choosePage(Number($('source-page').value)+1);
  $('return-source-page').onclick=()=>showTab('pages',true);
  $('page-unit').oninput=()=>{const page=draft.pages.find(row=>row.number===Number($('source-page').value));page.unit=$('page-unit').value?Number($('page-unit').value):null;changed();};
  $('page-lessons').onchange=()=>{
    const number=Number($('source-page').value),selected=new Set([...$('page-lessons').selectedOptions].map(option=>Number(option.value)));
    draft.lessons.forEach((row,index)=>{const before=row.source_pages||[],after=before.filter(p=>p!==number);if(selected.has(index))after.push(number);if(before.includes(number)!==selected.has(index))row.reviewed=false;row.source_pages=after;});changed();renderLessons();
  };
  $('save-page-links').onclick=()=>action(async()=>{await saveDraft();message('本页关联已保存。');});
  $('open-upload').onclick=()=>{$('upload-panel').hidden=false;$('upload-panel').open=true;$('source-files').focus();};
  $('previous-lesson').onclick = () => { const order=lessonOrder(); selectLesson(order[order.indexOf(selectedLesson)-1]); };
  $('next-lesson').onclick = () => { const order=lessonOrder(); selectLesson(order[order.indexOf(selectedLesson)+1]); };
  $('review-lesson').onclick = () => action(async()=>{
    const row=draft.lessons[selectedLesson];
    if (!row?.title?.trim() || !Number.isInteger(row.unit) || !Number.isInteger(row.number) || row.unit<1 || row.number<1) throw new Error('请先填写本课的单元、课次与标题。');
    const wasReviewed=row.reviewed; row.reviewed=true;dirty=true;
    try { await saveDraft(); } catch(error) { row.reviewed=wasReviewed;renderLessons();throw error; }
    message('本课已核对并保存，可用于默写，无需核对整本。'); await library();
  });
  $('back-library').onclick = () => {
    if (busy || (dirty && !window.confirm('有尚未保存的修改，确定放弃并返回课本列表吗？'))) return;
    clearTimeout(timer); generation++; draft=null;returnPoint=null; dirty=false; $('editor').hidden=true; $('library-section').hidden=false; $('upload-panel').hidden=true; message(''); action(library);
  };
  $('revise-book').onclick = () => action(async()=>{
    const origin=returnPoint,lesson=selectedLesson;
    const result=await api('/books/'+encodeURIComponent(draft.id)+'/revise',{method:'POST'});
    setDraft(result.draft);
    returnPoint=origin?{...origin,bookId:draft.id}:null;
    selectedLesson=Math.min(lesson,draft.lessons.length-1);renderLessons();
    message('已建立修订草稿。原教材保持不变，请逐课核对后另存。');await library();
  });
  $('add-lesson').onclick = () => { draft.lessons.push({number: Math.max(0, ...draft.lessons.map(r => r.number || 0)) + 1, unit: 1, title: '', four: [], two: [], expansions:'',text:'', source_pages: [], reviewed: false}); selectedLesson=draft.lessons.length-1; changed(); renderLessons(); };
  $('save').onclick = () => action(async () => { await saveDraft(); message('草稿已保存，尚未用于练习。'); await library(); });
  $('recognize').onclick = () => action(async () => {
    if (draft.lessons.length && !window.confirm('重新识别将替换当前课文和字表，并需要重新核对。是否继续？')) return;
    if (dirty) await saveDraft();
    const result = await api('/drafts/' + encodeURIComponent(draft.id) + '/recognize', {method: 'POST'}); setDraft(result.draft); message('识别已开始，可以稍后从草稿列表继续核对。');
  });
  $('publish').onclick = () => action(async () => {
    if (!window.confirm('确认整本课文均已核对并保存为教材？已保存核对的单课无需等待整本发布，即可用于默写。')) return;
    if (dirty) await saveDraft();
    const result = await api('/drafts/' + encodeURIComponent(draft.id) + '/publish', json('POST', {revision: draft.revision, confirmed: true}));
    draft.status = 'published'; $('draft-state').textContent = '已保存为教材'; message(`已保存“${result.book.title}”，现在可以按课次或单元练习。`); await library();
  });
  $('upload-form').onsubmit = event => {
    event.preventDefault();
    if (dirty && !window.confirm('当前草稿有未保存的修改，确定放弃并上传新课本吗？')) return;
    action(async () => {
      const files = Array.from($('source-files').files);
      if (!files.length) throw new Error('请先选择课本文件。');
      if (files.reduce((sum, file) => sum + file.size, 0) > 100 * 1024 * 1024) throw new Error('文件合计超过 100 MB，请拆分后上传。');
      if (files.length > 200 || files.some(file => !/\.(pdf|png|jpe?g)$/i.test(file.name))) throw new Error('请选择 PDF、PNG 或 JPEG，最多 200 个文件。');
      const form = new FormData(); files.forEach(file => form.append('files', file)); message('正在上传并准备页面…');
      const result = await api('/import', {method: 'POST', body: form}); setDraft(result.draft); $('source-files').value = ''; message('上传成功。点击“识别课本”开始整理，或手动填写课文。'); await library();
    });
  };
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  $('page-image').onerror = () => { $('page-image').hidden = true; message('原页预览加载失败，请通过原页链接重试或重新登录。', true); };
  action(library);
})();
