# 语文认读与默写 v2 本地验收记录

基线：`docs/plans/2026-10-21-1206-feat-chinese-dictation-baseline-plan.md` r3。2026-10-21。本文记录实现证据，不修改需求、readiness 或发布权限。

**当前结论：本地实现、离线跨端流程及真实 KIMI + H5 完整闭环已验证。首次失败记录保留；用户随后要求使用 KIMI，新增一场单次测试成功。未部署、未安装家庭电视，实体声学与遥控验收未执行。**

## 首次真实模型运行（历史失败记录）

原始报告：[dictation-tc1-20261021-01/report.json](artifacts/dictation-tc1-20261021-01/report.json)。实际模型 `kimi-k3`，一次初始请求、一次实际模型调用、0 个新业务任务、0 次完成；终态 `failed`。模型真实询问教材，未调用工具。旧回归断言要求澄清前先调 `get_dictation_context`，因此在必要教材澄清阶段停止。这是回归前置条件过严；需求允许先澄清，但准备任务前必须读取真实教材与知识事实。

后续已离线修正提示与断言，保留“准备前必须读上下文”的检查。首次交付时没有重新运行该 case，原始失败报告不改写。用户随后明确要求测试使用 KIMI，产生下节独立的新运行；没有将首次失败改判为通过。首次报告中 browser_closed/worker_done 为 true，H5 timers/pendingInputs 为 0、audioActive 为 false，server_pending/pending_continue_inputs 为 0。这里只证明该次失败能够收尾。

只读检查（退出码 1 是该报告未通过的正确结果，不会发起模型调用）：

```powershell
python device/scripts/evaluate_teaching_agent.py --dictation-report docs/verification/artifacts/dictation-tc1-20261021-01/report.json
```

## 用户要求后的 KIMI 单次完整运行

新报告：[dictation-kimi-tc1-20261021-02/report.json](artifacts/dictation-kimi-tc1-20261021-02/report.json)。读取现有设备 LLM 配置，校验模型为 `kimi-k3`、服务为 `api.moonshot.cn`，密钥只在内存中使用。准备与执行中辅导共 7 次实际模型调用，全部来自同一个 KIMI 客户端。豆包实时语音未参与；播报使用本地 Melo TTS，H5 AudioContext 提供实际结束回调。

本场耗时 339.77 秒：1 次初始请求、1 个新任务、1 次完成、28 次孩子交互、ready 后再准备 0 次。KIMI 完成教材澄清、读取知识与 skill、选择词序及准备任务，并在执行中调用暂停、查询当前项后回答问题和追问。两次辅导均在实际暂停后发声、实际排空后才允许恢复。认读分页、重播/提醒、普通暂停、断线租约、页面重载、播放失败重试、照片/错词核对及知识回流均通过。完成后即退出，没有循环或再次准备任务。

模型角色与提供商无关：模型负责理解请求、读取知识、作教学判断和调用同一套业务工具；电视状态机负责执行冻结流程；ASR/TTS 负责文字与声音转换。本场孩子输入为受控模拟文字，不构成真实麦克风识别验收。

只读检查退出码 0，全部机械门禁通过；检查器本身不调用模型。报告的定性回答仍需结合实际教学反馈评估，不把软件流程通过等同于教学效果或实体声学通过。

```powershell
python device/scripts/evaluate_teaching_agent.py --dictation-report docs/verification/artifacts/dictation-kimi-tc1-20261021-02/report.json
```

模型调用、两次回答排空和终态清理证据均在新报告中。browser_closed/worker_done 为 true，timers、pendingInputs、server_pending、pending_continue_inputs 为 0，audioActive 为 false。新报告 SHA-256：`7a700c10b83338f25da75473b793d8c806e20b06837639807e8352ae186b98e1`。

## 本地证据

