# 教学基础能力 U1 实施与协议验证

日期：2026-10-18（Asia/Shanghai）。依据：原教学管理实施计划 r5。本报告记录执行结果，不修改计划正文。

## 当前结果

U1 真实云端门槛已通过（12/12），已恢复原计划实施。下文保留首次失败及修复历史，最终凭据见末节。

U1 的能力注册、参数校验、可用性、非阻塞工具执行、后台操作查询/取消、调用幂等、会话隔离和敏感诊断最小化已在本地实现。扩展由 `LIRA_REALTIME_CAPABILITIES` 控制，默认关闭。未改经典链路、未部署、未提交；原有工作区的在途改动保留。

主执行方在 WSL Python 3.11 复核：`cd device && .venv/bin/python -m pytest tests/test_realtime_capabilities.py tests/test_voice_trace.py tests/test_conversation_paths.py tests/test_voice_history.py -q`，结果 **49 passed in 11.54s**。失败先行证据来自实施 worker：模块实现前，测试因缺少 `lira.realtime_capabilities` 收集失败。

## 首次云端门槛：未通过（历史记录）

三次独立合成语音会话使用设备已有凭据和原 JSON 工具结果消息，不读麦克风、不外放、不执行真实业务工具、不重启现有服务。为检验并发前提，仅在隔离探测中将原 `_tool` 调度为后台任务；这不等于已把新实现部署到设备。

- 工具挂起期间新语音仍可转写并回答，证明接收可持续。
- 含明确暂停请求的一次探测无法证明原结果仍有效，因此另做“查询继续等待、不要取消”的控制探测。
- 控制探测中，工具于 14.803 秒被调用，26.805 秒才返回；模型在 17.083 秒先声称设备在线、服务正常且无异常。
- 实际返回的合成标记是“青色盒子”，后续模型却回答“蓝色的智能音箱”，未可靠使用返回值。
- 会话没有传输失败，但这不代表结果已被模型正确消费。不能由此断言提供方完全不支持延迟工具；尚需确认当前端点的工具结果封装、续接和跨话轮语义。

原计划 U1 明确要求此真实协议门槛通过后才启动依赖机制的 U5/U8。因此 U1 整体验收尚未完成，后续单元尚未启动。未用假用户消息、其他接口的事件类型或固定教学流程绕过。

## 代码与复现

- `device/lira/realtime_capabilities.py`
- `device/lira/audio/doubao_realtime.py`
- `device/lira/conversation_paths.py`
- `device/lira/voice_trace.py`
- `device/lira/voice_history.py`
- `device/tests/test_realtime_capabilities.py`
- `device/tests/test_voice_trace.py`
- `device/scripts/probe_realtime_tool_protocol.py`：板端静默复现脚本，读取运行服务凭据但不输出；仅固定合成语料，结果不代表业务执行。脚本当前依赖板端 TTS 及设备运行环境。

结构化观察：[探测记录](2026-10-18-teaching-capabilities-u1-probe.json)。后续修复及重新验收见下节。未进入代码审查与发布尾段，不能认定完整功能或交付完成。

## 生命周期修复与重新验收：已通过

排除实验表明：原消息封装的立即结果可以消费，延迟 12 秒且没有新语音时同样可以消费；故障集中在跨话轮的旧调用结果续接，不能归结为 JSON 封装字段或延迟本身。

修复保留原 `conversation.item.create / type=message / role=tool` 格式，不增加假用户消息或其他接口事件。所有扩展操作均保留会话内 ID、工具名、参数哈希、真实状态与结果。新语音后不再回传旧轮的迟到结果；通过已有 `session.update` 同步操作元数据，并与身份指令组合。模型在当前轮调用 `list_capability_operations` / `get_capability_operation` 读取实际结果；元数据不包含结果正文。新增通用指引要求“继续等待/查进度”复用原操作，明确的新请求仍可重新执行，未用参数去重限制新意图。

首次修复探测虽读到正确标记，但出现同一查询启动两次，因此未作为放行证据。探针已把执行次数纳入验收；曾出现的诊断回调参数错误也已修正并完整重新运行。

最终使用实际 `CapabilityRegistry`、`CapabilityExecutor` 与 `RealtimeSession` 源码隔离覆盖到板端 `/tmp`，没有替换运行服务。合成处理器延迟 12 秒，分别返回青色盒子、橙色纸船、紫色风筝。四类情况每类三次，共 **12/12 通过**：

| 情况 | 次数 | 实际结果 |
|---|---:|---|
| 延迟、无中途新语音 | 3 | 准确复述实际标记 |
| 延迟、中途新语音 | 3 | 保留原操作，当前轮查询并准确复述 |
| 后台、中途新语音 | 3 | 当前轮查询并准确复述 |
| 用户取消 | 3 | 处理器实际取消，未产生完成结果 |

每次仅执行一次；未发现提前编造结果，会话无传输失败。工具调用、转写、回答和处理器时间线均保留在[最终云端记录](2026-10-18-teaching-capabilities-u1-cloud-fixed.json)，版本及源码指纹见[验收凭据](2026-10-18-teaching-capabilities-u1-gate.json)。[首次修复记录](2026-10-18-teaching-capabilities-u1-first-fix.json)仅作历史，不作放行证据。

主执行方本地复核能力、分发、隐私诊断、历史、身份、录入和实验室测试，共 **101 passed in 17.77s**；后续通用指令小改由 worker 复核能力测试 8 passed。新增回归覆盖跨轮保留查询、旧响应抑制、取消隔离及身份指令组合。U1 协议门槛据此通过，继续 U2。

边界：这是实际云端对合成工具的协议验证，不是 OCR、物理播报、投屏或完整教学效果验收。新能力开关仍默认关闭；未部署、重启或提交，测试后运行服务仍为 active。经典链路和原计划 HTML/Markdown 保持不变。

## 代码审查修复后最终源码复验

2026-10-19 容量与调用绑定修复后，用当前隔离源码重新执行相同四类场景各三次，仍为 **12/12**，每次仅执行一次、无提前编造结果、无会话传输失败。原始时间线见 [最终复验](2026-10-19-teaching-capabilities-u1-final.json)，当前源码摘要见 [最终门禁](2026-10-19-teaching-capabilities-u1-final-gate.json)。不篡改旧回执，也不将 U1 协议通过写成 U10 自主协同或物理设备已通过。

随后发现并修复关闭竞态与迟到回调，最终 audio `f5354685…` 再次真实云端 **12/12**、每次仅执行一次且正常退出。以后以[最新完整回执](2026-10-19-teaching-capabilities-u1-final-reviewed.json)和[最新源码门禁](2026-10-19-teaching-capabilities-u1-final-reviewed-gate.json)为准，上述中间快照保留。
