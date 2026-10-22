"""Small local IR tool surface. Raw codes never enter model context."""
from __future__ import annotations

import asyncio
import time

from lira.appliances import ApplianceError, ApplianceModel
from lira.hal.base import HalError


def tool(name, description, properties=None):
    properties = properties or {}
    return dict(type='function', name=name, description=description, parameters={
        'type': 'object', 'properties': properties, 'required': list(properties),
        'additionalProperties': False})


NAME = {'type': 'string', 'minLength': 1, 'maxLength': 40}
IR_TOOLS = [
    tool('list_infrared_devices', '查询本地红外家电、动作和是否已学习。控制前先查询，使用返回的准确名称。'),
    tool('prepare_infrared_learning', '用户明确要求配对时准备学习。缺少设备、按键或类别时先问清。只准备，不开始收码；向用户念出返回的摆放和准备步骤。',
         {'device': NAME, 'action': NAME,
          'category': {'type': 'string', 'enum': ['电视', '空调', '风扇', '其他']}}),
    tool('start_infrared_learning', '仅在已经讲完准备步骤且用户明确说准备好了或开始学习时调用。开启约10秒学习窗口，提示立即按原遥控器的目标按键；按完让用户说按好了，再查询结果。'),
    tool('get_infrared_learning_status', '用户说按好了、询问学习结果时查询。必须依据状态说明成功、等待或重试，不能自行声称已配对。'),
    tool('cancel_infrared_learning', '用户说取消配对、取消学习时调用；取消尚未完成的学习，不删除已经保存的按键。'),
    tool('control_infrared_device', '用户明确要求遥控时调用，准确使用已查询的设备与动作。成功只说指令已发出，不能宣称家电状态已改变。不把配对成功当成发送授权。',
         {'device': NAME, 'action': NAME}),
]
IR_TOOL_NAMES = {entry['name'] for entry in IR_TOOLS}


def validate_tool_arguments(name, args, tools):
    spec = next((entry['parameters'] for entry in tools if entry['name'] == name), None)
    if spec is None or not isinstance(args, dict):
        raise ValueError('Invalid tool')
    properties = spec['properties']
    if set(args) != set(properties):
        raise ValueError('Invalid arguments')
    for key, value in args.items():
        if (not isinstance(value, str) or len(value.strip()) < properties[key].get('minLength', 1) or len(value) > properties[key].get('maxLength', 40)
                or any(ord(char) < 32 for char in value)
                or ('enum' in properties[key] and value not in properties[key]['enum'])):
            raise ValueError('Invalid argument')