| 层次 | 本次证据 | 能证明与限制 |
| --- | --- | --- |
| Python 服务 | 合同、仓储、skill/配置、准备、音频、身份、暂停竞态、上下文、照片/核对与生命周期定向测试 | 正反例及正式业务服务；不等于云端模型完整行为 |
| 真实 KIMI + H5 | 新运行 `dictation-kimi-tc1-20261021-02` 完成；7 次 KIMI 调用、28 次交互、1 任务/1 完成 | 完整模型工具与 H5 软件流程；本地 TTS、模拟孩子输入，未使用豆包，不是物理电视证明 |
| 管理页面 | 最终 10 个实际 Edge 管理/导航用例通过；390/1440 两种宽度，见 `tmp/dictation-v2-work/ui-post-review` | 实际 HTTP 持久化、冲突保留、照片核对、知识查询、跨任务草稿隔离与保存期编辑保护；不声明已部署 |
| H5 离线最长流程 | [最终报告](artifacts/dictation-offline-final/report.json)；2 passed、1 显式真实集成 skip（53.08 秒） | 正式 Python 服务与真实 AudioContext 排空；模型响应、音调为合成，报告明确 OFFLINE |
| Android 宿主 | `tmp/dictation-v2-work/native-verification-final.json`：9 个 Java 测试入口全部通过 | v1/v2 合同、状态、分页、确认防抖、租约、晚回调及持久 outbox |
| Android 构建 | 最新 APK 编译和签名验证通过，`tv-receiver/build/lira-tv.apk` | 可构建；未安装，不证明物理声音或电视生命周期 |
| 副本迁移 | `device/tests/test_dictation_integration.py` 的只读源→pristine→迁移副本测试 | 真实 v1 快照/执行记录及普通聊天旧表保持；迁移两次幂等；真实 v2 DDL/seed 在故障下回滚；旧完成未变全对 |
| Python 3.11 相关兼容回归 | 501 passed（40.55 秒），见 `tmp/dictation-v2-work/remote-verification.txt`；隔离 `/tmp` 源码副本，覆盖全部 dictation/teaching 测试及 conversation_paths、dev_console、实时能力/语音/音频、personal_memory、tool_stream、主循环、模型客户端与实时实验室 | 未更改设备服务；不是项目全量测试。Windows Python 3.10 缺少板端语音依赖，不能以其全量收集失败冒充功能失败或全量通过 |

离线长流程覆盖认读多页、全部默写词、重播、仅提醒不跳项、问题及追问、实际暂停与回答排空后恢复、普通暂停、断线租约、页面重载、一次播放失败、家长照片与错词核对及只读事实回流。每类故障最多注入一次，同任务续接，完成即收尾。它不是第二场真实模型业务测试。

最终离线 case 共 28 次孩子交互，新增 1 个任务、完成 1 次、ready 后重新准备 0 次。页面示例：[大字认读](artifacts/dictation-offline-final/recognition.png)、[实际辅导后暂停](artifacts/dictation-offline-final/paused-after-actual-explanation.png)、[播放失败待重试](artifacts/dictation-offline-final/playback-failed.png)、[单次完成](artifacts/dictation-offline-final/completed.png)。这些示例均来自隔离 H5，已人工查看认读、暂停、完成三张截图。

本次新增及修改的核心 Python 模块通过 `compileall`，工作区 `git diff --check` 通过。项目没有配置独立 lint/typecheck 命令，未将编译检查称为类型检查。已有 FastAPI `on_event` 与 multipart 弃用警告保留，未影响上述测试结果。

## 代码审查与修复

实际 `ce-code-review` 已完成，run `20261021-152733-ba62daf9`，10 个审查视角及一次独立验证批次。原始 [review.json](artifacts/dictation-review-20261021/review.json) 保留修复前 `Not ready` 结论。独立跨模型启动在 Windows ACL 加固阶段两次失败，未启动外部模型、未发送代码；本地对抗审查完成。浮点哈希候选经真实 org.json 深拷贝归一验证后否决。

修复后的 [验证与源码哈希记录](artifacts/dictation-review-20261021/followup.json) 单独保存；最终被测 Python 源码与交付工作区对应文件的 SHA-256 一致。

| 编号 | 已应用修复 | 证据 |
| --- | --- | --- |
| #1 | 模型工具先成功读取匹配范围的上下文，才可准备；可信后端 API 保持原合同 | 新增 model_context_gate 正反例，包含跨孩子/范围及实际 PCM 生成门禁 |
| #2 | 取消或完成后清除解释门控，恢复普通语音；迟到回答不回写辅导事实 | terminal/dialogue 先失败再通过用例 |
| #4 | 确认切换后重建目标任务核对表；取消切换和同任务刷新保留草稿 | 实际 HTTP 双任务、成员及准备后自动加载浏览器用例 |
| #5 | 两条正式语音路径带上任务固定版本 skill；转白话与文字聊天不注入默写上下文 | 真实客户端请求捕获与实时 session 配置/更新测试 |
| #6 | 请求期间锁定表单输入，完成后恢复原 disabled 状态 | 延迟实际保存的浏览器用例；CAS 冲突草稿仍保留 |
| #7 | 管理摘要读取仓储平铺 item_id | 正式仓储事件后的浏览器展示断言 |
| #8 | 暂停任务可接受获批的电视辅导音频；实际排空前阻止推进，显示仍独占 | 新增 native engine/receiver 组合边界测试；APK 编译与签名通过 |

