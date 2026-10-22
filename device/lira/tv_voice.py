"""A single, short-lived presentation consent bound to observed voice turns."""
from __future__ import annotations

import asyncio
import base64
import heapq
import os
from pathlib import Path
import re
import time
import uuid

from lira.infrared_voice import tool
from lira.textbook_practice import TextbookPractice, needs_input

TV_TOOLS=[
    tool('prepare_tv_display','通用电视投屏入口。根据对话提取参数；未知参数可省略，返回 needs_input 时按 question 追问并合并上下文重新调用。只准备内容，必须另行征得同意才能展示。',{
        'content_type':{'type':'string','enum':['text','image'],'minLength':0,'maxLength':16,'description':'文字用 text，已登记图片用 image；能从上下文确定时不重复询问。'},
        'text':{'type':'string','minLength':0,'maxLength':8000,'description':'需要展示的完整文字，不能编造用户未提供的作业字表。'},
        'image_id':{'type':'string','minLength':0,'maxLength':100,'description':'list_tv_images 返回的图片编号。'},
        'caption':{'type':'string','minLength':0,'maxLength':160,'description':'图片说明，可省略。'},
    }),
    tool('list_practice_textbooks','查询已导入的教材及单元、课文目录。教材编号和课号只能来自这里，不能猜测课文内容。'),
    tool('prepare_pinyin_practice','从已导入教材准备看汉字写拼音练习，复用通用投屏能力。参数缺失返回 needs_input，必须追问；只显示汉字，不提供拼音答案。',{
        'book_id':{'type':'string','minLength':0,'maxLength':100,'description':'教材编号；只有一本时可省略，多本时从对话确认。'},
        'scope_type':{'type':'string','enum':['lesson','unit','lesson_range'],'minLength':0,'maxLength':16,'description':'单课、整单元、连续课文范围。'},
        'start':{'type':'integer','minimum':1,'maximum':999,'description':'课号、单元号或起始课号。'},
        'end':{'type':'integer','minimum':1,'maximum':999,'description':'仅 lesson_range 使用，包含这节结束课。'},
        'unit':{'type':'integer','minimum':1,'maximum':999,'description':'单课或连续课范围的单元限定；识字课和阅读课重号时必须追问单元，不可猜测。'},
        'character_group':{'type':'string','enum':['four','two','all'],'minLength':0,'maxLength':8,'description':'四会字=four，二会字=two，两类都练=all。用户未指定时必须省略并追问，不能默认 all。'},
    }),
    tool('list_tv_images','查询已登记的可展示图片。只能使用这里返回的 image_id；没有图片时不能编造。'),
    tool('prepare_tv_text','准备在客厅电视展示大字。不切换电视。必须逐字念出返回的 question 并等待下一轮家人明确同意。',
         {'text':{'type':'string','minLength':1,'maxLength':8000}}),
    tool('prepare_tv_image','准备展示已登记图片。不切换电视。必须逐字念出返回的 question 并等待下一轮家人明确同意。',
         {'image_id':{'type':'string','minLength':1,'maxLength':100},'caption':{'type':'string','minLength':0,'maxLength':160}}),
    tool('confirm_tv_display','仅在问完电视展示问题、下一轮用户明确同意后调用。只提交先前固定的内容，本地验证真实转写；不能替用户同意。',
         {'request_id':{'type':'string','minLength':1,'maxLength':40}}),
    tool('cancel_tv_display','取消尚未完成的展示请求；不关闭电视，不切换已经显示的内容。'),
    tool('get_tv_display_status','查询本轮展示结果。只有 displayed 才能说已显示；displaying 仅代表发送中。'),
]
# The generic entry accepts partial arguments so missing information has a
# conversational response, rather than failing JSON validation.
for entry in TV_TOOLS:
    if entry['name'] in ('prepare_tv_display','prepare_pinyin_practice'):entry['parameters']['required']=[]
