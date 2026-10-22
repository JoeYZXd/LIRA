# U5 精确播报与输出仲裁执行证据

本地实现与集成验证完成；云端已验证成功、打断、取消路径，保留一项未归因时序异常与模型状态表达问题，不能称全部发布门槛通过。未提交或部署。

工具start_exact_speech({text})采用受管后台operation，原格式pending实际发送后，只有随后收到的response.done才放行，再等真实播放器排空，发送现有speech_text_buffer.commit。每次本地调用票据由ContextVar关联，禁止早到事件误放行；同call_id不重复发声。换人、关闭清理票据，输入、暂停/修订、隐私、取消、故障或输出来源冲突均撤销；不自动恢复或推进。原工具输出、身份插话共用占用检查。实际排空及权限有效才completed，并在send锁内核验后写assistant历史。单次精确播报35秒上限，身份插话仍20秒；长资料由模型根据情境决定分段，不编排自动流程。

原deferred策略被真实云证伪：工具结果挂起时云端不保证先发response.done，导致等待屏障超时。失败原记录见[首轮](2026-10-18-teaching-capabilities-u5-first-failed.json)。修复为先pending后关联屏障，未使用新事件或固定静默替代完成信号。

云端修复后九次测试（complete/interrupt/local-cancel各3）中8次符合预期，见[第二轮](2026-10-18-teaching-capabilities-u5-second-probe.json)。完整播报三次均在实际drain后completed；取消三次均cancelled。一次interrupt样例在普通回复drain后未观察到response.done，未开始精确播报，探针等候超时；未误报completed。此异常未充分归因，不能直接声称云端漏事件。

随后为接收协议类型、失败状态补充诊断，按原输入再次运行interrupt三次，**3/3通过**，见[诊断重验](2026-10-18-teaching-capabilities-u5-interrupt-diagnostic.json)。没有复现异常，不删除前次失败，不以重跑代替归因；缺少结束信号仍有界返回busy/失败，禁止猜测完成。探针为device/scripts/probe_realtime_speech.py，静默按真实24kHz速率消费音频，非物理扬声器验收。

另外，普通回复排队期间模型仍有“正在播报”措辞提前的问题，虽然没有误报已读完，仍须纳入U10状态真实性评测，不能将本单元协议证据当作模型自主性通过。

本地red：旧speak_text在audio.done后interrupt仍错误返回True；新增pending发送前early response.done不可放行。当前能力/会话/任务/播报四组由root复核 **66 passed in14.28s**（含U1最新call_id冲突小修）。此前扩展身份/录入/实验室回归115通过。物理播放、整体模型职责仍留后续门槛。

代码：device/lira/realtime_speech.py、audio/doubao_realtime.py、conversation_paths.py、tests/test_realtime_speech.py。U1会话层另补_call_bindings，仅保存工具名和规范参数哈希，同ID改参数/工具在执行和缓存重放前拒绝，合法重放幂等，不覆盖原缓存；换人清扩展绑定，关闭全清。新增test_realtime_capabilities回归已红绿验证。

## PCM证据补强后的复核

先用失败测试重现零PCM也会speak_text=True及发布assistant_delivered的问题，再按每段播放owner分别记录queued/consumed字节、EOS实际消费、对应task成功排空。零PCM、提前返回、上一段音频抵扣下一段均不能完成或生成已讲明历史。身份插话同样拒绝虚假零音频成功。正例改用真实非零PCM与受控消费。

Root最新相关能力、任务、分发、身份、录入和实验室回归 **123 passed in16.26s**。随后使用补强后的实际源码再次云端跑complete、interrupt、local-cancel，**3/3通过**，见[最终PCM重验](2026-10-18-teaching-capabilities-u5-final-pcm.json)。原先missing-done未归因及排队措辞问题仍保留给U10，不被本次成功覆盖。

## 审查后最终源码复验

2026-10-19 第一次复验 complete 在 PCM 排空后返回 execution_failed，检查为探针的 phase-only 夹具没有 U7 完成证据所需 generation/owner；不是生产任务状态。改用真实 AgentTaskState 的 idle 会话，产品代码、语料和协议不变后，完成/打断/取消 **3/3**，见[最终重验](2026-10-19-teaching-capabilities-u5-final-state-fixed.json)。首次失败亦[保留](2026-10-19-teaching-capabilities-u5-final.json)。这不解除 U10 初始 pending 话术或物理扬声器门槛。

全部关闭竞态修复后的最终 audio `f5354685…` 再次执行三项，仍 **3/3** 且正常退出，见[最新源码回执](2026-10-19-teaching-capabilities-u5-final-reviewed.json)。
