"""Bounded OpenAI-compatible teaching conversation using shared business tools."""
import json

class DictationAgent:
    def __init__(self,client,model,service,*,owner_id,session_id,paired_device_id,callback_registration_id,max_rounds=8,request_options=None):
        self.client,self.model,self.service=client,model,service
        self.trusted=dict(owner_id=owner_id,session_id=session_id,paired_device_id=paired_device_id,callback_registration_id=callback_registration_id)
        self.max_rounds=max_rounds
        self.request_options={'timeout':90,**(request_options or {})}
        self.messages=[]
        self.prepared=None

    async def respond(self,text):
        if self.prepared is not None:
            self.service._session(self.trusted['owner_id'],self.trusted['session_id'])
            return dict(status='already_prepared',task=self.prepared['task'],result=self.prepared,trace=[])
        session=self.service._session(self.trusted['owner_id'],self.trusted['session_id'])
        self.service.observe_initiator(self.trusted['owner_id'],self.trusted['session_id'],text,turn='user:'+str(len(self.messages)))
        if not self.messages:
            self.messages.append(dict(role='system',content=session['skill']['content']+'\n准备任务前必须调用get_dictation_context读取真实教材和学习事实。缺少教材或范围时可以先询问，获得信息后再读取上下文。最终选词顺序由你决定；用prepare_dictation_task提交覆盖全部目标字的词语及理由，不能声称工具未完成的成功。词库外候选先向发起者确认。只做认读和默写，不做背诵。'))
        self.messages.append(dict(role='user',content=text))
        trace=[]
        for _ in range(self.max_rounds):
            self.service._session(self.trusted['owner_id'],self.trusted['session_id'])
            response=await self.client.chat.completions.create(model=self.model,messages=self.messages,tools=self.service.tool_definitions,**self.request_options)
            message=response.choices[0].message
            packed=message.model_dump(exclude_none=True) if hasattr(message,'model_dump') else dict(role='assistant',content=message.content)
            self.messages.append(packed)
            calls=getattr(message,'tool_calls',None) or []
            if not calls:return dict(status='response',text=message.content or '',trace=trace)
            for call in calls:
                try:
                    args=json.loads(call.function.arguments)
                    result=await self.service.handle_tool(call.function.name,args,**self.trusted)
                except (ValueError,TypeError) as exc:result=dict(status='error',reason=str(exc))
                trace.append(dict(tool=call.function.name,arguments=call.function.arguments,result=result))
                self.messages.append(dict(role='tool',tool_call_id=call.id,content=json.dumps(result,ensure_ascii=False)))
                if result.get('status')=='ready' and 'task' in result:
                    self.prepared=result
                    return dict(status='ready',task=result['task'],result=result,trace=trace)
        return dict(status='round_limit',reason='模型工具轮次已达上限',trace=trace)