_TV_VALIDATION_TOOLS=TV_TOOLS
# Preserve the existing internal call surface without offering competing
# preparation tools to the conversational model.
TV_TOOLS=[entry for entry in _TV_VALIDATION_TOOLS if entry['name'] not in ('prepare_tv_text','prepare_tv_image')]
TV_TOOL_NAMES={entry['name'] for entry in _TV_VALIDATION_TOOLS}
TV_INSTRUCTIONS=('识别到需要投屏、在电视展示或用大字/图片帮助理解时，使用通用工具prepare_tv_display。'
    '从当前及前文提取content_type、text或image_id、可选caption。已知信息不重复问，缺失信息不能编造；'
    '可先用已有参数调用工具，needs_input时按question追问，等待用户补齐后合并前文参数重新调用。'
    '参数补齐不是展示同意。工具返回awaiting_consent后，'
    '逐字念出工具返回的question，等待下一轮家人明确同意，再confirm_tv_display。'
    '即使用户最初说请显示，也不能跳过这次询问。不要在同一轮假装获得同意，不要把准备成功说成显示成功。'
    '同意仅对该request_id的内容有效；改变内容需重新询问。拒绝或取消时cancel_tv_display。'
    'displaying时先说正在发送，随后查询get_tv_display_status，只有displayed才说已显示。'
    '图片必须先list_tv_images并用返回编号；本工具不生成图片。电视不在线时如实说明，本阶段不唤醒、不关机，也不要借红外工具代做电源操作。')
TV_INSTRUCTIONS+=('教材字表可通过list_practice_textbooks查询目录；仅当用户选择看字写拼音并明确范围和类别时，才使用prepare_pinyin_practice。'
    '默写一词不等于选择了看字写拼音；不要预设练习形式、节奏或提示方式。已知范围与类别不重复询问，未知不猜测。'
    'needs_input按真实缺失补充；textbook_required或no_characters不得编造字表、用通用文字绕过教材缺失。'
    '仅当当前约定仍为看字写拼音时，不主动泄露拼音答案；孩子请求答案或讲解而任务意图不清时先澄清，已明确转为讲解则不重复确认。'
    '投屏仍须遵守逐项内容同意与实际显示结果；纸上内容未经核对不能宣称已批改。')


def validate_tv_arguments(name,args):
    entry=next((entry for entry in _TV_VALIDATION_TOOLS if entry['name']==name),None)
    if entry is None or not isinstance(args,dict):raise ValueError('Invalid TV tool')
    properties=entry['parameters']['properties']
    if not set(entry['parameters']['required'])<=set(args)<=set(properties):raise ValueError('Invalid TV arguments')
    for key,value in args.items():
        spec=properties[key]
        if spec['type']=='integer':
            if type(value) is not int or not spec['minimum']<=value<=spec['maximum']:raise ValueError('Invalid TV argument')
            continue
        if not isinstance(value,str) or not spec['minLength']<=len(value)<=spec['maxLength']:
            raise ValueError('Invalid TV argument')
        if any(ord(c)<32 and c not in '\n\t' for c in value):raise ValueError('Invalid text')
        if 'enum' in spec and value not in spec['enum'] and value!='':raise ValueError('Invalid TV argument')


def normalized(text):
    return re.sub(r'[\s，。！？、,.!?：:；;]','',text)