另修复两项审查记录的边界：准备写盘或提交失败只清理本次新建目录，既有任务与无关文件保留；worker 先取回事件并确认任务仍存活才续租，事件通道失败不能持续续租。两者均先观察失败用例，再获得通过证据。合入时保留了并行新增的静音检测修改，最终 501 项回归针对汇合后的源码执行。

这些修改涉及模型工具信任边界、语音并发和跨端路由；软件测试与编译通过仍不能证明实体电视声学。原始真实 TC1 报告 SHA-256：`58f799c5c5b8c523bf057847d215e88a1dd70906f653caefa4d174f89978dd5f`，终态和计数保持原样。

隔离工作树测试缓存清理被自动审批以 `blocked by policy` 拒绝，未提供更具体原因。`__pycache__` 保留在隔离目录，未纳入交付源码。

## 需求与场景追溯

“本地已覆盖”表示代码/定向测试及离线链路有证据；下表任何一行均不提升为真实家庭验收。

| 需求 | 实现/验证入口 | 状态与对应场景 |
| --- | --- | --- |
| R1 教材/进度 | textbook_practice、teaching_profile、管理页面 | 本地及新 KIMI 场景已覆盖；AE1/F1 |
| R2 skill 管理 | teaching_skills、chinese_dictation_skill.md、版本 CAS 测试 | 本地已覆盖；AE1/F1 |
| R3 流程配置 | dictation_settings、管理配置页、v2 合同 | 本地已覆盖；AE1/F1/F3 |
| R4 发起/澄清 | DictationAgent、管理/语音可信会话入口 | 本地及新 KIMI 完整链已覆盖；AE1/F1 |
| R5 默认内容/选词 | 服务覆盖校验、provenance、原模型词序测试 | 本地已覆盖；AE2/F1，不以代码频次算法替代模型 |
| R6 掌握/快照 | 定向 context、版本固定、PCM 后冻结哈希 | 本地已覆盖；AE1/F1 |
| R7 二会认读 | 原字、WordPageLayout、H5/Java 多页确认 | 本地已覆盖；AE3/F3；实体电视大字待验 |
| R8 默写 | 排空→等待孩子、超时仅提醒、单次重播 | 本地已覆盖；AE4/F3；家庭实际扬声器待验 |
| R9 提问暂停/恢复 | DictationCoordinator、实际事件回执、generation/ticket 门控 | 本地及 KIMI/H5 实际暂停、两轮回答排空已覆盖；AE5/F2；物理声学顺序待验 |
| R10 状态/安全日志 | 电视安全环形日志、管理完整事件、答案泄露断言 | 本地已覆盖；AE6/F3 |
| R11 离线/幂等 | transport/inbox/outbox、任务哈希、waiting_tv | 本地已覆盖；AE7/F3 |
| R12 重启/失败 | lease、检查点、重载暂停、failed_item 显式重试 | 本地已覆盖；AE7/F3；物理断网重启待验 |
| R13 照片/核对 | DictationResults、真实 PNG/JPEG 校验、多图替删、错词错字 | 本地已覆盖；AE8/F1 |
| R14 知识回流 | 最新核对、辅导、认读困难与 word_history 定向 context | 本地及新 KIMI/H5 完成后回流已覆盖；AE8/F1 |
| R15 事实边界 | unreviewed 与全对区分；无未经核对的精确准确率/纯书写速度 | 本地已覆盖；AE9 |
| R16 初版范围 | 无背诵执行/OCR/执行中换词 | 本地已覆盖；AE9 |

## 清记忆与恢复边界

修复了清记忆后 `owner_epoch > 0` 永久禁止新练习的问题。准备 session 持久绑定 owner/epoch；模型工具不能在已清记忆 epoch 自行创建新 session。家长鉴权后的新任务入口或实际语音 user_text 才能登记新 session。旧 session、旧模型工具及合成途中旧任务不能因新会话到来而复活。该修复先有失败测试，再通过旧会话拒绝、新会话允许、合成中清记忆及语音 epoch 轮换测试。

