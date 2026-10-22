# U7 教学记忆、证据与家长核对执行证据

本地实现及独立语义核验通过，未提交、部署或修改计划。经典摘要与路由算法保持原样。

新表独立区分长期偏好、未核实学习建议、纸面家长结果、背诵证据及附注。模型只引用本地实际当前turn，不可选member_id；真实确认身份、来源时效、会话与异步前后状态均校验。当前约定优先；临时安排不写长期偏好。家长核对无模型工具入口，修订只允许paper_review，原始背诵证据不可覆盖；更正另存annotation。request_id重放必须目标、版本、载荷全同。

长期偏好独立语义核验首轮26/27，一次自然表达被保守拒绝；[诊断](2026-10-18-teaching-capabilities-u7-preference-diagnostic.json)证实150token回复预算导致finish_reason=length且正文空，并非越权保存。预算提高至300、仍限10秒，length一律拒绝。最终9类各3次，**27/27通过**，含明确长期/自然表达、今天/本次、否定、疑问、转述、注入及自报掌握，见[最终矩阵](2026-10-18-teaching-capabilities-u7-preference-final.json)。保留[首次记录](2026-10-18-teaching-capabilities-u7-preference-first.json)，无静默重跑替换失败。复现脚本device/scripts/probe_teaching_preference.py；只发送合成语料，未调用经典聊天路由。

背诵对齐只用当前实际ASR与已确认版本，最多8000字、线程中有界比较，保留片段、差异、归一化说明和ASR/完整性不确定性，始终needs_review，不自动判掌握。实际成功排空的精确播报才可作正向音频证据，仅关联观测前发生的事件；普通回复是否提示未知，空记录不等于无提示，hint_usage/mastery保持unknown。

遗忘先撤销权限及来源，关闭旧实时上下文，清教学表、相关会话历史/诊断、关联缓存；新上下文可重新学习。cleanup使用实时_stop，不打断既有经典forget确认播报。成员删除UI await清理后成功。purge_content使用结构化teaching_record_refs清所有成员关联证据和annotation，不用正文匹配。管理路由删除资料仍须先关闭旧cloud、purge、再删原文，U9负责鉴权及调用顺序。

Red：新增模块缺失；补真实SQLite/registry及生命周期测试。Root权威六组：

```text
.venv/bin/python -m pytest tests/test_teaching_memory.py tests/test_teaching_evidence.py tests/test_personal_memory.py tests/test_conversation_paths.py tests/test_realtime_speech.py tests/test_ui_chat.py -q
154 passed in15.54s
```

变更：device/lira/teaching_memory.py、teaching_evidence.py、conversation_paths.py、realtime_speech.py、personal_memory.py（仅forget hook）、ui/app.py、ui/chat.py、main.py及tests/test_teaching_memory.py、test_teaching_evidence.py。原有用户WIP保留。

管理API：parent_records(member_id)、parent_review(...)、parent_annotate_recitation(...)、purge_content(content_id)、await paths.forget_member(member_id)。调用者必须家长鉴权。模型工具仅read_teaching_memory、propose_teaching_memory、compare_observed_recitation，成功transport status=ok且领域review_state独立，实际executor operation completed已覆盖。
