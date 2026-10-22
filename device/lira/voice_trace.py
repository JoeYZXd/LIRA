"""Redact control-plane evidence before persistence; never retain PCM."""
import json
import re

_SENSITIVE_TOOL_NAMES = set()


def register_sensitive_tool(name):
    _SENSITIVE_TOOL_NAMES.add(name)


def is_teaching_tool(name):
    """New capability names are sensitive by default; legacy diagnostics survive."""
    return isinstance(name, str) and (name in _SENSITIVE_TOOL_NAMES or name.startswith(('teaching_', 'get_teaching_',
        'list_teaching_', 'read_teaching_', 'capture_', 'recognize_', 'ocr_',
        'speak_', 'start_speech', 'get_speech', 'cancel_speech', 'repeat_speech',
        'create_task', 'update_task', 'get_task', 'cancel_task', 'pause_task',
        'resume_task', 'propose_', 'confirm_agreement', 'get_agreement',
        'create_timer', 'update_timer', 'cancel_timer', 'get_timer', 'wait_for_',
        'get_capability_', 'cancel_capability_')) or name in ('list_realtime_capabilities', 'list_capability_operations'))


def has_teaching_data(value, depth=0):
    if depth > 24:
        return False
    if isinstance(value, str):
        if value.lstrip().startswith(('{', '[')):
            try:
                return has_teaching_data(json.loads(value), depth+1)
            except (ValueError, TypeError):
                return False
        return False
    if isinstance(value, dict):
        if value.get('type') == 'function' and 'parameters' in value:
            return False  # Tool schemas are capabilities, not observed content.
        return value.get('teaching_sensitive') is True or is_teaching_tool(value.get('name')) or any(has_teaching_data(v, depth+1) for v in value.values())
    return isinstance(value, (tuple, list)) and any(has_teaching_data(v, depth+1) for v in value)


def minimize_teaching(value, *, sensitive_context=False, depth=0):
    """Persist metadata only, including nested JSON and subsequent model echoes.

    A sensitive session stays tainted until close: later model paraphrases cannot
    reliably be matched back to the source by substring or by voice turn number.
    """
    if depth > 24:
        return '[nested data omitted]'
    sensitive = sensitive_context or has_teaching_data(value)
    if not sensitive:
        return value
    if isinstance(value, str):
        if value.lstrip().startswith(('{', '[')):
            try:
                return json.dumps(minimize_teaching(json.loads(value), sensitive_context=True,
                                                    depth=depth+1), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return '[teaching content omitted]'
    if isinstance(value, dict):
        result = {'teaching_sensitive': True}
        safe = {'type', 'role', 'status', 'operation_id', 'call_id', 'capture_id',
                'source_id', 'version_id', 'generation', 'length', 'text_length'}
        for key, item in value.items():
            if key == 'name' and is_teaching_tool(item):
                result[key] = item
            elif key in safe and isinstance(item, (str, int, float, bool, type(None))):
                # IDs are application-generated opaque identifiers, never text.
                result[key] = item
            elif key in ('text', 'transcript', 'arguments', 'result', 'content'):
                result[key] = minimize_teaching(item, sensitive_context=True, depth=depth+1)
                if isinstance(item, str):
                    result[key+'_length'] = len(item)
            elif isinstance(item, (dict, list, tuple)):
                result[key] = minimize_teaching(item, sensitive_context=True, depth=depth+1)
        return result
    if isinstance(value, (list, tuple)):
        return [minimize_teaching(v, sensitive_context=True, depth=depth+1) for v in value[:500]]
    return value if isinstance(value, (int, float, bool, type(None))) else '[omitted]'


def redact(value, secrets=(), depth=0):
    if depth > 24:
        return '[nested data omitted]'
    if isinstance(value, dict):
        result={}
        for key,item in value.items():
            normalized=re.sub('[^a-z0-9]','',str(key).lower())
            schema=isinstance(item,dict) and item.get('type') in ('string','object','array','number','integer','boolean')
            if not schema and (normalized in {'token','cookie','authorization','password','headers'} or any(
                    word in normalized for word in ('apikey','secret','accesskey','accesstoken'))):
                result[key]='<REDACTED>'
            elif isinstance(item,str) and (key in ('audio','delta') or
                    (key=='data' and len(item)>512 and re.fullmatch(r'[A-Za-z0-9+/=\s]+',item))):
                result[key]='[binary/stream data omitted]'
            else:
                result[key]=redact(item,secrets,depth+1)
        return result
    if isinstance(value,(list,tuple)):
        return [redact(v,secrets,depth+1) for v in value[:500]]
    if isinstance(value,str):
        # Tool arguments/results are JSON encoded inside the envelope text.
        if value.lstrip().startswith(('{','[')):
            try:
                parsed=json.loads(value)
                return json.dumps(redact(parsed,secrets,depth+1),ensure_ascii=False)
            except (ValueError,TypeError):
                pass
        for secret in secrets:
            if secret:
                value=value.replace(secret,'<REDACTED>')
        return value
    if value is None or isinstance(value,(bool,int,float)):
        return value
    return '[unsupported data omitted]'