class TvVoiceTools:
    def __init__(self,*,sender=None,image_dir=None,textbook_dir=None, textbook_import_dir=None):
        self.sender=sender
        self.image_dir=Path(image_dir).resolve() if image_dir else None
        self.pending=None
        self.task=None
        self.result={'status':'idle'}
        self.practice=TextbookPractice(textbook_dir, draft_directory=textbook_import_dir)
        self.practice_group=None
        self.catalog_snapshot=[]
        self.dictation_active=False
        self._dictation_preparing=False
        self.category_question=None
        self.category_armed=False
        self.displayed_id=None
        self.cleared_ids=set()

    @classmethod
    def from_env(cls,env):
        config=env.get('LIRA_TV_CONFIG','')
        sender=None
        if config:
            try:
                from lira.tv_display import TvDisplaySender
                sender=TvDisplaySender(config)
            except (OSError,ValueError,KeyError):pass
        return cls(sender=sender,image_dir=env.get('LIRA_TV_IMAGE_DIR'),textbook_dir=env.get('LIRA_TEXTBOOK_DIR'),textbook_import_dir=env.get('LIRA_TEXTBOOK_IMPORT_DIR'))

    def event(self,kind,text):
        value=normalized(text)
        if kind in ('assistant_text','assistant_prompt'):
            four='四会' in value;two='二会' in value
            question=any(w in value for w in ('对吗','可以吗','好吗','是吗','确认吗')) or ('可以' in value and value.endswith('吗'))
            self.category_question=('four' if four else 'two') if question and four!=two else None
        elif kind=='response_done':self.category_armed=self.category_question is not None
        elif kind=='interrupted':self.category_armed=False
        if kind=='user_text':
            if self.category_armed and value in {'好','好的','可以','对','是的','确认','同意','可以的'}:
                self.practice_group=self.category_question
            self.category_armed=False
            value=normalized(text)
            value=re.sub(r'(?<!\d)4[会回]','四会',value)
            value=re.sub(r'(?<!\d)2[会回]','二会',value)
            for alias in ('思悔字','四汇字','四惠字','四回字','4回字','四悔字','思会字'):
                value=value.replace(alias,'四会字')
            for alias in ('二汇字','二惠字'):
                value=value.replace(alias,'二会字')
            if any(w in value for w in ('练','默写','听写')):
                from pypinyin import lazy_pinyin
                sounds=lazy_pinyin(value)
                # ASR homophones are interpreted only inside an explicit practice request.
                for syllables,label in ((('si','hui','zi'),'四会字'),(('er','hui','zi'),'二会字')):
                    if any(tuple(sounds[i:i+3])==syllables for i in range(len(sounds)-2)):
                        value+=label
                for alias in ('思悔','四回','4回'):
                    value=value.replace(alias,'四会')
            # Require a positive, observed category choice. Ambiguous negation
            # is left to a follow-up instead of letting a model invent a slot.
            if not any(word in value for word in ('不','别','取消','什么','区别','怎么','哪些','为什么','意思','还是','或者')):
                four='四会' in value;two='二会' in value
                if (any(word in value for word in ('两类都练','两类字都练','两种都练')) or value=='都练'
                        or (four and two and any(word in value for word in ('和','都练','一起练')))):
                    self.practice_group='all'
                elif four and not two:self.practice_group='four'
                elif two and not four:self.practice_group='two'
                elif four or two:self.practice_group=None
            elif '四会' in value or '二会' in value:
                self.practice_group=None
        if kind=='user_text' and normalized(text) in {'取消','取消显示','取消展示','不要显示','不用显示了','不用了','不要','等一下','先别显示','停止显示'}:
            self.pending=None
            if self.task and not self.task.done():self.task.cancel()
            self.result={'status':'cancelled'}
            return
        p=self.pending
        if not p:return
        if time.monotonic()>p['expires']:
            self.pending=None
            return
        if kind in ('assistant_text','assistant_prompt'):
            p['asked']=normalized(text).endswith(normalized(p['question']))
            if p.get('dictation_pinyin') and all(w in value for w in ('电视','拼音','显示')) and (any(w in value for w in ('可以吗','好吗','确认吗','是否')) or ('可以' in value and value.endswith('吗'))):
                p['asked']=True
            p['armed']=False
        elif kind=='response_done' and p['asked']:
            p['armed']=True
        elif kind=='interrupted':
            self.pending=None
        elif kind=='user_text':
            yes=normalized(text) in {'同意','同意显示','可以','可以显示','好','好的','显示吧','可以放到电视上','可以显示到电视上','统一显示'}
            # A full, explicit display instruction can answer the pending
            # question. Match the imperative itself; negations, conditions,
            # reported speech and questions never fit this grammar.
            yes = yes or (not any(mark in text for mark in ('?', '？')) and re.fullmatch(
                r'(?:(?:可以|好的|同意)(?:请|先|你|帮我)*|请|你|帮我)(?:把|将)?'
                r'(?:这页|这一页|这段|这些)?(?:二会字|四会字)?(?:拼音|内容|文字|图片)?'
                r'(?:显示|投屏|放)(?:到|在)(?:客厅)?电视上?(?:吧)?', normalized(text)) is not None)
            if p['armed'] and yes:
                p['approved']=True
                p['armed']=False
            else:
                self.pending=None
                self.result={'status':'cancelled','reason':'未收到针对本次询问的明确同意，请重新准备并询问。'}

    def images(self):
        if not self.image_dir or not self.image_dir.is_dir():return []
        with os.scandir(self.image_dir) as entries:
            names=heapq.nsmallest(64,(p.name for p in entries if p.is_file(follow_symlinks=False)
                and Path(p.name).suffix.lower() in ('.png','.jpg','.jpeg') and len(p.name)<=100))
        return [{'image_id':name,'label':Path(name).stem} for name in names]

    def _image_payload(self,args):
        if args['image_id'] not in {i['image_id'] for i in self.images()}:raise ValueError('Unknown image')
        path=(self.image_dir/args['image_id']).resolve()
        if path.parent!=self.image_dir:raise ValueError('Invalid image path')
        with path.open('rb') as f:raw=f.read(2*1024*1024+1)
        if len(raw)>2*1024*1024 or not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff')):
            raise ValueError('Unsupported image')
        return {'type':'image','text':args['caption'],'image_base64':base64.b64encode(raw).decode()}

    async def close(self):
        self.displayed_id=None
        self.cleared_ids.clear()
        self.pending=None
        self.practice_group=None
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        self.task=None
        self.result={'status':'cancelled'}

    def register_capabilities(self,registry):
        registry.register('probe_tv_receiver','Probe paired TV version and capabilities without launching or waking.',
            {'type':'object','properties':{},'required':[],'additionalProperties':False},
            self._probe,availability=lambda:'configured' if self.sender else 'unavailable')
        registry.register('clear_tv_content','Clear content successfully displayed by this session using its content_id. Cancelling pending display is separate.',
            {'type':'object','properties':{'content_id':{'type':'string','minLength':1,'maxLength':64}},
             'required':['content_id'],'additionalProperties':False},self._clear,
             availability=lambda:'configured' if self.sender else 'unavailable')

    async def prepare_dictation_pinyin(self, text):
        self._dictation_preparing=True
        try:return await self.call('prepare_tv_display',{'content_type':'text','text':text})
        finally:self._dictation_preparing=False

    async def probe_receiver(self):
        return await self._probe({})

    async def ensure_receiver_ready(self):
        """Explicit dictation preparation may launch; generic probes stay read-only."""
        if not self.sender:return {'status':'unavailable','reason':'TV not configured.'}
        try:
            prepare=getattr(self.sender,'ensure_ready',self.sender.probe)
            result=await prepare()
            return dict(result,status='ok',clear_supported='clear_content_id' in (result.get('capabilities') or []))
        except Exception:
            return {'status':'unavailable','reason':'Receiver unavailable after task startup attempt; no power command sent.'}

    async def show_trusted_notice(self, text):
        """Known task notice only; the requesting capability owns user authorization."""
        if not self.sender:return {'status':'unavailable','reason':'电视未配置，未显示准备画面。'}
        if self.task and not self.task.done():return {'status':'busy'}
        self.pending=None
        self.result={'status':'displaying'}
        self.task=asyncio.create_task(self._send({'type':'text','text':text,'id':uuid.uuid4().hex,'approved':True}))
        return dict(self.result)

    async def _probe(self,args):
        if not self.sender:return {'status':'unavailable'}
        try:
            result=await self.sender.probe()
            return dict(result,status='ok',clear_supported='clear_content_id' in (result.get('capabilities') or []))
        except Exception:return {'status':'unavailable','reason':'Receiver unavailable; no wake or launch attempted.'}

    async def _clear(self,args):
        content_id=args['content_id']
        if content_id in self.cleared_ids:return {'status':'ok','display_status':'cleared','cleared_id':content_id}
        if content_id!=self.displayed_id:return {'status':'failed','reason':'Content not owned by this session.'}
        try:
            result=await self.sender.clear(content_id)
            if result.get('cleared_id')!=content_id:raise ValueError('No acknowledgement')
        except Exception:return {'status':'failed','reason':'Clear not confirmed: unavailable, dismissed, unsupported or replaced.'}
        self.cleared_ids.add(content_id)
        if self.displayed_id==content_id:
            self.displayed_id=None
            self.result={'status':'cleared','cleared_id':content_id}
        return {'status':'ok','display_status':'cleared','cleared_id':content_id}

    async def _send(self,payload):
        try:
            result=await self.sender.show(payload)
            self.result=({'status':'displayed','content_id':payload['id']} if result.get('foreground') and result.get('rendered_id')==payload['id']
                else {'status':'failed','reason':'未确认电视已展示，请稍后重新询问。'})
            if self.result['status']=='displayed':self.displayed_id=payload['id']
        except asyncio.CancelledError:
            self.result={'status':'cancelled'}
            raise
        except Exception:
            self.result={'status':'failed','reason':'电视展示未成功。请确认电视已开机、网络正常；未尝试唤醒或关机。'}

    async def call(self,name,args):
        validate_tv_arguments(name,args)
        if self.dictation_active and not self._dictation_preparing and name in ('prepare_tv_display','prepare_tv_text','prepare_tv_image','prepare_pinyin_practice'):
            return {'status':'dictation_tool_required','reason':'默写中的资料须由present_dictation_item输出，不能绕过核对、类别、读音或分页校验；换任务请先cancel_dictation。'}
        if name=='list_practice_textbooks':
            result=await asyncio.to_thread(self.practice.catalog)
            self.catalog_snapshot=result['books']
            return result
        if name=='prepare_pinyin_practice':
            if self.task and not self.task.done():return {'status':'busy','reason':'上次展示正在发送，请先查询或取消。'}
            self.pending=None
            practice=await asyncio.to_thread(self.practice.prepare,args)
            if practice['status']!='ready':
                self.result=practice
                return dict(practice)
            if args.get('character_group')!=self.practice_group:
                self.result=needs_input(['character_group'],'请确认这次练四会字、二会字，还是两类字都练？')
                self.result['observed_character_group']=self.practice_group
                self.result['instructions']='没有观察到与参数一致的用户选择；不得自行默认类别。若已有 observed_character_group，请按真实选择修正参数。'
                return dict(self.result)
            result=await self.call('prepare_tv_display',{'content_type':'text','text':practice['text']})
            if result['status']=='awaiting_consent':
                group={'four':'四会字','two':'二会字','all':'四会字和二会字'}[practice['character_group']]
                scope=(f"第{args['start']}单元" if args['scope_type']=='unit' else
                    f"第{args['start']}到第{args['end']}课" if args['scope_type']=='lesson_range' else f"第{args['start']}课")
                if args.get('unit') and args['scope_type']!='unit':scope=f"第{args['unit']}单元"+scope
                question=f"可以把《{practice['book_title']}》{scope}的{group}显示到客厅电视上，让你看字写拼音吗？"
                self.pending['question']=question
                result['question']=question
            return dict(result,practice={key:value for key,value in practice.items() if key not in ('text','status')})
        if name=='prepare_tv_display':
            if self.task and not self.task.done():return {'status':'busy','reason':'上次展示正在发送，请先查询或取消。'}
            self.pending=None
            kind=args.get('content_type','')
            missing=[];question=''
            if not kind:
                missing=['content_type'];question='你想在客厅电视上显示文字，还是图片？'
            elif kind=='text' and not args.get('text','').strip():
                missing=['text'];question='你想在电视上显示哪些文字？'
            elif kind=='image' and not args.get('image_id','').strip():
                missing=['image_id'];question='你想显示哪张已登记的图片？'
            if missing:
                self.result=needs_input(missing,question)
                return dict(self.result)
            if kind=='text':
                return await self.call('prepare_tv_text',{'text':args['text']})
            try:
                return await self.call('prepare_tv_image',{'image_id':args['image_id'],'caption':args.get('caption','')})
            except (OSError,ValueError):
                self.result={'status':'needs_input','missing_parameters':['image_id'],
                    'question':'这张图片不可用，请从已登记的图片中重新选择。','instructions':'调用 list_tv_images 查询可选图片。'}
                return dict(self.result)
        if name=='cancel_tv_display':
            owned=self.displayed_id
            cleared=set(self.cleared_ids)
            await self.close()
            self.displayed_id=owned
            self.cleared_ids=cleared
            return dict(self.result)
        if name=='get_tv_display_status':return dict(self.result)
        if name=='list_tv_images':return {'status':'ok','images':await asyncio.to_thread(self.images)}
        if not self.sender:return {'status':'unavailable','reason':'电视展示尚未配置；没有执行任何操作。'}
        if name=='confirm_tv_display':
            p=self.pending
            if not p or p['id']!=args['request_id'] or not p['approved'] or time.monotonic()>p['expires']:
                return {'status':'confirmation_required','reason':'没有本次内容的有效同意；请重新准备、询问并等待。'}
            self.pending=None
            self.result={'status':'displaying','instructions':'正在发送，稍后查询 get_tv_display_status；尚不能说已显示。'}
            self.task=asyncio.create_task(self._send(p['payload']))
            return dict(self.result)
        if self.task and not self.task.done():return {'status':'busy','reason':'上次展示正在发送，请先查询或取消。'}
        if name=='prepare_tv_text':
            if not args['text'].strip():raise ValueError('Empty text')
            payload={'type':'text','text':args['text']}
            question='可以把这段文字显示到客厅电视上吗？'
        else:
            payload=await asyncio.to_thread(self._image_payload,args)
            question='可以把这张图片显示到客厅电视上吗？'
        token=uuid.uuid4().hex
        self.pending={'id':token,'payload':dict(payload,id=token,approved=True),'question':question,
                      'expires':time.monotonic()+90,'asked':False,'armed':False,'approved':False}
        self.result={'status':'awaiting_consent'}
        return {'status':'awaiting_consent','request_id':token,'question':question,
                'preview':payload['text'][:160],
                'instructions':'逐字问出 question，等待下一轮明确同意。不能现在调用确认。'}
