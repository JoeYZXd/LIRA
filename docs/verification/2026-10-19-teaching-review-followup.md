# 代码审查修复跟踪

实际 `ce-code-review mode:agent` 已完成，回执 `status: complete`，run_id `20261019-teaching-48d377da`；完整十个视角、七条独立验证，审查时 verdict 为 Not ready。回执副本：[teaching-code-review.json](2026-10-19-teaching-code-review.json)。跨模型路由因无法限定既有用户 WIP 范围未启动，使用本地 adversarial，不宣称外部模型独立审查。

按文件和生命周期耦合分批，因共享脏工作区串行写入；不 staging、不提交、不部署。七条问题均有实际依据且在本轮范围内，无拒绝项。原始回执保持不变，修复进度另记：

| 审查编号 | 批次 | 当前结果 |
|---|---|---|
| #1 P1 终态永久耗尽容量 | 容量与调用绑定 | 已修复，主执行方 54 tests passed / 10.45s；[细节](2026-10-19-teaching-review-capacity.md) |
| #2 P1 换人保留旧云端正文 | 会话生命周期 | 已修复，关闭旧云会话、新建上下文、隔离旧回调并封闭无归属的旧历史读取；[细节](2026-10-19-teaching-owner-idle-review-fixes.md) |
| #4 P2 空闲回收撤销有效计时 | 同一会话生命周期批次 | 已修复，有效 pending 计时/等待保活，终态不无限保活；与 #2 主执行方回归 121 passed / 11.47s |
| #3 P2 迟到取消抹掉已到期事件 | 计时状态 | 已修复，终态更新/取消报冲突并保留事件；主执行方 36 passed / 1.56s；[细节](2026-10-19-teaching-review-expired-timer.md) |
| #5 P2 传感器命令无限等待 | 实时摄像头隔离 | 已修复，transient-only 超时/取消 kill+reap，classic 原分支保留；[细节](2026-10-19-teaching-transient-camera-review-fixes.md) |
| #6 P2 图片 CPU 阻塞实时接收 | 同一实时摄像头批次 | 已修复，临时捕获独立线程，实际结束才释放锁；与 #5 主执行方回归 84 passed / 10.30s |
| #7 P2 待核实笔记显示成普通记录 | 家长界面 | 已修复，真实 learning_note 显示待核实；worker 浏览器 3 passed / 14.69s，主执行方复验另记；[细节](2026-10-19-teaching-parent-ui-review-followup.md) |

计划 V3 所列 `tests/test_doubao_realtime.py` 在当前工作区不存在；使用真实现有 `test_doubao_lab.py` 以及 `test_realtime_capabilities.py` 的 RealtimeSession 回归覆盖，不创建同名空壳来满足命令。首次原样命令因此未收集测试，随后以上实际路径运行 54 项通过。

原审查协调者对 #1–6 当前源码及新增回归进行了独立只读复核，逐条判定 closed，未发现新的实质问题；未重跑已有套件、未改原审查回执。复核确认容量分离/旧 ID 过期、旧云端与回调隔离、终态计时保留、有效等待保活、临时捕获子进程清理及 CPU 线程隔离均有对应执行路径。#7 当时仍在写入，待单独复核。该结论不证明云提供方重连可靠性或物理设备门槛通过。

#7 停写后同一只读复核者已确认 closed；原 testing gap 亦关闭，新增 DOM 流程覆盖期望的 ID/版本/请求体及可见结果，无新增实质问题。七条审查问题全部已修复，无跳过/拒绝/延后项。最终整合首次 285 passed / 1 failed：诊断 sink 的 `__new__` 夹具缺新增 ContextVar/开关初始化，尚未执行原隐私断言；只补夹具字段，保留原断言后复验。

补夹具后 V1–V3 主执行方整合 **286 passed / 20.78s**。随后默认全量命令发现浏览器目录原收集规则只排除 `test_dev_console_ui.py`，漏掉新页面测试，混入 Playwright 后触发 `Runner.run() cannot be called from a running event loop` 批量夹具错误。该无效混层运行已中断，原日志 `tmp/teaching-final-nonbrowser.txt` 保留，不计作产品回归或通过证明。将该目录规则改为非显式浏览器请求时 `collect_ignore_glob=['test_*.py']`，显式请求仍收集全部；默认 collect-only 实测 1574 项、浏览器 0 项，符合原有分进程纪律。只改测试收集，不改产品或经典链路。非浏览器与浏览器最终分层重跑另记。

这两处测试基础设施修正经同一独立只读复核者确认：原隐私断言未弱化，glob 只影响浏览器目录，不误排除兄弟测试。最终分层主执行方验证：非浏览器 **1571 passed / 3 failed / 186.50s**（仅既有三个唤醒词音频失败），教学管理/旧课本 **5 passed / 25.13s**，AppCenter **5 passed / 19.43s**，Android unittest **6 passed**。`node --check teaching.js` 与相关 tracked 文件 `git diff --check` 通过。无新增代码审查问题或本地功能失败；模型和硬件门槛仍阻断。

随后额外云端诊断发现独立的关闭竞态，原七条审查关闭不变，新问题单列于[关闭竞态记录](2026-10-19-teaching-close-race.md)。两批共八条回归先红后绿，独立只读复核确认挂起、自等待、迟到回调和播放重启均关闭，无新问题。最终主执行方整合 **294 passed / 20.59s**；上一段全量数字属于修复前快照，最终全量及最终源 U1 正在重新验证。没有跳过已确认的问题，也不强制超时假报关闭成功。

最终验证已结束：最终源码非浏览器 **1579 passed / 3 failed / 183.93s**，仅原 HEAD 已有三个音频失败；最终 U1 **12/12**、精确播报 **3/3**、带完整参数/结果的计时插话诊断 **1/1**，全部正常清理退出。七条原审查问题及新增关闭问题均完成修复和复核，无未应用的代码审查发现。**Residual Work Gate 返回 blocked_acceptance**：36 条模型矩阵仍有约定激活假报、资料读取路径未覆盖及 pending 初始话术问题，完整计时矩阵 1/6 不能被单次成功覆盖；真实摄像头、物理音频及 TV APK 配对/清屏证据缺失。缺这些证据时不满足原计划支持声明门槛，维持默认关闭，建议下一阶段集中解决模型状态遵循并完成设备验收。详见[验收状态](2026-10-19-teaching-acceptance-status.md)，不是把这些问题降为已接受的发布风险。

模型与硬件验收残余不是上述代码修复完成的同义词。V5 三次矩阵已完成且阻断支持声明，见[人工复核](2026-10-19-teaching-capabilities-u10-matrix-review.md)；V6 摄像头、物理音频、TV 配对尚待实测。完成本地修复后仍须明确返回这些门槛，不将默认关闭说成已发布。
