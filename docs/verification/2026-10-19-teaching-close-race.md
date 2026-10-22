# 最终云端诊断发现的关闭等待问题

在 U1 最终源码 12/12、U5 真实任务状态 3/3、U8 最终矩阵 1/6 之后，运行一次额外的 U8 插话消费诊断，仅增加真实参数/返回值及独立核验器响应观察，不改角色或处理器。隔离进程约 210 秒仍未结束，处于 epoll 等待；没有生成最终回执，因此不能将它计为通过、补齐消费归因或新的 1/6 分母。

核对 PID 44568 的命令确为 `/tmp/probe_realtime_timers.py --case barge --repeat 1 --output /tmp/lira-u8-consumption-diagnostic.json` 后，只向该隔离探针发 SIGINT，设备运行服务未操作。退出堆栈：

```text
probe_realtime_timers.py:92 run
    timers.close(); await session.close()
lira/audio/doubao_realtime.py:724 close
    await asyncio.gather(*tasks, return_exceptions=True)
lira/audio/doubao_realtime.py:382 _send_audio
    pcm = await self.input.get()
asyncio.exceptions.CancelledError
```

`close()` 已先设置 closed 并取消任务，但发送任务仍等待队列，说明只靠一次取消和无条件循环不足以保证实际退出。确切竞态由控制测试重现后修复，不能仅把探针超时归为模型问题，也不能用外层强制超时伪装关闭成功。主执行方授权修复已在进行：实时关闭、发送/接收循环及对应回归；经典链路、协议和教学策略不变。

此前已记录“unknown”的那条 U8 原始记录仍无实际 get 参数/返回体，消费失败原因保持未决。新增诊断本来要补这项证据，但因关闭挂起未返回，不追溯编造其内容。

## 修复与复核

发送/接收循环增加 closed 状态退出及 await 后保护，close 的 tool/session task gather 排除当前任务；不用超时伪装成功。三个红测重现挂起/迟到接收/工具自等待，补第四项会话任务自关闭。见[第一批修复](2026-10-19-teaching-close-race-local-repair.md)。

独立只读复核还发现已进入事件分支的 await 后保护不足；另四个红测重现迟到文本/音频结束回调，以及停止播放吞取消后重新建立播放器。补 interrupt、_start_output 与 receive 分支检查后，见[后续修复](2026-10-19-realtime-close-late-callbacks.md)。同一复核者最终确认全部关闭、无新问题，源码 audio SHA256 `f5354685f06dd93a3a895b433462fc353f942ba9282b2d6a9bc2345061fb8bc4`。

主执行方最终 V1–V3 整合 **294 passed / 20.59s**。产品源码再次冻结；探针另加关闭前/后 checkpoint，未完成清理的结果不能视为正常回执，SDK 观察包装在 finally 恢复。最终源码真实云端复验及全量回归另记，不用上一快照的 12/12 替代。

最终源码带真实参数/结果记录的[插话诊断](2026-10-19-teaching-capabilities-u8-close-fixed-diagnostic.json)已正常退出且通过，[检查点](2026-10-19-teaching-capabilities-u8-close-fixed-checkpoint.json)清理状态为 false（无待清理）。它没有触发外部 180 秒终止边界；该边界仅防止隔离探针无限挂起，不参与通过判据。最终 U1 十二项复验另行运行。

最终同一源码非浏览器全量 **1579 passed / 3 failed / 183.93s**；仅既有唤醒词音频基线失败，无新增失败，原输出 `tmp/teaching-final-reviewed-nonbrowser.txt`。

最终同一源码 U1 四类各三次 **12/12**，见[门禁](2026-10-19-teaching-capabilities-u1-final-reviewed-gate.json)；精确播报三项 **3/3**，见[回执](2026-10-19-teaching-capabilities-u5-final-reviewed.json)。两个探针均正常退出，未触发外部终止边界。新增关闭问题的代码和真实退出验证已完成，不再作为未修复项；U10 模型稳定性与硬件门槛仍独立阻断。