## 副本迁移与回退

显式离线入口只打开源库 `mode=ro`，在全新目录创建 pristine 和演练副本；不读取配置或连接家庭服务：

```powershell
python device/scripts/verify_teaching_migrations.py --source PATH_TO_COPY.sqlite --output-dir NEW_REHEARSAL_DIR
```

本轮只用合成数据，不导出真实家庭库。新增表和修订保留旧记录；失败回滚 v2 schema/seed，原始副本始终保留。旧 v1 活动快照不能作为 v2 自动续播；旧 receiver 缺 v2 能力应明确升级，不降级旧拼音/计时流程。关闭新入口不删既有学习和核对记录。真正发布前须保存受保护的数据库与附件一致备份、核对 receiver 能力并暂停活动任务；回退恢复匹配版本备份和附件，不能把新 v2 活动快照交给旧引擎。本轮未执行生产迁移或回退。

## 尚未通过的完成门槛

1. 首次真实 case 保持失败；后续按用户新要求执行的 KIMI 单次 case 已完成 TC1 软件闭环，不能据此宣布实体发布验收完成。
2. 家庭电视实体遥控器/语音、失焦、网络切换、重启恢复及扬声器播报顺序未测。
3. 本地源码、构建、H5 与旧真机报告必须分开解释，旧报告不能补充此次 v2 物理证据。

## 发布后验证预案（部署前记录）

实际发布由后续授权的维护者负责。首次单任务观察期应关联 task_id、content_hash、command_id、run_epoch 和连续 sequence，核对 waiting_start、实际暂停回执、设备回答排空、孩子确认恢复及唯一完成事件。电视安全日志不得出现待默写答案；家长管理页可查看授权后的完整事件。失联应在租约到期后暂停，恢复网络不自行发声。重复完成、答案泄露、无有效确认推进或暂停前解释均应阻止继续使用新入口，保留任务与事件证据；回退按上文匹配备份执行。当前没有生产监测数据或实体测试结果。


## 2026-10-21 实体部署与用户测试交接

按用户明确授权，18:33 起部署到 LIRA 与 MiTV4，未自动创建或启动任何默写任务。此段取代前文“未发布”的当前状态描述，原验收记录保留为历史证据。

- LIRA：29 个相关源文件部署并逐一 SHA-256 校验，用户服务重启后健康接口、默写页面和静态资源均正常。后台既有 KIMI `kimi-k3` / `api.moonshot.cn` 配置保留，运行路径设为 classic；本地 ASR/TTS 负责语音输入输出，未调用豆包语音。
- 补齐 classic 语音入口：实际声纹身份关联孩子，准备与执行复用现有 DictationAgent / DictationDialogueAgent / 工具；解释等待本地实际播报完成，暂停和恢复仍受电视回执门控。相关 Python 3.11 回归 **506 passed in 40.48s**。
- 实际家庭库在停止服务后备份，先在副本验证增量迁移、幂等与失败回滚，再启动新版。成员 4、声纹档案 3 保留；无活动默写任务。模型密钥、电视配对和教材未覆盖。
- 电视 APK 安装成功并进入前台，认证接口确认 `dictation_task_v2`，音频 idle、默写 idle。保留原 APK 于本机私有部署目录。安装时仅确认本次系统安装弹窗，未勾选“应用到全部”。
- 设备备份目录：`/home/orangepi/lira/deploy-backups/dictation-v2-20261021-183313`。私有日志持续追加至其 `test-session.log`，采集单元 `lira-dictation-debug-20261021-183313.service`。电视调试日志默认显示，后台任务详情保留完整实际事件。
- 交接入口：`http://192.168.101.26:8080/dev/dictation`。选择孩子与已配置教材进度，输入任务、回复模型问题，发送到电视，孩子确认后开始。完成后家长上传照片并核对错词，写回知识记录。

部署就绪凭据见 `artifacts/dictation-deploy-20261021/receipt.json`。本次就绪检查未调用模型创建任务；此前 KIMI/H5 的单次通过报告与本次实体部署相互独立。家庭实际全流程由用户接下来测试，尚未宣称实体完整流程通过。
