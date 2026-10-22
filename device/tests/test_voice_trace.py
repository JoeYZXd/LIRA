import json
from lira.voice_trace import minimize_teaching, redact

def test_nested_teaching_content_is_minimized_but_legacy_survives():
    secret='synthetic child OCR preference result'
    event={'type':'response.function_call_arguments.done','items':[{'name':'capture_page','call_id':'call1','arguments':json.dumps({'text':secret})}]}
    result=minimize_teaching(event)
    assert secret not in json.dumps(result)
    assert result['items'][0]['call_id']=='call1'
    assert secret not in json.dumps(minimize_teaching({'type':'response.output_text.done','text':secret},sensitive_context=True))
    old={'name':'identify_current_speaker','result':{'status':'recognized','name':'existing diagnostic'}}
    assert minimize_teaching(old)==old
    assert redact({'arguments':json.dumps({'api_key':'hidden'})})['arguments']==json.dumps({'api_key':'<REDACTED>'})

def test_history_minimizes_both_entrypoints_and_related_echoes(tmp_path):
    from lira.appliances.store import ApplianceStore
    from lira.voice_history import VoiceHistory
    secret='SYNTHETIC_PRIVATE_OCR_8291'
    history=VoiceHistory(ApplianceStore(tmp_path/'trace.db'))
    history.begin()
    history.event('user_text',secret)
    history.trace('received','conversation.item.input_audio_transcription.completed',{'text':secret})
    history.trace('received','response.function_call_arguments.done',{'items':[{'name':'capture_page','call_id':'call1','arguments':json.dumps({'text':secret})}]})
    history.trace('local','tool.capture_page',{'arguments':{'text':secret},'result':{'status':'ok','text':secret}})
    history.event('assistant_text','I saw '+secret)
    history.trace('received','response.output_text.done',{'text':'I saw '+secret})
    dump=' '.join(str(tuple(row)) for table in ('voice_history_turns','voice_history_diagnostics') for row in history.db.execute('SELECT * FROM '+table))
    assert secret not in dump
    assert 'capture_page' in dump
    history.end();history.begin();history.event('user_text','ordinary conversation')
    assert history.db.execute('SELECT user FROM voice_history_turns ORDER BY id DESC LIMIT 1').fetchone()[0]=='ordinary conversation'

def test_actual_event_sink_masks_teaching_echo_but_preserves_identity(monkeypatch):
    from contextvars import ContextVar
    from types import SimpleNamespace
    import lira.conversation_paths as module
    paths=module.ConversationPaths.__new__(module.ConversationPaths)
    paths.capabilities_enabled=False
    paths._callback_generation=ContextVar('test_callback_generation',default=None)
    paths.rt=SimpleNamespace(privacy=SimpleNamespace(is_on=False),telemetry=object())
    paths.tv=SimpleNamespace(event=lambda *args:None)
    paths.dictation=SimpleNamespace(event=lambda *args:None)
    paths.history=SimpleNamespace(_teaching_sensitive=True,event=lambda *args:None)
    seen=[]
    monkeypatch.setattr(module,'safe_add_event',lambda *args:seen.append(args))
    paths._event('assistant_text','SYNTHETIC_OCR_PRIVATE')
    paths._event('identity','existing identity diagnostic')
    assert seen[0][-1]=='[teaching content omitted]'
    assert seen[1][-1]=='existing identity diagnostic'
