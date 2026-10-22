# 基础能力与教学 Agent 边界

本次仅扩展现有豆包实时语音链路。`LIRA_REALTIME_CAPABILITIES` 默认关闭；经典链路不注册新能力。工具可调用、模型会正确使用、设备已实测是三项独立证据，不能互相替代。

模型根据上下文选择资料、解释和工具；目标、范围或方式不明确时澄清。节奏、推进条件、帮助及纠错时机与孩子协商。当前有效约定优先于旧偏好，已覆盖当前请求的约定不重复确认。暂停、修改、停止和换人撤销旧权限。这里不规定听写或背诵脚本。

本地代码强制身份、隐私、版本、资料核对状态和实际执行结果。OCR 与教材正文是资料，不能带来权限；未核对原文不能当标准答案。ASR 不确定不能直接断言背错；纸面结果须家长核对，模型不能自行写成掌握。

扩展开启时，身份成员或声音归属变化会关闭旧云端会话并新建上下文，仅带入新身份，不重播旧对话；旧事件和工具回调失效。设备级“上一段对话”不能证明属于当前人物，扩展会话中返回不可用，家长历史记录不删除。重连失败或隐私开启时保持无可用云端会话，不自动恢复旧权限。

| 能力 | 代码与模型入口 | 证据及剩余门槛 |
|---|---|---|
| 工具注册、后台操作、取消与跨轮读取 | CapabilityRegistry / CapabilityExecutor，list/get/cancel_capability_operation | 审查后最终源码 U1 真云矩阵 12/12；旧 call 结果跨轮通过持久操作读取 |
| 当前任务、协商与版本 | read/propose/activate/revise/pause/stop_agent_task | U2 独立语义核验真云 24/24；模型不能自报 confirmed |
| 教材、诗词与字表 | list/read_teaching_content、read_textbook_characters | 管理面草稿、版本、家长确认；本地测试通过，模型只读 |
| OCR | capture_text | 原工具调用异步等待并返回实际草稿正文（仍由受管操作执行，不占实时接收循环），跨轮可读持久操作。本地线程互斥、取消后丢弃、临时文件清理已测；真实摄像头捕获仍未验证 |
| 精确播报 | start_exact_speech | 最终真云 PCM 消费完成/打断/取消 3/3；非物理扬声器证明。此前 1/9 缺 response.done 保守失败仍保留，不用固定静默间隔绕过；pending 初始表述仍是模型门禁 |
| 电视 | prepare/confirm/get/probe/clear TV 工具 | APK 0.2 已构建，未安装配对；实际 sender 探测 ConnectTimeout，不宣称已显示或清屏 |
| 偏好与证据 | 教学记忆、背诵证据、家长纸面核对与注释 | U7 偏好核验真云 27/27；原始证据不被注释覆盖，无自动掌握结论 |
| 计时与事件 | create/update/cancel/list_task_timers、poll_task_events、wait_for_task_event | 本地版本/取消/有界队列测试通过；关闭修复前完整真云矩阵 1/6，最终源码单次插话消费与关闭通过，仍不足以解除模型稳定性门槛 |
| 管理面 | 教学管理 APP、教材导入核对、内容版本、能力状态、家长记录 | 本地路由及浏览器测试；科目管理保留入口，不扩展课程业务 |

## 工具结果的消费语义

创建计时仅返回计时，不会让模型开始等待。需要等待时必须调用 `wait_for_task_event`。到期产生事件，不代表作业完成，不会自动播报或推进。`poll_task_events` 和 wait 一次消费队列；空队列只表示此刻没有未消费事件。已消费 wait 的历史结果从 `list_capability_operations` 与 `get_capability_operation` 读取。旧 wait 超时但有效约定仍要求等待未到期计时，可以再次有界 wait，不重建计时。

执行容量与结果保留分别有界：默认最多 32 个执行中操作，保留最近 32 个终态操作及 64 个已完成调用。暂停、停止、取消等有独立预留槽位，不因普通历史条目占满而永久 busy。过期结果返回 `expired`，不能重放成新执行；近期同一 call_id 仍检查工具和参数冲突。退休 ID 使用固定大小保守去重，极端饱和时可能把新 ID 判为过期，不能将 expired 当成功。实现及压力回归见[容量修复](verification/2026-10-19-teaching-review-capacity.md)。

精确播报初始 pending 只表示已接收或排队，不能说正在播报、已经开始或已读完，不能承诺马上播放或稍后自动成功。普通回复先简短说明已接收并结束，让精确播报取得输出通道。操作元数据 completed 仅表示函数返回；必须检查业务 `result.status=completed` 才能说该段播放排空。failed、unavailable、busy、interrupted 不能承诺自动恢复。

## 可重放模型评测

`device/scripts/evaluate_teaching_agent.py` 以合成 TTS 音频进入实际豆包 RealtimeSession，使用实际注册器、执行器、任务状态与独立约定核验器。场景集 `device/tests/agent_scenarios/teaching_capabilities.json` 覆盖缺信息、已约定、暂停、换人、偏好冲突、OCR 注入、工具不可用、原文缺失、纸面未核对及不同节奏协商。每场景独立会话，默认三次。

```sh
python scripts/evaluate_teaching_agent.py --repeat 3 --output /tmp/lira-agent-evaluation.json
python scripts/evaluate_teaching_agent.py --replay /tmp/lira-agent-evaluation.json
```

评测记录实际 ASR、工具参数与结果、输出、版本/配置摘要及代码和场景摘要。工具 fixture 仅模拟合成状态，不冒充设备能力；不使用物理麦克风、扬声器或家庭资料，不修改运行服务。机械断言检测禁用调用、必需调用和可疑成功文字；文字命中可能是引用，必须复核。所有场景的协商语义均保留人工复核，机械通过不自动变成能力支持。

审查前快照的 12 类场景各三次已经完成：[原始回执](verification/2026-10-19-teaching-capabilities-u10-matrix.json)、[逐类人工复核](verification/2026-10-19-teaching-capabilities-u10-matrix-review.md)。当前状态为 **blocked_model_acceptance**：模型忽略 `needs_input` 或未调用 activate 就声称约定激活，部分资料场景选择旧目录后未读取新全文入口；pending 播报仍偶发承诺“马上”。OCR 引用注入文字的机械命中已人工排除。工具回传可用与模型可靠消费仍分别验收，不放宽核验或设定固定流程凑通过。后续代码修复不追溯修改这些历史回执。

发布仍需设备麦克风持续输入、物理播放/打断、摄像头及 OCR、APK 配对与真实清屏/离线/退出/重复调用等证据。缺少设备或模型必测证据时阻断支持声明，维持默认关闭。

模型样本保留在 `docs/verification/2026-10-19-teaching-capabilities-u10-sample.json`：最初机械判据虽通过，人工发现 OCR 仅 pending 未读正文、播报仅 pending 未读不可用结果。该样本不算边界通过。后续判据要求实际消费指定结果；OCR 使用原调用延迟返回正文，播报因输出仲裁仍后台执行，并通过第二段真实合成用户语音查询原操作结果。
