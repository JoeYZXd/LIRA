"""Session-owned monotonic timers; expiry is evidence, never task progression."""
import asyncio
import copy
import math
import time
import uuid
from collections import deque


class AgentTimers:
    def __init__(self, task_state, *, clock=time.monotonic, max_timers=32, max_events=32):
        self.state, self.clock = task_state, clock
        self.max_timers, self.max_events = max_timers, max_events
        self.timers, self.events = {}, deque()
        self._wake = asyncio.Event()
        self._epoch = 0
        self._unsubscribe = task_state.subscribe(self._revoke)

    def _revoke(self):
        self._epoch += 1
        for timer in self.timers.values():
            timer['task'].cancel()
        self.timers.clear()
        self.events.clear()
        self._wake.set()
        self._wake = asyncio.Event()

    def close(self):
        self._revoke()

    @staticmethod
    def _snapshot(timer):
        return copy.deepcopy({k:v for k,v in timer.items() if k != 'task'})

    def _allowed(self, stamp):
        return self.state.permits(*stamp)

    async def _expire(self, timer, stamp, timer_revision):
        try:
            await asyncio.sleep(max(0, timer['deadline']-self.clock()))
            if (not self._allowed(stamp) or self.timers.get(timer['timer_id']) is not timer
                    or timer['timer_revision'] != timer_revision or timer['status'] != 'pending'):
                return
            timer['status'] = 'expired'
            self.events.append({'event_id':uuid.uuid4().hex,'type':'timer_expired',
                'timer_id':timer['timer_id'],'timer_revision':timer_revision,
                'revision':stamp[0], 'deadline':timer['deadline']})
            self._wake.set()
        except asyncio.CancelledError:
            pass

    async def call(self, name, args):
        snapshot = self.state.snapshot()
        stamp = (args.get('revision'), snapshot['generation'], snapshot['owner'])
        if not self._allowed(stamp):
            return {'status':'conflict','revision':snapshot['revision']}
        if name == 'list_task_timers':
            return {'status':'ok','timers':[self._snapshot(t) for t in self.timers.values()]}
        if name in ('poll_task_events','wait_for_task_event'):
            if name == 'wait_for_task_event' and not self.events:
                timeout = args.get('timeout_seconds', 0)
                if not self._duration(timeout, 40):return {'status':'rejected'}
                epoch = self._epoch
                deadline = self.clock()+timeout
                while not self.events:
                    remaining = deadline-self.clock()
                    if remaining <= 0:return {'status':'ok','event':None,'timed_out':True}
                    try:
                        await asyncio.wait_for(self._wake.wait(), remaining)
                    except TimeoutError:
                        if epoch != self._epoch or not self._allowed(stamp):
                            return {'status':'cancelled'}
                        return {'status':'ok','event':None,'timed_out':True}
                    if epoch != self._epoch or not self._allowed(stamp):
                        return {'status':'cancelled'}
                    if not self.events:self._wake.clear()
            if name == 'wait_for_task_event':
                event = self.events.popleft() if self.events else None
                if not self.events:self._wake.clear()
                return {'status':'ok','event':event,'timed_out':False}
            events = list(self.events)
            self.events.clear();self._wake.clear()
            return {'status':'ok','events':events}
        if name == 'create_task_timer':
            if not self._duration(args.get('duration_seconds'),86400):return {'status':'rejected'}
            reserved = len(self.events)+sum(t['status']=='pending' for t in self.timers.values())
            if len(self.timers)>=self.max_timers or reserved>=self.max_events:
                return {'status':'busy','reason':'timer_or_event_capacity'}
            timer={'timer_id':uuid.uuid4().hex,'timer_revision':1,'revision':stamp[0],
                   'status':'pending','deadline':self.clock()+args['duration_seconds']}
            self.timers[timer['timer_id']] = timer
        elif name in ('update_task_timer','cancel_task_timer'):
            timer=self.timers.get(args.get('timer_id'))
            if timer is None:return {'status':'unknown'}
            if (args.get('timer_revision') != timer['timer_revision']
                    or timer['status'] != 'pending'):
                return {'status':'conflict','timer_revision':timer['timer_revision'],
                        'timer_status':timer['status']}
            if name=='update_task_timer' and not self._duration(args.get('duration_seconds'),86400):
                return {'status':'rejected'}
            timer['task'].cancel()
            timer['timer_revision'] += 1
            if name=='cancel_task_timer':
                timer['status']='cancelled'
                self.events = deque(e for e in self.events if e['timer_id'] != timer['timer_id'])
                if not self.events:self._wake.clear()
                return dict(status='ok',timer=self._snapshot(timer))
            timer['deadline']=self.clock()+args['duration_seconds']
        else:return {'status':'unavailable'}
        timer['task']=asyncio.create_task(self._expire(timer,stamp,timer['timer_revision']))
        return {**self._snapshot(timer), 'timer_status':timer['status'], 'status':'ok', 'event_delivery':'queue_until_wait_or_poll', 'waiting':False}

    @staticmethod
    def _duration(value, maximum):
        return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and 0<value<=maximum

    def register(self, registry):
        descriptions={
            'create_task_timer':'创建计时但不等待。若用户要求等待，随后必须调用wait_for_task_event；仅创建不能说正在等待。到期入队，不自动发声或推进。',
            'update_task_timer':'同时核验约定revision与timer_revision后修改尚未到期的计时。',
            'cancel_task_timer':'核验版本后取消尚未到期的计时。',
            'list_task_timers':'查询本会话计时及其真实状态。',
            'poll_task_events':'一次消费未读事件。空队列不表示没有历史结果；已消费wait结果须list/get_capability_operation读取，勿重复创建。',
            'wait_for_task_event':'真正等待并一次消费事件，最长40秒；timeout不是到期。跨轮历史结果保存在本次operation，使用list/get_capability_operation读取，不再poll已消费事件。'}
        for name,description in descriptions.items():
            props={'revision':{'type':'integer','minimum':0}}
            if name in ('update_task_timer','cancel_task_timer'):
                props.update(timer_id={'type':'string','minLength':1,'maxLength':64},timer_revision={'type':'integer','minimum':1})
            if name in ('create_task_timer','update_task_timer'):
                props['duration_seconds']={'type':'number','minimum':.001,'maximum':86400}
            if name=='wait_for_task_event':props['timeout_seconds']={'type':'number','minimum':.001,'maximum':40}
            async def handler(args,name=name):return await self.call(name,args)
            registry.register(name,description,{'type':'object','properties':props,'required':list(props),'additionalProperties':False},handler)