class InfraredVoiceTools:
    def __init__(self, runtime, event):
        self.rt, self.event = runtime, event
        self.pending = None
        self.task = None
        self.target = None

    def _status(self):
        if self.task is None:
            return {'status': 'prepared' if self.pending else 'idle'}
        result = dict(device=self.target[0], action=self.target[1])
        if not self.task.done():
            return dict(result, status='learning', reason='仍在等待原遥控器按键，请对准红外盒子按一下。')
        if self.task.cancelled():
            return dict(result, status='cancelled', reason='本次学习已取消，没有保存新码。')
        if self.task.exception() is not None:
            return dict(result, status='failed', reason='未学到按键。请检查遥控器电池、对准红外盒子，重新准备后再试。')
        return dict(result, status='learned', reason='按键红外码已保存在本地。是否有效需用户另行要求试发并观察家电。')

    async def close(self):
        self.pending = None
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        self.target = None

    async def call(self, name, args):
        if name == 'list_infrared_devices':
            return {'status': 'ok', 'devices': [dict(name=model.name, enabled=model.enabled,
                high_risk=model.is_high_risk, actions=[dict(name=action, learned=bool(model.codes.get(action)))
                    for action in model.actions]) for model in self.rt.store.get_all_appliances()]}
        if name == 'get_infrared_learning_status':
            return self._status()
        if name == 'cancel_infrared_learning':
            saved = self.task is not None and self.task.done() and not self.task.cancelled() and self.task.exception() is None
            await self.close()
            return {'status': 'cancelled', 'reason': '学习已停止；之前已保存的按键仍保留。' if saved else '已取消准备或未完成的学习，没有删除已保存按键。'}
        if not hasattr(self.rt, 'ir_service') or not hasattr(self.rt, 'learning'):
            return {'status': 'unavailable', 'reason': '本地红外模块不可用，请检查红外盒子连接。'}
        if self.rt.learning.active:
            return {'status': 'busy', 'reason': '红外正在学习，请先完成或取消当前学习。'}
        if name == 'prepare_infrared_learning':
            device, action = args['device'].strip(), args['action'].strip()
            model = self.rt.store.get_appliance(device)
            if model is not None and (not model.enabled or model.codes.get(action)):
                return {'status': 'rejected', 'reason': '设备已禁用或此按键已经学习；本次不会覆盖。可查询已学按键。'}
            if model is None and len(self.rt.store.get_all_appliances()) >= 64:
                return {'status': 'rejected', 'reason': '设备数量已达上限，请在后台整理设备。'}
            if model is not None and len(model.actions) >= 64 and action not in model.actions:
                return {'status': 'rejected', 'reason': '此设备的按键数量已达上限。'}
            self.task, self.target = None, None
            self.pending = (device, action, args['category'], time.monotonic())
            return {'status': 'prepared', 'device': device, 'action': action,
                'instructions': '拿好家电的原装红外遥控器，把发射端对准连接 LIRA 的 BroadLink 红外盒子，距离约10到30厘米。先不要按键，准备好后说准备好了；我会开启约10秒学习窗口，再立即按一下目标按键。不是让你对着 LIRA 麦克风按。空调请先设好完整目标状态，再按对应键；蓝牙或射频遥控不支持。'}
        if name == 'start_infrared_learning':
            pending, self.pending = self.pending, None
            if pending is None or time.monotonic() - pending[3] > 120:
                return {'status': 'not_prepared', 'reason': '请先说明要配对的设备和按键，重新完成准备步骤。'}
            device, action, category, _ = pending
            model = self.rt.store.get_appliance(device)
            if model is not None and (not model.enabled or model.codes.get(action)):
                return {'status': 'rejected', 'reason': '配置已变化：设备禁用或按键已存在，不会覆盖。'}
            if model is None:
                model = ApplianceModel(name=device, is_high_risk=category == '其他')
            model.actions.setdefault(action, ())
            self.rt.store.upsert_appliance(model)
            try:
                self.task = self.rt.learning.begin(device, action)
            except ApplianceError:
                return {'status': 'busy', 'reason': '已有红外学习进行中，请稍后再试。'}
            self.target = (device, action)
            self.task.add_done_callback(self._finished)
            return {'status': 'learning', 'device': device, 'action': action,
                'instructions': '现在立即对准红外盒子按一下目标按键，约10秒内完成。按完说按好了，我会查询学习结果。'}
        if name == 'control_infrared_device':
            try:
                await self.rt.ir_service.send_ir(args['device'].strip(), args['action'].strip())
            except ApplianceError as exc:
                return {'status': 'rejected', 'reason': exc.kind,
                        'instructions': '没有发送。未学习请先配对；设备禁用时不能控制。高危设备暂不通过这个语音工具控制，请使用原遥控器。'}
            except HalError:
                return {'status': 'failed', 'reason': '红外发送失败，请检查红外盒子供电与网络。'}
            return {'status': 'sent', 'reason': '红外指令已发出；红外是单向通信，请用户观察家电，不能声称状态已确认。'}
        raise ValueError('Unsupported IR tool')

    def _finished(self, task):
        # Retrieve background exceptions and emit a local, code-free audit event.
        if task is self.task:
            status = self._status()['status']
            self.event('infrared', f'红外学习结果：{status}；设备={self.target[0]}；按键={self.target[1]}')
