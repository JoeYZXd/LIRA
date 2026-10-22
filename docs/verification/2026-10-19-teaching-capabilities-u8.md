# U8 计时与事件执行证据

本地实现已通过；最近一次完整真实云端矩阵 **1/6**（关闭竞态修复前），最终源码带完整参数/结果的单次插话诊断 **1/1**，不足以解除稳定性门槛，见[最终复核](2026-10-19-teaching-final-cloud-review.md)。下文保留最初 2/6 与首版角色 6/6 历史，不覆盖后续结果。原计划及脑图未修改，经典链路不动。

`AgentTimers` 绑定当前会话、身份归属和有效约定 revision。创建、修改、取消、查询计时及轮询/等待事件均由模型工具调用。到期只产生带唯一 event_id 的事件，不发声、不自动推进、不代表孩子已经完成。事件只能消费一次；并发等待者中只有一个取得事件，其余继续有界等待。

约定修改、暂停、停止、换人、隐私开启和关闭会立即撤销计时、排队事件和等待。旧回调检查约定与计时版本，不得发布迟到事件。最多32个计时和32个事件，创建时预留待到期事件容量，满时明确 busy，不静默丢弃。

证明先行：新模块缺失导致测试收集失败。补实现及并发消费测试后，root 权威回归：

```text
.venv/bin/python -m pytest tests/test_agent_timers.py tests/test_agent_task_state.py tests/test_realtime_capabilities.py tests/test_conversation_paths.py -q
70 passed in 8.84s
```

真实云端脚本为 `device/scripts/probe_realtime_timers.py`，只发送固定合成语音；使用实际豆包会话、Registry/Executor、AgentTaskState、独立 AgentAgreementVerifier（kimi-k3）及 AgentTimers。没有手工设置 active，没有伪造确认，没有新增云协议事件。静音播放器实际消费 PCM，不接触家庭麦克风或物理扬声器，不改变设备服务。

[首次检查](2026-10-19-teaching-capabilities-u8-first.json)通过：模型从实际语音建立并核验临时约定，只创建一个12秒计时，等待取得真实事件，再完整读回本地产生的随机 event_id。后续检查包括等待期间插话，再在当前轮读取原 operation 的实际结果。

[首次完整矩阵](2026-10-19-teaching-capabilities-u8-matrix.json)保留全部6次结果：普通等待2/3、插话后读取0/3。两次模型创建计时后口头称等待，却没有调用 wait 工具，故没有等待结果；另两次 wait 正常取得并消费真实事件，但模型在插话后的当前轮改查空的事件队列，没有用 list/get_capability_operation 读取已完成等待的持久结果。空队列不能证明事件未发生。这两类是模型职责/检索行为缺口，不是计时未到期，也不据此改为自动播报或自动推进；交 U10 补充能力契约并重新验证。失败记录不删除，不把首次偶然成功当完整通过。

U10 首版职责修复后，[普通/插话各三次矩阵](2026-10-19-teaching-capabilities-u8-role-first.json)为 **6/6**：全部真实调用 wait，跨轮从原 operation 读取完整随机 event_id，仅创建一个计时、不自动推进。该次隔离源码摘要为 audio/doubao_realtime.py `6bd55e745230ed62c79d363fbbea90a022b5c1be16833d3690ec2c104ed5dc0e`、agent_timers.py `4a3b192a377f3620c3a488f18f82a94f9d2cf51a137ada6559be44e9e7b7bd33`。后续补充合法 timeout 续等及意图边界后，仍需以最终源码复验；不把此处旧快照改写成最终发布证明。

变更：`device/lira/agent_timers.py`、`agent_task_state.py`（同步权限撤销订阅）、`conversation_paths.py`（注册及生命周期）、`tests/test_agent_timers.py`、`tests/test_conversation_paths.py`。尚未提交、部署；真实物理麦克风和扬声器门槛另行记录。
