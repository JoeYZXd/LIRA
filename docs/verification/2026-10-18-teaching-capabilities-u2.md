# U2 任务与会话约定执行证据

状态：本地实现与集成复核通过，未提交或部署。原计划 r5 不变，实施继续 U3。

新增会话任务读取、提议、激活、修订、暂停、停止能力。提议不等于生效约定，只有本地实际观测的当前用户语音轮次才能参与确认；来源有 120 秒有效期，独立语义核验前后复核 generation、owner、revision 和最新轮次。工具参数不能提供用户发言或以 confirmed=true 授权。语义不明确、服务不可用或结果结构异常均保持 needs_input。

孩子主动完整设定条款可直接作为来源；简短同意必须对应实际播放器成功排空、未被打断且在本次用户发言之前送达的提议。仅生成 assistant_text 不算已讲明。已有有效约定持续生效，修订暂停旧权限，不编排教学脚本。

语义核验复用现有文字模型配置的 SDK，执行隔离的非流式、无记忆/搜索/播报的结构化判断；孩子交流仍在原豆包实时会话，未调用或修改经典聊天路由。首次真实核验返回 HTTP 400，原因是设备模型 kimi-k3 只允许默认温度；已移除新增 temperature=0 并补兼容测试。

真实模型合成矩阵：明确完整设定、同意已讲明提议、拒绝、含糊、未讲明提议、无关回答、疑问、提示注入，共 8 类各 3 次，**24/24 通过**。完整结果见[语义核验记录](2026-10-18-teaching-capabilities-u2-semantic.json)，复现入口为 device/scripts/probe_agent_agreement.py。此矩阵仅证明所列输入，不证明语义模型永不误判；来源、身份、时效和版本由本地硬校验。

身份集成另增 track_generation，补足原 epoch 不覆盖多证据确认换人及恢复旧说话人的情况；保留原 epoch 语义。换人清空任务及约定、关闭旧 executor、取消旧扩展工具任务、清除旧操作与扩展调用缓存，旧操作读取为 unknown，迟到结果不能回传。新 executor 可用，legacy 调用缓存保留。关闭、隐私与切链路仍走已有会话生命周期。

主执行方权威验证：

```text
cd device
.venv/bin/python -m pytest tests/test_agent_task_state.py tests/test_realtime_capabilities.py tests/test_conversation_paths.py tests/test_voice_trace.py tests/test_voice_history.py tests/test_realtime_identity.py tests/test_realtime_enrollment.py tests/test_doubao_lab.py -q
112 passed in 18.45s
```

Worker 失败先行证据：缺少 agent_task_state 模块；同 epoch 真实换人未清约定；供应商默认温度契约失败。补齐测试覆盖版本冲突、来源伪造、TTL、迟到核验、实际播报确认、真实换人及恢复、旧后台任务取消与结果隔离。

代码：device/lira/agent_task_state.py、conversation_paths.py、audio/doubao_realtime.py、realtime_capabilities.py、realtime_identity.py；对应 tests/test_agent_task_state.py、test_conversation_paths.py、test_realtime_capabilities.py、test_realtime_identity.py。U8 可使用 agent_task.permits(revision, generation, owner) 校验约定权限。

经典链路未扩展；开关默认关闭。尚未通过 U3–U10 的资料、OCR、真实播报/投屏和整体自主性验收，不能将本单元通过等同教学功能全部完成。
