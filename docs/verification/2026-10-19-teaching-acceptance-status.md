# 教学基础能力验收状态

原计划 r5 的 HTML/Markdown 保持不变；本文件记录执行状态，不变更范围或把阻断项改成通过。当前总状态：**blocked_acceptance**。U1 协议真实云端门槛已通过后才继续 U2–U10；全部单元已有本地实现，七条代码审查问题及后续发现的关闭竞态均已修复并只读复核关闭，模型、硬件验收仍未通过。

| 原需求 | 已实现依据 | 验收限制 |
|---|---|---|
| R1–R6 管理入口与旧课本兼容 | 教学管理 App、科目禁用占位、旧课本地址与返回入口；路由/浏览器回归 | 最终分层浏览器回归另记 |
| R7–R8、R14 教材/OCR/原文版本 | TeachingContentRepository 草稿版本、手动录入/选择页导入/家长确认；模型只读确认版本，RealtimeVision 临时识别 | 原文缺失/草稿的真云矩阵未调用实际全文 read，不能视为模型消费已验证；真实摄像头未验证 |
| R9、R12、R15、R19、R20 自主判断/协商 | AgentTaskState 独立语义核验、当前约定及 revision、AgentTimers 事件，不固定节奏或反馈 | U2 核验器 24/24 与 U10 模型协同不是同一门禁。真实模型仍假称约定激活，V5 阻断 |
| R10、R18 精确播报/真实结果 | RealtimeSpeech 输出仲裁、非零 PCM 同任务排空、真实中断/取消、后台 get 查询 | 静音消费不证明物理扬声器；pending 初始话术仍偶发“马上”，不能宣称可靠完成说明 |
| R11 电视展示 | 真实探测/版本/clear 能力与内容归属确认；APK 0.2 已构建 | 实际 sender ConnectTimeout；未安装/配对/实测清屏。明确讲解意图的合成样本正确，但其约定并未真实激活 |
| R13、R16–R17 家长核对/不确定与记忆 | TeachingMemory/Evidence 区分偏好、待核实笔记、不可覆盖原始证据、家长核对与附注；遗忘撤销上下文，UI 明确待核实标签 | 不自动评分或推断掌握；物理识别表现未验证 |
| R18、R20 生命周期硬边界 | Registry/Executor 真实返回、跨轮 get、幂等/冲突、身份隔离、暂停/隐私/忘记/资料修改撤销 | 审查发现七项缺陷，修复状态见[跟踪](2026-10-19-teaching-review-followup.md)，不能由单元测试推导硬件已通过 |

验证分层：

- V1–V3：关闭竞态及迟到回调修复后，最终主执行方整合 **294 passed / 20.59s**。计划的 `test_doubao_realtime.py` 不存在，使用实际 `test_doubao_lab.py` 加真实会话 registry 测试。最终独立非浏览器全量 **1579 passed / 3 failed / 183.93s**；三项仍为 HEAD 已有“小鹿”词表与“小丽拉”音频测试不一致，依据见[经典回归](2026-10-19-teaching-classic-regression.md)，本轮不修改它们。原日志 `tmp/teaching-final-reviewed-nonbrowser.txt`，无新增失败。此前两个 1571/1575 通过的数字仅属中间快照。
- V4：最终教学管理与旧课本浏览器 **5 passed / 25.13s**；AppCenter **5 passed / 19.43s**；Android 客户端 unittest **6 passed**。APK 配对不以构建通过代替。
- V5：36 条真云矩阵及[人工复核](2026-10-19-teaching-capabilities-u10-matrix-review.md)完成，**未通过支持门槛**。OCR 三条引用误报已排除，其他失败保留，不继续盲目提示词重试或手动绕过。
- V6：最终 audio `f5354685…` 的 U1 **12/12**，每次执行一次、无提前编造、正常退出，见[最终门禁](2026-10-19-teaching-capabilities-u1-final-reviewed-gate.json)。同一最终源码播报完成/打断/取消 **3/3**，见[回执](2026-10-19-teaching-capabilities-u5-final-reviewed.json)。关闭修复前最近完整计时矩阵 **1/6**，未通过；最终源码完整参数/结果单次插话诊断 **1/1** 且正常清理，不能替代稳定性矩阵。探针夹具修正、消费归因证据与局限见[最终云端复核](2026-10-19-teaching-final-cloud-review.md)。物理麦克风持续输入、扬声器排空/打断、摄像头 OCR、TV 接收器实测尚缺，是发布门槛。
- V7：实际板端库只读备份后在副本重复迁移两次，旧 schema/行数保持、quick_check 正常，见[回执](2026-10-19-teaching-data-compatibility.json)。运行服务未升级或重启。

已完成实际 ce-simplify-code 与完整 ce-code-review（10 视角、7 条独立验证）；[审查回执](2026-10-19-teaching-code-review.json)的原 verdict Not ready 保留，后续修复不篡改。开关默认关闭，不提交、不推送、不部署，也不触碰原有 staging。运行验证预案见[独立文档](2026-10-19-teaching-operational-validation.md)。

要解除当前阻断，需要能在真实实时模型上重放并通过约定激活、状态真实性、资料选择/读取和排队播报状态表述，而非只使代码工具可调用；并补齐真实设备及 APK 配对证据。缺少证据不能由确定性教学流程替代。
