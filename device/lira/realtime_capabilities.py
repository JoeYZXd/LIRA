"""Small realtime-only tool registry and session-scoped operation ownership.

Handlers receive validated arguments. Background handlers return an operation
immediately; deferred handlers keep the original call open without blocking recv.
No framework or teaching policy lives here.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import math
import hashlib
import json
import uuid


def validate_value(value, schema):
    kind = schema.get('type')
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'boolean': isinstance(value, bool),
             'integer': isinstance(value, int) and not isinstance(value, bool),
             'number': isinstance(value, (int, float)) and not isinstance(value, bool)}
    if kind not in valid or not valid[kind]:
        raise ValueError('Invalid argument type')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('Invalid argument choice')
    if kind == 'object':
        props = schema.get('properties', {})
        if not set(schema.get('required', ())) <= value.keys():
            raise ValueError('Missing arguments')
        if schema.get('additionalProperties', False) is False and value.keys() - props.keys():
            raise ValueError('Unknown arguments')
        for key, item in value.items():
            if key in props:
                validate_value(item, props[key])
    elif kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 100):
            raise ValueError('Invalid array length')
        for item in value:
            validate_value(item, schema['items'])
    elif kind == 'string':
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 8192):
            raise ValueError('Invalid string length')
    elif kind in ('integer', 'number'):
        if not math.isfinite(value) or value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise ValueError('Invalid number')


@dataclass(frozen=True)
class Capability:
    spec: dict
    handler: object
    availability: object
    background: bool
    sensitive: bool


class CapabilityRegistry:
    def __init__(self):
        self.entries = {}

    def __contains__(self, name):
        return name in self.entries or name in CONTROL_NAMES

    def register(self, name, description, parameters, handler, *, availability=None,
                 background=False, sensitive=True):
        if name in self.entries or name in CONTROL_NAMES:
            raise ValueError('Duplicate or reserved capability')
        if not isinstance(name, str) or not name or len(name) > 64 or not name.isascii() or not name.replace('_', '').isalnum():
            raise ValueError('Invalid capability name')
        if sensitive:
            from lira.voice_trace import register_sensitive_tool
            register_sensitive_tool(name)
        spec = dict(type='function', name=name, description=description,
                    parameters=copy.deepcopy(parameters))
        self.entries[name] = Capability(spec, handler, availability, background, sensitive)

    def validate(self, name, args):
        if name in CONTROL_NAMES:
            spec = next(s for s in CONTROL_TOOLS if s['name'] == name)
        elif name in self.entries:
            spec = self.entries[name].spec
        else:
            raise ValueError('Unknown capability')
        validate_value(args, spec['parameters'])

    @property
    def tools(self):
        return copy.deepcopy([c.spec for c in self.entries.values()] + CONTROL_TOOLS)

    def status(self):
        return [{'name': name, 'status': self.available(name)} for name in self.entries]

    def available(self, name):
        check = self.entries[name].availability
        try:
            state = check() if check else 'configured'
        except Exception:
            return 'unavailable'
        return state if state in ('configured', 'unavailable', 'busy') else 'unavailable'


_OP_ID = {'type': 'string', 'minLength': 1, 'maxLength': 64}
CONTROL_TOOLS = [dict(type='function', name='list_realtime_capabilities',
    description='查询本会话扩展能力的实际可用状态。',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False})]
CONTROL_TOOLS += [dict(type='function', name='list_capability_operations', description='列出本会话操作标识和状态；实际结果须再查询get_capability_operation。', parameters={'type':'object','properties':{},'additionalProperties':False})]
CONTROL_TOOLS += [dict(type='function', name=name, description=description,
    parameters={'type': 'object', 'properties': {'operation_id': _OP_ID},
                'required': ['operation_id'], 'additionalProperties': False})
    for name, description in (('get_capability_operation', '查询本会话后台操作的真实状态。'),
                              ('cancel_capability_operation', '取消本会话后台操作。'))]
CONTROL_NAMES = frozenset(s['name'] for s in CONTROL_TOOLS)


# Fixed 64 KiB, four SHA-256-derived bits: no retired ID is ever forgotten.
# False positives fail closed as expired (~0.0004 at 20,000 retired IDs).
# At extreme saturation new IDs may also expire; never re-execute an old ID.
class RetiredIds:
    def __init__(self): self.bits = bytearray(65536)
    def _positions(self, value):
        digest = hashlib.sha256(value.encode()).digest()
        return [int.from_bytes(digest[i:i+4], 'big') % (len(self.bits)*8) for i in range(0,16,4)]
    def __contains__(self, value):
        return all(self.bits[p//8] & (1 << (p%8)) for p in self._positions(value))
    def add(self, value):
        for p in self._positions(value): self.bits[p//8] |= 1 << (p%8)

# These handlers revoke permissions/output. They have bounded reserved capacity,
# independent of ordinary reads or long-running work.
REVOCATION_NAMES = frozenset(('pause_agent_task', 'stop_agent_task',
    'cancel_task_timer', 'clear_tv_content', 'cancel_capability_operation'))


class CapabilityExecutor:
    def __init__(self, registry, *, generation=None, max_operations=32, max_calls=64,
                 timeout=60, on_change=None):
        self.registry = registry
        self.generation = generation or uuid.uuid4().hex
        self.max_operations, self.max_calls, self.timeout = max_operations, max_calls, timeout
        self.on_change = on_change
        self.closed = False
        self.operations, self.calls = {}, {}
        self.retired_calls, self.retired_operations = RetiredIds(), RetiredIds()

    async def call(self, name, args, call_id):
        self.registry.validate(name, args)
        if self.closed:
            return {'status': 'cancelled'}
        if call_id in self.calls:
            old_name, old_args, task = self.calls[call_id]
            if (name, args) != (old_name, old_args):
                return {'status': 'rejected', 'reason': 'call_id_conflict'}
            return copy.deepcopy(await asyncio.shield(task))
        self._trim_calls()
        if call_id in self.retired_calls:
            return {'status':'expired', 'reason':'call_result_expired'}
        active = sum(not row[2].done() for row in self.calls.values())
        if active >= self.max_calls + (8 if name in CONTROL_NAMES | REVOCATION_NAMES else 0):
            return {'status': 'busy'}
        task = asyncio.create_task(self._call(name, copy.deepcopy(args)))
        self.calls[call_id] = (name, copy.deepcopy(args), task)
        result = copy.deepcopy(await asyncio.shield(task))
        self._trim_calls()
        return result

    def _trim_calls(self):
        completed = [key for key, row in self.calls.items() if row[2].done()]
        for key in completed[:-self.max_calls]:
            self.retired_calls.add(key)
            del self.calls[key]

    def _trim_operations(self):
        completed = [key for key, op in self.operations.items() if op['task'].done()]
        for key in completed[:-self.max_operations]:
            self.retired_operations.add(key)
            del self.operations[key]

    async def _call(self, name, args):
        if name == 'list_realtime_capabilities':
            return {'status': 'ok', 'capabilities': self.registry.status()}
        if name == 'list_capability_operations':
            return {'status':'ok', 'operations':self.metadata()}
        if name in CONTROL_NAMES:
            operation = self.operations.get(args['operation_id'])
            if operation is None:
                return {'status': 'expired' if args['operation_id'] in self.retired_operations else 'unknown'}
            if name == 'cancel_capability_operation' and operation['status'] == 'pending':
                operation['status'] = 'cancelled'
                operation['task'].cancel()
                await self._notify()
            return self._snapshot(operation)
        state = self.registry.available(name)
        if state != 'configured':
            return {'status': state}
        cap = self.registry.entries[name]
        active = sum(not op['task'].done() for op in self.operations.values())
        if active >= self.max_operations + (8 if name in REVOCATION_NAMES else 0):
            return {'status': 'busy'}
        operation = {'operation_id':uuid.uuid4().hex, 'status':'pending', 'name':name, 'args_hash':hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        self.operations[operation['operation_id']] = operation
        operation['task'] = asyncio.create_task(self._complete(operation, cap, args))
        operation['task'].add_done_callback(lambda _: self._trim_operations())
        await self._notify()
        if not cap.background:
            await asyncio.shield(operation['task'])
            return copy.deepcopy(operation.get('result', {'status':operation['status']}))
        return self._snapshot(operation)

    def metadata(self):
        return [{k:op[k] for k in ('operation_id','name','status')} for op in self.operations.values()]

    async def _notify(self):
        if self.on_change and not self.closed:
            await self.on_change()

    async def _run(self, cap, args):
        try:
            result = await asyncio.wait_for(cap.handler(args), self.timeout)
            if self.closed:
                return {'status': 'cancelled'}
            if not isinstance(result, dict) or not isinstance(result.get('status'), str):
                return {'status': 'failed', 'reason': 'invalid_result'}
            return result
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return {'status': 'failed', 'reason': 'timeout'}
        except Exception:
            return {'status': 'failed', 'reason': 'execution_failed'}

    async def _complete(self, operation, cap, args):
        try:
            result = await self._run(cap, args)
            if not self.closed and operation['status'] == 'pending':
                status = result['status']
                terminal = ('completed' if status in ('ok', 'success', 'completed') else
                            'cancelled' if status in ('cancelled', 'interrupted') else 'failed')
                operation.update(status=terminal, result=result)
                self._trim_operations()
                await self._notify()
        except asyncio.CancelledError:
            operation['status'] = 'cancelled'

    @staticmethod
    def _snapshot(operation):
        return copy.deepcopy({k: v for k, v in operation.items() if k != 'task'})

    async def close(self):
        self.closed = True
        tasks = [v[2] for v in self.calls.values()]
        for op in self.operations.values():
            if op['status'] == 'pending':
                op['status'] = 'cancelled'
            tasks.append(op['task'])
        for task in tasks:
            task.cancel()
        # Revocation must not wait indefinitely for a provider swallowing cancellation.
        if tasks:
            await asyncio.wait(set(tasks), timeout=1)
            for task in set(tasks):
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        self.calls.clear()
        self.operations.clear()
