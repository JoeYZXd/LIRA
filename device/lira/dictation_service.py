"""Shared trusted preparation boundary for management and model tools."""
import asyncio
import copy
import hashlib
import inspect
import json
from pathlib import Path
import re
import uuid
from .dictation_context import DictationContext
from .dictation_assets import pcm24, MAX_ASSET_BYTES, MAX_TASK_BYTES
from .dictation_repository import DictationRepository
from .dictation_results import DictationResults
from .dictation_settings import DictationSettings
from .dictation_task_contract import content_hash,validate_task
from .teaching_profile import TeachingProfile
from .teaching_skills import TeachingSkills

class DictationService:
    def __init__(self,db,textbooks,root,tts=None,*,synthesize=None,member_exists=None,privacy=None,repository=None,cloud_api_key='',cloud_synthesize=None):
        self.db,self.root,self.tts,self.synthesize=db,Path(root),tts,synthesize
        self.cloud_api_key=cloud_api_key
        self.cloud_synthesize=cloud_synthesize
        self.member_exists=member_exists or (lambda n:bool(db.execute('SELECT 1 FROM members WHERE id=?',(n,)).fetchone()))
        self.privacy=privacy or (lambda:False)
        self.repository=repository or DictationRepository(db)
        self.settings=DictationSettings(db);self.skills=TeachingSkills(db)
        self.profiles=TeachingProfile(db,self.member_exists)
        self.results=DictationResults(db,self.root/'photos',self.repository,self.member_exists)
        self.context=DictationContext(textbooks,self.results,self.profiles)
        self.sessions={};self.lock=asyncio.Lock()
        db.execute('CREATE TABLE IF NOT EXISTS dictation_preparation_context(task_id TEXT PRIMARY KEY,owner_id INTEGER,session_id TEXT UNIQUE,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS dictation_preparation_sessions(session_id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL,owner_epoch INTEGER NOT NULL)')
        if self.settings.get_v2() is None:self.settings.save_v2(dict(phases=['recognition','dictation'],playback=dict(repeat_count=2,interval_ms=1000,speech_rate=1)),expected_revision=0)

    def owner_epoch(self,owner_id):
        row=self.db.execute('SELECT epoch FROM dictation_owner_epochs WHERE owner_id=?',(owner_id,)).fetchone()
        return row[0] if row else 0

    def begin_session(self,owner_id,session_id,*,turn):
        """Register only at a trusted new user-input boundary, never a model tool."""
        if not turn:raise ValueError('actual initiator input required')
        return self._session(owner_id,session_id,register=True)

    def _session(self,owner_id,session_id,*,register=False):
        if type(owner_id) is not int or owner_id<1 or not self.member_exists(owner_id):raise ValueError('invalid member')
        if self.privacy():raise ValueError('privacy mode active')
        if type(session_id) is not str or not 0<len(session_id)<=128:raise ValueError('invalid preparation session')
        epoch=self.owner_epoch(owner_id)
        registration=self.db.execute('SELECT owner_id,owner_epoch FROM dictation_preparation_sessions WHERE session_id=?',(session_id,)).fetchone()
        if registration:
            if registration[0]!=owner_id:raise ValueError('session owner conflict')
            if registration[1]!=epoch:raise ValueError('member forgotten; preparation session expired')
        else:
            if epoch and not register:raise ValueError('member forgotten; new initiator input required')
            from .db_atomic import atomic
            with atomic(self.db):
                self.db.execute('INSERT INTO dictation_preparation_sessions VALUES(?,?,?)',(session_id,owner_id,epoch))
        session=self.sessions.setdefault(session_id,dict(owner_id=owner_id,skill=self.skills.get(),config=self.settings.get_v2(),profile=self.profiles.get(owner_id)))
        if session['owner_id']!=owner_id:raise ValueError('session owner conflict')
        return session

    def get_context(self,args,*,owner_id,session_id):
        session=self._session(owner_id,session_id)
        session.pop('model_context',None)
        request=copy.deepcopy(args)
        if 'book_id' not in request and session.get('book_id'):request['book_id']=session['book_id']
        result=self.context.resolve(request,owner_id,profile_snapshot=session['profile'],profile_pinned=True)
        result.update(skill=copy.deepcopy(session['skill']),config=copy.deepcopy(session['config']))
        if result['status']=='ready':
            session['context']=copy.deepcopy(result)
            session['book_id']=result['book_id']
            session['request']={k:request[k] for k in ('book_id','scope_type','start','end','unit') if k in request}
            session['request']['book_id']=result['book_id']
        return result

    async def _audio(self,text,speed,tts_settings=None):
        if tts_settings is not None:
            if self.cloud_synthesize:
                data=await self.cloud_synthesize(text,copy.deepcopy(tts_settings),allowed=lambda:not self.privacy())
            else:
                from .dictation_cloud_tts import synthesize_cloud
                data=await synthesize_cloud(text,tts_settings,self.cloud_api_key,allowed=lambda:not self.privacy())
        elif self.synthesize:
            data=self.synthesize(text,speed)
            if inspect.isawaitable(data):data=await data
        elif self.tts:
            data=await asyncio.to_thread(self.tts.generate,text,speed=speed)
            data=pcm24(data,self.tts.sample_rate)
        else:raise ValueError('audio provider unavailable')
        if type(data) is not bytes or not data or len(data)%2 or len(data)>MAX_ASSET_BYTES:raise ValueError('invalid PCM audio')
        return data

    def confirm_candidates(self,owner_id,session_id,confirmation_id,*,turn):
        session=self._session(owner_id,session_id)
        pending=session.get('pending_candidates')
        if not pending or pending['confirmation_id']!=confirmation_id or not turn:raise ValueError('candidate confirmation expired')
        fresh=self.context.resolve(session['request'],owner_id,profile_snapshot=session['profile'],profile_pinned=True)
        sources=[(l['lesson_id'],l['source_sha256']) for l in fresh.get('lessons',[])]
        if sources!=pending['sources']:raise ValueError('candidate source changed')
        session.setdefault('approved_candidates',set()).add(pending['fingerprint'])
        session.pop('pending_candidates',None)

    def observe_initiator(self,owner_id,session_id,text,*,turn):
        session=self._session(owner_id,session_id)
        pending=session.get('pending_candidates')
        if pending and re.sub(r'[\s，。！,.!]','',text) in ('确认','确认采用','同意','可以','是的','就用这些词'):
            self.confirm_candidates(owner_id,session_id,pending['confirmation_id'],turn=turn)

    async def prepare_task(self,args,*,owner_id,session_id,paired_device_id,callback_registration_id):
        async with self.lock:
            session=self._session(owner_id,session_id)
            prior=self.db.execute('SELECT task_id,owner_id,payload FROM dictation_preparation_context WHERE session_id=?',(session_id,)).fetchone()
            if prior:
                if prior[1]!=owner_id:raise ValueError('session owner conflict')
                self.results._task(owner_id,prior[0])
                return json.loads(prior[2])
            context=self.get_context(args,owner_id=owner_id,session_id=session_id) if 'scope_type' in args else session.get('context')
            if not context:return dict(status='needs_input',missing_parameters=['scope_type','start'])
            if context['status']!='ready':return context
            # Re-read reviewed source on each attempt; skill/config remain pinned.
            fresh=self.context.resolve(session['request'],owner_id,profile_snapshot=session['profile'],profile_pinned=True)
            if fresh['status']!='ready':return fresh
            if [(l['lesson_id'],l['source_sha256']) for l in fresh['lessons']]!=[(l['lesson_id'],l['source_sha256']) for l in context['lessons']]:return dict(status='source_changed',reason='请重新确认教材范围')
            task=dict(schema_version=2,task_id=uuid.uuid4().hex,content_hash='',config_revision=session['config']['revision'],skill_revision=session['skill']['revision'],callback_registration_id=callback_registration_id,lessons=[],phases=[],audio_assets=[],playback=session['config']['parameters']['playback'])
            items={'recognition':[],'dictation':[]};texts={};provenance=[]
            for lesson in context['lessons']:
                row=lesson['row'];lid=lesson['lesson_id'];two=list(dict.fromkeys(row['two']));four=list(dict.fromkeys(row['four']))
                task['lessons'].append(dict(lesson_id=lid,source=lesson['source'],two_targets=two,four_targets=four))
                proposal=args.get('proposals',{}).get(lid,args.get('proposals',{}).get(str(row['number']),{}))
                words=proposal.get('words',proposal.get('expanded_words',[]))
                initiator_confirmed=False
                if type(words) is not list or any(type(w) is not str or not re.fullmatch(r'[\u3400-\u9fff]{1,32}',w) for w in words):return dict(status='word_proposals_required',lesson_id=lid)
                if 'dictation' in session['config']['parameters']['phases']:
                    missing=[c for c in four if not any(c in w for w in words)]
                    if missing:return dict(status='word_coverage_required',lesson_id=lid,missing_targets=missing)
                    expansion=row.get('expansions','');known=re.findall(r'[\u3400-\u9fff]{1,32}',expansion) if isinstance(expansion,str) else []
                    unknown=[w for w in words if w not in known]
                    fingerprint=hashlib.sha256(json.dumps([lid,lesson['source_sha256'],words],ensure_ascii=False).encode()).hexdigest()
                    if unknown and fingerprint not in session.get('approved_candidates',set()):
                        pending=session.get('pending_candidates')
                        if not pending or pending['fingerprint']!=fingerprint:
                            pending=dict(confirmation_id=uuid.uuid4().hex,fingerprint=fingerprint,lesson_id=lid,candidates=unknown,sources=[(l['lesson_id'],l['source_sha256']) for l in context['lessons']])
                            session['pending_candidates']=pending
                        return dict(status='word_confirmation_required',lesson_id=lid,candidates=unknown,confirmation_id=pending['confirmation_id'])
                    initiator_confirmed=bool(unknown)
                if 'recognition' in session['config']['parameters']['phases']:
                    from .dictation_preparation import choose_words
                    recognition=proposal.get('recognition_words')
                    known=re.findall(r'[\u3400-\u9fff]{2,32}',row.get('expansions',''))
                    if recognition is None:
                        try:recognition=choose_words(two,known,[])
                        except ValueError:return dict(status='recognition_words_required',lesson_id=lid,missing_targets=two)
                    if type(recognition) is not list or any(type(w) is not str or not re.fullmatch(r'[\u3400-\u9fff]{2,32}',w) for w in recognition):
                        return dict(status='recognition_words_required',lesson_id=lid,missing_targets=two)
                    missing=[c for c in two if not any(c in w for w in recognition)]
                    if missing:return dict(status='recognition_words_required',lesson_id=lid,missing_targets=missing)
                    if any(not any(c in w for c in two) for w in recognition) or len(set(recognition))!=len(recognition):return dict(status='word_scope_error',lesson_id=lid)
                    unknown=[w for w in recognition if w not in known]
                    fingerprint=hashlib.sha256(json.dumps([lid,lesson['source_sha256'],'recognition',recognition],ensure_ascii=False).encode()).hexdigest()
                    if unknown and fingerprint not in session.get('approved_candidates',set()):
                        pending=session.get('pending_candidates')
                        if not pending or pending['fingerprint']!=fingerprint:
                            pending=dict(confirmation_id=uuid.uuid4().hex,fingerprint=fingerprint,lesson_id=lid,candidates=unknown,sources=[(l['lesson_id'],l['source_sha256']) for l in context['lessons']])
                            session['pending_candidates']=pending
                        return dict(status='word_confirmation_required',lesson_id=lid,candidates=unknown,confirmation_id=pending['confirmation_id'])
                    for n,word in enumerate(recognition):items['recognition'].append(dict(item_id=f'{lid}.r{n}',lesson_id=lid,text=word,covers=[c for c in two if c in word],audio_asset_id=None))
                for n,word in enumerate(words):
                    covers=[c for c in four if c in word]
                    if not covers:return dict(status='word_scope_error',word=word)
                    aid='speech.'+hashlib.sha256(word.encode()).hexdigest()[:40];texts[aid]=word
                    items['dictation'].append(dict(item_id=f'{lid}.d{n}',lesson_id=lid,text=word,covers=covers,audio_asset_id=aid))
                provenance.append(dict(lesson_id=lid,source_sha256=lesson['source_sha256'],words=words,reason=proposal.get('reason',''),confirmed=initiator_confirmed))
            for kind in session['config']['parameters']['phases']:task['phases'].append(dict(phase_id=kind,type=kind,items=items[kind]))
            if not any(p['type']=='dictation' for p in task['phases']):texts={}
            blobs={};total=0
            try:
                for aid,text in texts.items():
                    blobs[aid]=await self._audio(text,task['playback']['speech_rate'],session['config']['parameters'].get('tts'));total+=len(blobs[aid])
                    if total>MAX_TASK_BYTES:raise ValueError('task audio quota exceeded')
                    task['audio_assets'].append(dict(asset_id=aid,sha256=hashlib.sha256(blobs[aid]).hexdigest(),media_type='audio/pcm'))
            except Exception as exc:return dict(status='audio_unavailable',reason=str(exc))
            task['content_hash']=content_hash(task);task=validate_task(task)
            self._session(owner_id,session_id)
            root=self.root.resolve()
            folder=root/(task['task_id']+'-'+task['content_hash'])
            if folder.resolve().parent!=root:raise ValueError('invalid task resource path')
            root.mkdir(parents=True,exist_ok=True)
            folder.mkdir(exist_ok=False)
            try:
                manifest=dict(task_id=task['task_id'],content_hash=task['content_hash'],assets=[])
                for a in task['audio_assets']:
                    data=blobs[a['asset_id']];(folder/(a['asset_id']+'.pcm')).write_bytes(data)
                    manifest['assets'].append(dict(asset_id=a['asset_id'],sha256=a['sha256'],pcm_bytes=len(data),frames=len(data)//2,sample_rate=24000,channels=1))
                (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf8')
                result=dict(status='ready',task=task,manifest=manifest,folder=str(folder),context=context,provenance=provenance)
                from .db_atomic import atomic
                with atomic(self.db):
                    self._session(owner_id,session_id)
                    self.repository.submit(task,paired_device_id=paired_device_id,owner_id=owner_id)
                    self.db.execute('INSERT INTO dictation_preparation_context VALUES(?,?,?,?)',(task['task_id'],owner_id,session_id,json.dumps(result,ensure_ascii=False)))
            except BaseException:
                # Only this invocation's newly created folder is ours to remove.
                if not folder.is_symlink() and folder.resolve().parent==root:
                    import shutil
                    shutil.rmtree(folder)
                raise
            return copy.deepcopy(result)

    async def handle_tool(self,name,args,**trusted):
        identity={k:trusted[k] for k in ('owner_id','session_id')}
        if name=='get_dictation_context':
            result=self.get_context(args,**identity)
            if result['status']=='ready':
                session=self._session(**identity)
                session['model_context']=dict(request=copy.deepcopy(session['request']),context=copy.deepcopy(result))
            return result
        if name=='prepare_dictation_task':
            session=self._session(**identity)
            exposed=session.get('model_context')
            scope_keys=('book_id','scope_type','start','end','unit')
            if not exposed or session.get('context')!=exposed['context'] or any(
                key in args and args[key]!=exposed['request'].get(key) for key in scope_keys
            ):
                return dict(status='context_required',reason='请先读取匹配教材范围的 get_dictation_context，再依据孩子事实和业务 skill 提交最终词序')
            # Scope is pinned to the context already returned to the model.
            return await self.prepare_task({k:v for k,v in args.items() if k not in scope_keys},**trusted)
        raise ValueError('unknown dictation tool')

    async def sync_display(self):
        transport=getattr(self,'transport',None)
        if transport is None:return dict(status='pending')
        try:return await transport.configure_display(self.settings.get_display())
        except Exception:return dict(status='pending')

    @property
    def tool_definitions(self):
        scope=dict(book_id={'type':'string'},scope_type={'type':'string','enum':['lesson','lesson_range','unit']},start={'type':'integer'},end={'type':'integer'},unit={'type':'integer'})
        return [dict(type='function',function=dict(name=name,description=description,parameters=dict(type='object',properties=props))) for name,description,props in [('get_dictation_context','读取指定孩子教材、业务skill和学习事实；缺少范围时澄清。',scope),('prepare_dictation_task','必须先成功调用 get_dictation_context 读取匹配教材范围的孩子事实和业务skill，再按照最终模型词序准备任务；覆盖全部四会字；词库外候选必须经发起者确认。proposals以lesson_id为键，值含words数组、recognition_words数组和reason。recognition_words用二字以上组词覆盖全部二会字，保留词语完整展示，不提交裸字；优先教材词库，缺词提出候选。词库外候选确认由真实发起者回复或页面确认记录，模型不能自报已确认。',dict(scope,proposals={'type':'object','additionalProperties':{'type':'object','properties':{'words':{'type':'array','items':{'type':'string'}},'recognition_words':{'type':'array','items':{'type':'string'}},'reason':{'type':'string'},'confirmed':{'type':'boolean'}}}}))]]

    def attach_transport(self,client,*,base,token,device_id):
        from .dictation_transport import DictationTransport
        self.transport=DictationTransport(self.repository,client,base=base,token=token,device_id=device_id)
        return self.transport

    async def deliver_task(self,task_id,*,owner_id):
        self.results._task(owner_id,task_id)
        row=self.db.execute('SELECT payload FROM dictation_preparation_context WHERE task_id=?',(task_id,)).fetchone()
        if not row:raise ValueError('missing prepared resources')
        if not getattr(self,'transport',None):return dict(self.repository.status(task_id),delivery_state='waiting_tv')
        await self.sync_display()  # Apply the parent display preference before the first task frame.
        return await self.transport.deliver(json.loads(row[0]))

    async def control_task(self,command,*,owner_id):
        self.results._task(owner_id,command['task_id'])
        if not getattr(self,'transport',None):raise ValueError('television unavailable')
        return await self.transport.command(command)

    def forget(self,owner_id):
        """Revoke synchronously before member removal; cleanup resources after commit."""
        from .db_atomic import atomic
        with atomic(self.db):
            ids=[r[0] for r in self.db.execute('SELECT task_id FROM dictation_tasks WHERE owner_id=?',(owner_id,))]
            self.results.forget(owner_id)
            for tid in ids:
                self.db.execute('DELETE FROM dictation_v2_actual WHERE task_id=?',(tid,))
                self.db.execute('DELETE FROM dictation_v2_commands WHERE task_id=?',(tid,))
            self.db.execute('DELETE FROM dictation_preparation_context WHERE owner_id=?',(owner_id,))
            self.db.execute('DELETE FROM teaching_profile_revisions WHERE member_id=?',(owner_id,))
        self.sessions={k:v for k,v in self.sessions.items() if v['owner_id']!=owner_id}
        if not self.db.in_transaction:self.reclaim_resources()

    def reclaim_resources(self):
        if self.db.in_transaction:raise ValueError('commit before reclaiming resources')
        import shutil
        self.results.reclaim_files()
        retained={r[0] for r in self.db.execute('SELECT task_id FROM dictation_preparation_context')}
        for folder in self.root.iterdir():
            if folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'.+-[0-9a-f]{64}',folder.name):
                task_id=folder.name.rsplit('-',1)[0]
                if task_id not in retained:shutil.rmtree(folder)
