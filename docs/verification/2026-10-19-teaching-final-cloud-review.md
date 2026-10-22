# 审查后真实云端复验

隔离板端 `/tmp` 源码覆盖，不替换运行服务，不使用家庭麦克风、扬声器、摄像头或私有教材。合成 TTS 输入与静音按速率消费 PCM，现有豆包端点及消息格式不变。产品源码摘要见[最终源码](2026-10-19-teaching-final-source.json)。本文件区分协议通过、模型消费失败与探针问题。

## U1 协议

全部关闭竞态修复后，最终 audio `f5354685f06dd93a3a895b433462fc353f942ba9282b2d6a9bc2345061fb8bc4` 四类场景各三次仍 **12/12**，每次执行一次，无提前编造、无会话传输失败，进程正常退出且未触发外部超时。[最终原始回执](2026-10-19-teaching-capabilities-u1-final-reviewed.json)、[最终门禁](2026-10-19-teaching-capabilities-u1-final-reviewed-gate.json)。

容量修复后、关闭竞态修复前的另一次 12/12 亦[保留](2026-10-19-teaching-capabilities-u1-final.json)，其[快照门禁](2026-10-19-teaching-capabilities-u1-final-gate.json)不冒充最终源码。一个中间关闭版本的复验因发现后续迟到回调问题而主动停止，不计入通过门禁。

## U8 计时

关闭竞态修复前，普通/插话各三次的最近完整矩阵 **1/6**，见[完整回执](2026-10-19-teaching-capabilities-u8-final.json)。该快照源码为 audio `d3c1c9e398edc9dec9dab0269edcd3653d29648c3f5ecb8a355d92370231e8ee`、registry `24e4f5a097dacfb537a8533751c15161a0b50519fd250a632c87ba5e63dee243`、timers `beef0aa29b8d8120f45e3e9f31c4a28829b3be9f8119090c60680aa35061063e`。下文另列最终源码单次诊断，不将不同快照合并成通过率。

- 普通第 1/2 次、插话第 1/2 次：最终任务仍 proposed，未形成有效约定，没有到期事件。有的调用 activate 后 needs_input，有的继续调用受到 revision/权限冲突。不能把此类失败当计时执行成功，也不手设 active 绕过。
- 普通第 3 次：真实独立核验后 active，创建一个计时，wait 取得事件，后续 get 读回完整随机 event_id，符合通过判据。
- 插话第 3 次：真实 active，wait 本地结果包含已到期事件 `b94f81798c404334bf03c5504946c388`，operation `516a9cc9458f458695af99832a11bb13` 为 completed。模型后续却说该操作 unknown、无可读内容，未读回事件。

最后一条旧探针只记录调用名和本地审计快照，没有 get 的实际参数/返回体。因此能确认消费未通过，**不能据此断言查错 ID 或传输丢失**。初步口头“查询错误操作”的归因已撤回。已补探针记录真实 executor 调用参数和结果，并把本地审计读取排除出模型消费记录；进行一次有界诊断，不改变模型角色、提示词、处理器或约定核验。

额外诊断先遇到[关闭竞态](2026-10-19-teaching-close-race.md)，没有生成可用回执，故不拿它归因消费问题。关闭及跨 await 保护修复后，最终 audio `f5354685f06dd93a3a895b433462fc353f942ba9282b2d6a9bc2345061fb8bc4` 的[带完整参数/结果诊断](2026-10-19-teaching-capabilities-u8-close-fixed-diagnostic.json) **1/1**，[检查点](2026-10-19-teaching-capabilities-u8-close-fixed-checkpoint.json) `cleanup_pending=false`。独立核验响应为 `{"accepted":true}`、finish_reason=stop、2.865 秒；实际 wait 返回随机事件，当前轮 list 后 get 正确 operation_id `915dee0e01794880bb320e73007f17a5`，得到真实事件 `005bb7baabdd471e86b30a1f85dc4276`，模型完整读回。没有伪造 active、工具结果或用户消息。

这条确认实际结果可消费、关闭可完成；未复现旧 unknown 的原因，不能替旧记录补参数或以单次成功覆盖先前 1/6。此后停止模型提示词重试，支持稳定性门槛继续阻断。

旧首版角色源码曾为 6/6，只作为历史，不覆盖当前 1/6。U8 模型支持声明阻断；确定性计时、终态保留与取消的本地回归另有通过证据。

## U5 播报

[首次最终复验](2026-10-19-teaching-capabilities-u5-final.json)为 2/3：打断与取消符合，complete 的 PCM 已消费但业务返回 execution_failed。源码检查定位为探针仍用仅含 phase 的 SimpleNamespace，U7 增加播报完成证据后需要 generation/owner；该夹具不能代表生产 AgentTaskState。探针改用真实 AgentTaskState 的 idle 会话，未设置 active，也未修改产品播放逻辑、合成语料或协议；[同输入重验](2026-10-19-teaching-capabilities-u5-final-state-fixed.json)为 **3/3**。完成场景会话累计消费 210628 帧（包括普通回复），业务 completed 另由逐段排空证据约束；打断返回 interrupted，取消为 cancelled。首次失败不删除。

U10 的排队初始话术与约定状态误报问题仍见[36 条矩阵人工复核](2026-10-19-teaching-capabilities-u10-matrix-review.md)，不因单段 PCM 排空就解除模型支持阻断。

关闭竞态与迟到回调全部修复后，最终同一 audio `f5354685…` 再次执行三项，[最终源码回执](2026-10-19-teaching-capabilities-u5-final-reviewed.json) **3/3**，探针正常退出，未触发外部超时。精确播报的生产代码、语料和协议未因上述探针修正而改变。此结果仍是静音 PCM 消费证据，不代表物理扬声器。
