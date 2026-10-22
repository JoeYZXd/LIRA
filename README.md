# LIRA — 家庭智能助手探索项目

LIRA 是一个围绕真实家庭需求持续探索的项目：结合开发板、大模型、语音、视觉和家中已有设备，尝试让助手承担阅读、家电操作、孩子学习辅导等具体事务。

项目最初面向低视力老人，提供辅助阅读与家电控制；现在逐步扩展为开放的家庭助手实验平台。当前已完成的代表性场景是**语文认读与纸笔默写辅导**：模型准备任务，电视按固定流程展示、播报，家长核对后将结果留作下次参考。

这些能力高度定制于本家庭的开发板、麦克风、摄像头、红外设备、电视和教材。仓库保留多条实现与实验路线，用实际效果决定下一步方向；当前不是通用家庭产品，也不是所有入口都已完成真实家庭验收。

> 文档基线：2026-10-22。下列“当前采用”指最近已记录的部署或用户选择，本次 README 更新未重新探测设备。测试数量均属于对应日期与范围，不累加为项目全量通过数。

## 项目入口与当前环境

- 设备面板：[`http://192.168.3.253:8080/dev`](http://192.168.3.253:8080/dev)，SSH：`ssh lira`。最新地址与部署方式见 [运维手册](deploy/SETUP.md)。
- 已验证主开发板：Orange Pi 5 系列 / RK3588S；开发机通过 mock HAL 做隔离开发。
- 家庭外设：本地收音与扬声器、OV13855 摄像头、BroadLink 红外盒子、MiTV4（Android 6 / API 23）及遥控器。替换硬件需重新验证采集、播放和控制链路。
- 当前默写方案：**KIMI 调度 + 豆包 Vivi 2.0 合成 + 电视原生播放 + 遥控手动推进**。
- 原始老人辅助需求保留在 [最初需求](docs/brainstorms/2026-09-26-lira-requirements.md) 与 [初版计划](docs/plans/2026-09-26-001-feat-lira-phase1-device-backend-plan.md)；它们描述项目起点，不覆盖后续确认的教学行为。

## 功能、入口与代码地图

下表 Python 模块均位于 `device/lira/`。管理页面主要运行在开发板上的 `ui/`，与早期独立的 `backend/` 家庭管理服务不同。

| 功能 | 使用入口与当前边界 | 主要目录 / 模块 | 说明与证据 |
| --- | --- | --- | --- |
| 语音对话与追问 | 唤醒后交谈；可选本地组件链路或豆包实时语音 | `conversation_paths.py`、`audio/`、`dialog/`、`llm/` | [A/B 路线](docs/conversation-paths-2026-10-16.md)、[流式语音](docs/voice-streaming-validation.md) |
| 辅助读书与 OCR | 拍摄书页、识别原文、朗读、暂停/继续/重读；复杂文字可调用模型转白话 | `vision/reading.py`、`vision/engine.py`、`vision/ocr_rknn.py`、`vision/ocr_x86.py`、`hal/board/camera_isp.py` | [连续阅读](docs/voice-continuity-validation.md)、[俯拍验证](docs/overhead-camera-validation.md) |
| 家电遥控与红外学习 | 本地路由、管理后台或豆包工具操作；仅支持实际学习/配置的红外码 | `appliances/`、`infrared_voice.py`、`hal/`；`backend/app/` | [豆包红外工具](docs/doubao-infrared-tools-2026-10-17.md)；发射成功不等于家电实际状态已确认 |
| 按成员文字聊天与记录 | 面板选人聊天、查看历史与关联个人记录；隐私模式拒绝远程请求 | `chat/`、`ui/chat.py`、`personal_memory.py`、`voice_history.py` | [身份与聊天历史](docs/identity-continuity-and-voice-history-2026-10-18.md) |
| 声纹录入与自然聊天身份 | 面板引导录入/测试，或豆包会话内调用本地工具；真人稳定识别仍待验收 | `voice_enrollment.py`、`realtime_enrollment.py`、`realtime_identity.py`、`identity_confirmation.py`、`identity_journal.py` | [阶段总结、门槛与缺口](docs/voiceprint-stage-summary-2026-10-18.md)；没有自动训练或调阈值 |
| 教材与教学资料管理 | 教学管理 → 语文：课本导入、页面核对、课文/诗词版本与孩子进度 | `textbook_*.py`、`teaching_content.py`、`teaching_profile.py`、`ui/textbooks.py`、`ui/teaching.py` | [教学 PRD](docs/product/teaching-prd.md)；草稿不作为标准答案 |
| 语文认读与默写 | 教学管理 → 语文 → 默写；二会组词认读、四会扩词默写、提问暂停、家长错词核对 | `dictation_*.py`、`llm/chinese_dictation_skill.md`、`ui/dictation.py`；`tv-receiver/src/` | [PRD](docs/product/teaching-prd.md)、[操作手册](docs/product/teaching-user-guide.md)、[开发复盘](docs/product/teaching-retrospective-20261022.md) |
| 电视展示与声音输出 | 局域网配对接收文字、图片、PCM 和默写任务；不以接收 accepted 代替实际执行回执 | `tv_display.py`、`tv_voice.py`、`tv_audio.py`、`dictation_transport.py`；`tv-receiver/` | [电视 APP 构建与协议](tv-receiver/README.md)、[语音投屏](docs/tv-voice-display-2026-10-18.md) |
| 开发控制台与观测 | 运行参数、语音链路、Jev、视觉、声纹、工具结果、耗时与用量 | `ui/dev.py`、`ui/doubao_lab.py`、`ui/jev_probe.py`、`voice_trace.py`、`usage.py`、`ui/static/` | [计时口径](docs/interaction-timing-recording.md)、[成本用量](docs/costs-and-usage-2026-10-16.md) |
| 开放式教学 Agent 实验 | 独立能力注册、约定、计时、证据和精确播报；默认扩展关闭，模型稳定性未过门槛 | `realtime_capabilities.py`、`agent_task_state.py`、`agent_timers.py`、`teaching_memory.py`、`teaching_evidence.py`、`realtime_speech.py` | [能力边界与评测](docs/agent-capabilities.md)；与已交付的固定默写流程分开看待 |

教学管理保留数学、英语与背诵等入口，但数学题/视频/打印、专门英语辅导和背诵执行仍待建设或验收。规划中的乐高小车、儿童陪伴机器人不属于当前已交付能力。

## 路线与切换方式

### 1. 对话链路：A 与 B

| 路线 | 执行链与用途 | 切换方式 | 验证状态 |
| --- | --- | --- | --- |
| A · `classic` | 本地唤醒/ASR → 路由或配置的大模型 → TTS；适合组合本地能力与指定模型，最近 KIMI 默写演示采用此路线 | 设备面板“运行参数 → 语音交互路径”选择 A，点击“切换并结束当前对话” | 本地流式管线与 HAL 验证已有记录；默写另有真实 KIMI/H5 与电视证据 |
| B · `doubao_realtime` | 本地唤醒后上传音频，豆包实时语音完成转写、对话与音频输出；本地工具负责家电、声纹等实际操作 | 同一面板选择 B；服务须已有 `LIRA_DOUBAO_REALTIME_API_KEY` 和 `LIRA_DOUBAO_REALTIME_VOICE` | 已有真实云端及电视音频实验；各业务分别验收，不能由连接成功推断所有工具可靠 |
| Jev 固定语料实验 | 文本分段决策 → 选择预热的固定 PCM；用于低延迟提示探索 | A 路线下打开独立“Jev 测试”；配置 `TYPESAFE_API_KEY`，可选 `TYPESAFE_MODEL` | 独立测试入口，不是第三条生产对话主链路，也不会保存真实声纹资料 |

A/B 选择保存在设备数据库 `conversation_path`，重启保留；首次缺省为 A。切换会结束旧对话和播报、清理未完成状态，已保存声纹保留。B 连接失败明确报错，不暗中回退 A。B 待机只做本地唤醒，建立会话后才持续上传；隐私开启关闭云端会话。详细操作见 [A/B 说明](docs/conversation-paths-2026-10-16.md)。

A 路线及文字大模型配置来自 `device/config.yaml` 和环境变量：`LIRA_LLM_BASE_URL`、`LIRA_LLM_MODEL`、`LIRA_LLM_API_KEY`。仓库示例为 GLM，最近默写部署记录为 KIMI `kimi-k3`，示例默认值不代表设备正在使用的模型。修改进程环境后重启服务；不要靠改 `conversation_path` 切换 KIMI/GLM，它控制的是整条语音链路。

### 2. 合成路线：普通回答与默写分开配置

| 路线 | 配置 / 切换 | 当前用途与限制 |
| --- | --- | --- |
| 本地 TTS | `LIRA_TTS_BACKEND` 设为 `matcha`、`kokoro` 或 `melo`；`LIRA_TTS_THREADS` 设置线程数；修改后重启 | 代码默认 Matcha；已有板端 Melo 测量。本地提示、阅读与 A 链路可使用，不意味着每个模型在每块板上都已配置 |
| A 路线在线回答 TTS | `online_tts.enabled` 或 `LIRA_ONLINE_TTS_ENABLED`；配置 `LIRA_ONLINE_TTS_API_KEY`、资源与音色 | 只切换相应实时回答合成；与 B 全双工、默写音频是独立装配。具体降级/隐私行为见 `device/lira/audio/` 与 [流式验证](docs/voice-streaming-validation.md) |
| 豆包实时语音音频 | B 路线内由实时服务返回音频 | 不经过本地 ASR → 文字模型 → 本地 TTS，不把 B 的回复速度算作本地 TTS 性能 |
| 默写豆包 TTS 2.0 | 教学管理 → 语文 → 默写 → 可配置流程，选择豆包并保存新版本 | 当前采用 Vivi 2.0，音调 `0`、语速 `-20`、音量 `30`；指令“朗读 播音 拉长音 着重强调 标准普通话” |
| Qwen3-TTS 0.6B | 独立实验环境/试听脚本；没有生产切换开关 | 板端 CPU 可运行，但当前实测太慢，未接入生产默写；不是选择本地 TTS 后就自动可用 |

默写的 KIMI 负责准备与调度，豆包只负责合成；完整 PCM 校验并缓存后由电视原生播放器播放。流程、TTS 与 skill 版本在准备会话中绑定，**修改后新建准备会话才生效，旧任务不变**。电视“调试显示”是独立实时设置，默认关闭，不需要重建任务。参数及手动操作见 [默写手册](docs/product/teaching-user-guide.md)。

### 3. 输出路线：开发板与电视

| 场景 | 选择方法 | 边界 |
| --- | --- | --- |
| B 实时回答从开发板播放 | `LIRA_REALTIME_AUDIO_OUTPUT=device`（默认），重启服务 | 走设备现有声音输出 |
| B 实时回答从电视播放 | `LIRA_REALTIME_AUDIO_OUTPUT=tv`，配置既有电视配对，重启服务 | 仅作用 B 实时语音，不改变 A；须电视能力、前台与播放回执满足条件 |
| 默写词语从电视播放 | 默写任务协议与原生资源播放器自动处理 | 不依赖上述 B 输出开关；电视静音、路由或未就绪仍会导致失败 |
| H5 模拟电视 | `tv-receiver/preview/` 静态预览；正式回归使用 `device/tests/h5_tv/` | 前者用于界面审阅，后者验证软件交互；均不能替代物理遥控和人耳听感 |

### 4. 视觉、硬件与开放式 Agent

- **HAL**：`LIRA_HAL_BACKEND=mock` 用于开发；真实开发板使用 `board` 并按 [部署手册](deploy/SETUP.md) 配好摄像头、声音和红外设备。不是只切一个开关就能兼容任意开发板。
- **OCR**：`vision/ocr_x86.py` 是开发机 ONNX 路线，`vision/ocr_rknn.py` 是 RK3588 板端 RKNN 路线；随运行装配和依赖选择。摄像头方向、节点、ISP 与光照相关，见 [俯拍验证](docs/overhead-camera-validation.md) 和 [画质实验](docs/camera-quality-experiment.md)，没有统一跨硬件画质切换按钮。
- **开放式教学 Agent**：`LIRA_REALTIME_CAPABILITIES=true` 开启 B 路线独立扩展，进程缺省关闭。该实验有模型可靠性门槛，不能因为已有固定默写功能就默认启用；详见 [能力评测](docs/agent-capabilities.md)。

环境变量应配置在设备服务的私有环境文件或 drop-in 中，修改后 `systemctl --user restart lira`；仅修改 systemd unit/drop-in 本身时先 `systemctl --user daemon-reload`。密钥、配对令牌与家庭数据不写 README、不提交仓库。

## 已记录的测试结果与指标

这些是可追溯的历史样本，不是本次重新跑出的基准；软件帧排空、模型回答和物理听感分别验证。当前没有统一的全项目性能分数，也没有充分样本支持各路线声学 p50/p95 排名。

| 日期 / 路线 | 测试结果或指标 | 口径与限制 | 证据 |
| --- | --- | --- | --- |
| 10-16 · 本地 Melo / 预缓存 PCM | “我在。”首次输出中位数 `0.638 / 0.058 s`；20 字提示 `2.786 / 0.055 s`；43 字说明 `2.166 / 0.056 s`；18 次播报均成功 | 每句每路线 3 次，文本提交 → HAL 首次消费；不含 ASR/模型生成、初始化，也不是物理首声。云端对比当时未完成 | [基线及原始结果](docs/tts-latency-benchmark-2026-10-16.md) |
| 10-16 · Jev / A/B 基础 | Jev 控制台/UI/新功能定向 121 项通过；A/B 最终影响面 234 项通过，显式排除 1 项既有 KWS 用例 | 不作为当前全量结果。早期全量存在 3 项 `Unknown token: shei2` 的真实 KWS 失败；Jev 本地测试不代表在线中文准确率 | [Jev](docs/jev-cached-speech-test.md)、[A/B](docs/conversation-paths-2026-10-16.md) |
| 10-17 · B 红外工具 | 146 项相关测试通过；静音云端两轮正确选择查询/准备工具 | 自动测试模拟红外输入；云端探针使用模拟业务返回，没有实际家电发射，不声明硬件成功率 | [红外记录](docs/doubao-infrared-tools-2026-10-17.md) |
| 10-18 · 本地声纹 | 初次认人阈值 `0.80`、第一/第二分差 `0.10`；原音重提取示例 `0.8045`，向量平均 `0.7862` | 这些是算法门槛和单次对照，不是识别准确率；自然聊天稳定性与反馈闭环未全部验收，阶段暂停调参 | [声纹总结](docs/voiceprint-stage-summary-2026-10-18.md) |
| 10-19 · B 开放式教学 Agent | 局部真云矩阵：基础操作 `12/12`、约定核验 `24/24`、偏好 `27/27`；整体仍为 `blocked_model_acceptance` | 局部通过不能消除忽略 needs_input、未激活却声称成功等整体门槛；默认扩展关闭 | [评测边界](docs/agent-capabilities.md) |
| 10-20 · B → 真实电视音频 | 6 段返回 completed，PCM `603016` 帧，约 `25.13 s`；播放帧与写入帧相等 | 路由回执为内置扬声器；合成用户输入、无外部听感录音。旧第7课实验不作为当前默写内容规则 | [电视链路](docs/verification/2026-10-20-tv-audio-and-lesson7-two.md) |
| 10-21 · KIMI + H5 单次长流程 | `339.77 s`，7 次真实 KIMI 调用、28 次受控交互、1 个任务/1 次完成 | 含准备、提问暂停/恢复、异常与核对回流；输入是模拟文字、当时用本地 TTS，不证明真实麦克风或电视声学 | [完整验收](docs/verification/2026-10-21-dictation-v2-acceptance.md) |
| 10-21 · Qwen3-TTS 板端 CPU | “山顶”合成 `35.59 s`，“形状” `31.02 s`，原音均 `1.28 s`；RTF `27.80 / 24.23`，峰值约 `5.20 GiB` | 0.6B、CPU float32、4 线程；RTF 为合成耗时/音频时长。没有 NPU/量化实测，未生产采用 | [板端试听](docs/verification/2026-10-21-qwen3-tts-board-preview.md) |
| 10-21 · KIMI + Vivi + 实体电视 | 单次第8课完成，7 个词资源，播放开始/完成 `10/10`；首词等待 6 秒无自动推进 | 助手代操作演示、未核对；当时 UI 的认读键位已被最新左右键规则替代，报告保留历史 | [Vivi 实机验收](docs/verification/2026-10-21-doubao-vivi-manual.md) |
| 10-22 · 最新儿童 UI / 源码归档 | 提交前 67 项定向 Python、11 组 Java 测试通过；更早该 UI 的管理浏览器 10 项、H5 3 项通过 | 最新 APK 已部署，大字布局有实机截图。补充任务进度已变化后脚本停止，未宣称最新 UI 全场自动回归通过 | [UI 验证](docs/verification/2026-10-22-child-tv-ui.md)、[归档检查](docs/verification/2026-10-22-source-snapshot.md) |

持续观察时可启用 `LIRA_VOICE_METRICS=1`，各阶段事件定义见 [流式指标说明](docs/voice-streaming-validation.md)。A 的 ASR/模型/TTS 阶段、B 的云端转写完成 → HAL 输出、默写准备总时长不是同一计时起点，不直接比较。

## 目录结构

```text
LIRA/
├─ device/
│  ├─ config.yaml                  # 仓库示例配置，环境变量可覆盖
│  ├─ lira/
│  │  ├─ main.py / config.py       # 运行装配与配置
│  │  ├─ conversation_paths.py     # A/B 切换、工具和业务装配
│  │  ├─ audio/ dialog/ llm/       # 收音、唤醒/ASR、对话、合成与模型
│  │  ├─ hal/ vision/ appliances/  # 外设、OCR/阅读、红外业务
│  │  ├─ chat/                    # 按成员文字聊天
│  │  ├─ realtime_*.py            # 实时语音扩展、身份、精确播报等
│  │  ├─ textbook_*.py            # 教材导入、核对与练习资料
│  │  ├─ teaching_*.py            # 学习档案、skill、内容和证据
│  │  ├─ dictation*.py            # 默写准备/合同/执行/结果/传输
│  │  ├─ tv_*.py                  # 电视展示、语音工具与实时音频
│  │  └─ ui/                      # 本机面板、教学管理与实验入口
│  ├─ scripts/                    # TTS 基准、模型评测、H5 回归等
│  ├─ tests/                      # 服务、模拟端到端、浏览器与 H5
│  └─ deploy/audio/               # 音频路由与消回声部署
├─ tv-receiver/
│  ├─ src/ tests/                 # Android 原生接收/播放/状态机与测试
│  ├─ preview/                    # 独立 H5 界面预览
│  └─ build.py / client.py        # APK 构建与本地接收端客户端
├─ backend/                       # 早期家庭管理、设备/家电与同步服务
├─ deploy/                        # 板端 systemd、设备配置与运维说明
├─ docs/
│  ├─ product/                    # 当前教学 PRD、手册与复盘
│  ├─ plans/ brainstorms/         # 各探索方向的规划，不等于已实现
│  ├─ verification/ reviews/      # 测试、部署与审查的历史证据
│  └─ *.md                        # 语音、身份、视觉等专项记录
├─ assets/                        # 测试样张、关键词等必要资源
├─ models/                        # 模型下载脚本；模型权重不入库
└─ tmp/ artifacts/                # 本地临时实验/采集产物，忽略提交
```

成员、模型调用、教材、skill 版本、音频、电视连接与执行回执是后续功能的复用基础。当前部分可靠性机制仍位于默写模块中，不是已完成抽象的通用工作流引擎，详见 [PRD 中的基础服务边界](docs/product/teaching-prd.md)。

## 开发与验证

Python 项目要求 **3.11+**。先在开发机选择 mock HAL；真实声音、摄像头与红外需要对应依赖及硬件配置。

```bash
cd device
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
LIRA_HAL_BACKEND=mock .venv/bin/python -m lira.main --dry-run
.venv/bin/python -m pytest
```

按需安装 `audio`、`ocr-x86`、`llm`、`ui`、`textbooks`、`sync` extras；模型下载入口为 `python3 models/download_models.py`。模型权重、设备私有配置、家庭数据库、配对令牌和 APK 签名密钥不随仓库分发，新环境不能直接复用本家庭的配对状态。

```bash
# 浏览器测试须独立进程，与默认 pytest-asyncio 层分开
cd device
.venv/bin/pip install -e ".[browser]"
.venv/bin/python -m pytest tests/browser

# 早期家庭管理服务
cd ../backend
python3 -m pip install -e ".[dev]"
python3 -m pytest

# 回到仓库根目录，构建电视 APK（JDK 17 + Android SDK）
cd ..
python3 tv-receiver/build.py
```

- 原生 Java 测试及安装说明见 [电视模块](tv-receiver/README.md)；H5 长流程入口为 `device/scripts/run_dictation_h5_regression.py`，实际模型/硬件回归须按脚本说明准备配置，不当成普通离线单测。
- `device/tests/e2e/` 使用 mock 外设与受控输入，部分真实语音用例依赖模型/音频夹具；缺依赖、跳过及历史失败须在报告中注明。
- 运行测试不会替代现场验收。新路线先做小样，再做一次有边界的完整交互；记录模型、硬件、配置、样本数、失败数和计时起终点。
- 只需使用默写功能时，从 [操作手册](docs/product/teaching-user-guide.md) 开始；要扩展其他家庭功能时，从 [基础能力边界](docs/product/teaching-prd.md) 与 [开发复盘](docs/product/teaching-retrospective-20261022.md) 开始。
