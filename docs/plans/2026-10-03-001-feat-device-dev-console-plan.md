---
title: "feat: 设备开发者控制台（远程状态监控 / 配置 / 独立测试）"
type: feat
status: active
date: 2026-10-03
origin: docs/brainstorms/2026-10-03-dev-console-requirements.md
---

# feat: 设备开发者控制台（远程状态监控 / 配置 / 独立测试）

## Summary

在现有设备 UI（8080）内以条件挂载的 dev 路由实现开发者控制台：口令一次验证发短时会话 cookie，提供整机状态总览（扩展现有 /status）、环形缓冲日志查看、运行参数调整，以及语音/视觉/同步三链路的服务端独立触发测试与整机场景回放——全部同进程直驱运行中的 DeviceRuntime 实例，严格服从隐私联锁。

---

## Problem Frame

M7 主循环接线完成（2026-10-03），设备进入板上验证期：所有冒烟验证都必须物理在设备旁（听喇叭、说话术、翻日志），单设备 bring-up 迭代成本高。设备已有两张 web 面（老人设置 UI、家属后台）都不承载"单链路独立触发"的调试语义。详见 origin Problem Frame。

---

## Requirements

- R1. 状态总览：状态机状态、音频路由、同步状态与 (epoch, version)、远程可用性、语音栈/OCR 就绪性，一屏可见且保持可编程读取
- R2. 日志查看：进程内环形缓冲最近事件日志（日志纪律保证无识别内容）
- R3. 配置在线修改：日志级别在线调整；音量/语速沿用既有设置端点
- R4. 语音测试：TTS 播报指定文本；麦克风服务端录音 ~10s 并回放；录音→ASR→意图分类结果返回
- R5. 觙觉测试：单帧拍摄 → OCR → 识别文本 + 分阶段耗时（拍摄/det/rec）
- R6. 同步测试：手动触发快照拉取，显示应用结果与 (epoch, version)
- R7. 整机场景回放：模拟唤醒 + 指令文本注入 → 真实状态机执行 → 展示迁移与播报记录
- R8. 隐私联锁：隐私 ON 时采集类端点（录音/ASR/视觉）一律拒绝并返回原因；TTS 播报类不受限
- R9. 鉴权：口令一次验证 → 短时会话 cookie（HttpOnly），验证失败限速；口令未设 fail-closed 引导设置
- R10. 区块可整体隐藏：config 开关（默认关），关闭时无入口、路由不可达
- R11. 操作留痕：dev 端点操作以 dev-console 来源标记写入事件日志（日志纪律：内容不落盘）

---

## Scope Boundaries

- 实时视频/音频流：明确不做（实时视频还需 ISP 打通）——Phase 2 候选
- 红外测试：M5 硬件到货后加入
- config 文件级参数在线修改：仍走 yaml + 重启
- 远程重启 / systemd 管理 / OTA、多设备、公网可达：不做
- 浏览器端录音与上传：不做（服务端采集语义 = 设备听到什么录什么）

### Deferred to Follow-Up Work

- 实时流（含 ISP 打通）、IR 测试面板：Phase 2 / M5 后迭代

---

## Context & Research

### Relevant Code and Patterns

- `device/lira/ui/app.py` — FastAPI + Jinja2 装配模式、UiServices 注入、口令 verify→POST 模式、/status 只读 JSON、 privacy 联锁先例（隐私开关口令路径）
- `device/lira/main.py` — DeviceRuntime（engine/tts/store/pipeline 均为其属性，可注入 dev 服务面）；`_run_sync` 监督循环（加 pull 事件）；`_NetworkProbe`
- `device/lira/audio/mic.py` SoundDeviceMic（服务端录音可另开短时 InputStream，PipeWire 多路捕获已验证——M2/M6）；`lira/audio/asr.py` AsrStream（离线喂 wav 喂流）；`lira/audio/tts.py` TtsEngine.speak（返回 done 事件）
- `device/lira/vision/reading.py` ReadingPipeline._grab（拍摄→解码先例）；`lira/vision/ocr_x86.py`/`ocr_rknn.py`（read 接口）
- `device/lira/dialog/state_machine.py` DialogEngine（on_wake/on_asr_text 文本注入通道 = e2e say_text 同构）；`device/tests/e2e/harness.py`（整机装配形态参照）
- `device/lira/settings.py` DeviceSettings（音量/语速）；`lira/privacy.py` PassphraseVault（scrypt verify）+ PrivacyState

### Institutional Learnings

- docs/solutions/ 不存在（无既往学习库）
- 本会话评审残留（docs/residual-review-findings/1222d13-...）：UI 口令无失败限速、CSRF 缺失为已知债——本计划的会话 cookie + 限速直接缓解口令爆破面

---

## Key Technical Decisions

- **同进程直驱运行时**：dev 端点直接调用运行中 DeviceRuntime 的 engine/tts/相机/OCR（uvicorn in-process 同一事件循环，无跨线程问题；sqlite check_same_thread=False 已允许）。场景回放不起第二套装配（双装配会争抢麦克风/喇叭）
- **进程内环形日志缓冲**：logging Handler 环形队列（如 500 条），不耦合 journald；日志纪律保证缓冲内无识别内容
- **服务端录音**：dev 测试另开短时 sounddevice InputStream（PipeWire 多路捕获，板上已验证），录 ~10s 存临时 wav 供回放与 ASR 测试复用；浏览器端 MediaRecorder/上传编码问题整体回避
- **口令会话**：vault.verify 一次 → itsdangerous 签名 HttpOnly 会话 cookie（短 TTL，如 4h）+ 验证失败限速（朴素计数器，镜像后台登录限速模式）；未设口令 fail-closed
- **dev 路由条件挂载**：config `dev_console.enabled`（默认 false；环境变量可覆盖），关闭时路由不注册、首页无入口
- **状态 = 扩展现有 /status JSON**（保持只读不鉴权语义，新增字段均为非敏感：状态名/版本号/布尔就绪位）

---

## Open Questions

### Resolved During Planning

- 场景回放驱动方式 → 直驱运行时实例的引擎文本通道（on_wake/on_asr_text），STANDBY 守卫（非待机拒绝并返回当前状态）
- 日志源 → 进程内环形缓冲
- 音频编码 → 服务端采集 wav，无上传编码问题
- 区块隐藏 → config 开关条件挂载

### Deferred to Implementation

- 临时 wav 的存放与清理（/tmp + 用后即删 or 覆盖复用）
- 限速计数器的窗口/阈值参数（镜像后台模式起步，实测调）
- dev 页面布局细节（单页分区即可，不为打磨投入）

---

## Implementation Units

<!-- Units sequential U1..U6. -->

### U1. dev 控制台骨架：config 开关 + 会话鉴权 + 路由挂载

**Goal:** dev_console.enabled 配置 + dev 路由条件挂载 + 口令→会话 cookie 鉴权（含限速、fail-closed），dev 首页骨架。

**Requirements:** R9, R10, R11（来源标记 = lira.dev logger 命名）

**Dependencies:** None

**Files:**
- Create: `device/lira/ui/dev.py`（dev 路由 + 会话鉴权助手）
- Modify: `device/config.yaml`、`device/lira/config.py`（dev_console 段 + env 覆盖 LIRA_DEV_CONSOLE）
- Modify: `device/lira/ui/app.py`（UiServices 增加 dev 句柄字段；create_app 条件挂载 dev 路由）
- Modify: `device/lira/main.py`（_run_ui 构建时传入 runtime 句柄；enabled 判定）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- DevServices 数据类持有 runtime 句柄与依赖（runtime 本身即可，弱耦合：dev.py 依赖 runtime 的公开面而非全量）
- 会话：verify 成功 → itsdangerous 签名 cookie（HttpOnly + SameSite=Strict，TTL 4h）；失败朴素限速（5 次锁 5 分钟，镜像后台模式）；未设口令 → 页面重定向口令设置（fail-closed）
- 响应形态区分（评审补）：dev JSON 数据端点在 cookie 缺失/过期/伪造时一律返回 401 JSON（绝不 302 到登录页——fetch 型按钮会把 HTML 当结果静默"成功"）；仅整页 GET 重定向口令流程
- dev 路由前缀 /dev；disabled 时 create_app 不注册，首页无入口

**Patterns to follow:**
- ui/app.py 的 verify→POST 与重定向风格；backend/app/auth.py 的 cookie/限速形态

**Test scenarios:**
- Covers AE3 (origin AE3, R9/R12 origin). Happy path: 设口令 → POST 正确口令 → 得会话 cookie → /dev 可达
- Error path: 未设口令访问 /dev → 引导设置；错误口令 5 次 → 锁定提示
- Happy path: dev_console.enabled=false → /dev 404、首页无入口
- Error path: cookie 过期/伪造 → 整页 GET 重定向口令流程；数据端点 POST → 401 JSON（非 302）
- Edge case: 数据端点缺 confirm 标记（回放类）→ 拒绝执行

**Verification:**
- 开关关闭时设备 UI 与现状等价；开启后未鉴权不可达 dev 功能

---

### U2. 状态总览 + 环形日志 + 日志级别

**Goal:** runtime 状态面扩展（/status 新字段）、dev 日志视图、日志级别在线调整。

**Requirements:** R1, R2, R3

**Dependencies:** U1

**Files:**
- Modify: `device/lira/main.py`（DeviceRuntime.status() 访问器：engine.state/route/sync_status/network/models；_run_sync 维护 sync_status；logging 环形 handler）
- Modify: `device/lira/ui/app.py`（/status JSON 新字段）
- Modify: `device/lira/ui/dev.py`（/dev 页状态区 + 日志视图 + 级别调整端点）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- 环形 handler：logging.Handler 子类 + deque(maxlen=500)，挂根 logger；记录格式化后的单行摘要
- sync_status 由 _run_sync 监督循环维护（连接状态/最近中断原因/退避），store 读 (epoch,version)
- 状态区含隐私 ON/OFF 展示（来源 privacy.is_on；采集类测试被锁时开发者一眼可见原因——评审补）
- 日志级别端点：root logger setLevel + 状态回显

**Patterns to follow:**
- ui/app.py 的 _index_context 组装风格

**Test scenarios:**
- Covers AE? origin R1. Happy path: runtime.status() 返回各字段（engine 状态名、epoch/version 来自 store、network 布尔）
- Happy path: 注入日志 → 环形缓冲可见且按序；>500 条后最旧被挤出
- Happy path: 调整级别为 DEBUG → 后续 debug 记录进入缓冲
- Edge case: 无模型/无同步配置时字段降级为明确"未配置"而非缺失

**Verification:**
- /status 与 /dev 页展示全部 R1 维度；日志视图只含事件类内容（纪律复核）

---

### U3. 语音链路测试（TTS / 服务端录音 / ASR→意图）

**Goal:** 三个语音测试端点 + privacy 联锁。

**Requirements:** R4, R8, R11

**Dependencies:** U1

**Files:**
- Modify: `device/lira/ui/dev.py`（speak / record / asr 端点）
- Modify: `device/lira/main.py`（runtime 暴露 tts、settings、privacy、新增服务端录音助手 dev_record()）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- speak：runtime.tts.speak(text, volume=settings.volume, speed=settings.tts_speed)，立即返回"已触发"（并发语义沿用现状，重叠播报属已知残留 R3）
- record：新开短时 sounddevice InputStream（16k/mono/int16）录 ~10s → /tmp 下临时 wav（覆盖复用 + 用后清理）；回放经 HTML audio 指向受会话保护的下端点
- asr：读取上一步 wav → 离线喂 AsrStream（与 e2e 音频注入同构）→ endpoint 检测或读满 → 文本 + 意图分类结果（router 本地规则分类展示，不执行）
- 联锁：record/asr 端点先查 privacy.is_on → 拒绝；speak 不受限
- wav 路径含会话无关固定名（覆盖复用），操作留痕经 lira.dev logger

**Patterns to follow:**
- e2e conftest 的 WavQueueSource 喂流方式（离线喂 AsrStream）；mic.py 的 SoundDeviceMic 参数

**Test scenarios:**
- Covers AE1 (origin AE1). 隐私 ON：record/asr → 拒绝响应含原因，无采集发生（录音助手未被调用）
- Covers AE4 (origin AE4). Happy path: 夹具 wav（"打开台灯"）→ asr 端点返回识别文本 + 家电-打开意图；日志无该文本
- Happy path: speak 端点 → tts.speak 被调且参数含 settings 音量/语速（FakeTts 断言）
- Edge case: 未先录音直接调 asr → 明确提示"先录音"
- Error path: 录音设备打开失败（注入失败源）→ 友好错误不炸进程

**Verification:**
- 三个端点在板上经控制台可独立触发；AE1/AE4 语义由测试钉住

---

### U4. 视觉链路测试（单帧→OCR→分阶段耗时）

**Goal:** 视觉测试端点 + privacy 联锁。

**Requirements:** R5, R8, R11

**Dependencies:** U1

**Files:**
- Modify: `device/lira/ui/dev.py`（vision 端点）
- Modify: `device/lira/main.py`（runtime 暴露 camera/ocr 访问器）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- 流程：privacy 检查 → camera.capture() 计时 → 解码计时 → ocr.read() 计时 → 返回文本行 + 三段耗时
- 不创建朗读会话、不动 speaker（与 run_capture 隔离，仅共享相机/OCR 实例——单线程事件循环内串行无冲突）

**Patterns to follow:**
- reading.py _grab 的解码方式；e2e FakeOcrEngine 的 read 接口

**Test scenarios:**
- Covers AE2 (origin AE2). 隐私 ON → 拒绝；隐私 OFF → 返回文本+耗时（fake 相机/OCR）
- Happy path: 返回的分阶段耗时键齐全且非负
- Error path: 相机 HalError（无 /dev/video0，x86 环境）→ 友好错误（板上设备错误族）

**Verification:**
- AE2 语义测试钉住；x86 fake 全流程绿

---

### U5. 同步测试（手动拉取 + 同步状态展示）

**Goal:** 手动触发快照拉取端点 + 同步状态区。

**Requirements:** R6, R1

**Dependencies:** U1, U2

**Files:**
- Modify: `device/lira/main.py`（_run_sync 增加 pull 事件：退避等待改为 wait(stop|pull)，收到 pull 立即 heartbeat_once；维护 sync_status）
- Modify: `device/lira/ui/dev.py`（pull 端点 + 状态区）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- runtime 增加 pull_event（asyncio.Event）；supervisor 每轮退避等待时同时等 stop|pull；pull 触发立即 heartbeat_once（连着会话时）或提前进入下一轮重连
- 状态区显示：会话状态（运行中/中断/未配置）、(epoch, version)、最近中断原因与退避秒数

**Patterns to follow:**
- _run_sync 现有 stop/pull 双事件等待结构

**Test scenarios:**
- Covers origin R9(同步). Happy path: 注入 pull 事件 → 伪造 transport 收到 PullMsg（会话保持时）
- Happy path: 未配置 sync.ws_url → 端点返回"未配置"而非报错
- Edge case: 会话中断退避期收到 pull → 提前重连尝试（不再等满退避）

**Verification:**
- AE 语义：后台改配置 → 控制台点拉取 → (epoch,version) 前进可见

---

### U6. 整机场景回放（唤醒→指令注入）

**Goal:** 场景回放端点：直驱运行时引擎文本通道，展示迁移与播报。

**Requirements:** R7, R11

**Dependencies:** U1, U2

**Files:**
- Modify: `device/lira/ui/dev.py`（replay 端点 + 结果区）
- Modify: `device/lira/main.py`（DeviceCallbacks.speak 增加最近播报环形记录，供回放/状态区展示）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- 端点：STANDBY 守卫（非待机拒绝并返回当前状态）→ spawn 引擎 on_wake → 等待进入 LISTENING → 注入 on_asr_text(指令) → 采集 engine.transitions 增量与 speak 触发（speak 观测：临时订阅? 引擎回调 speak 走 DeviceCallbacks——控制台观察面用轮询 transitions + tts 最近播报记录。为可观测，给 DeviceCallbacks.speak 加最近播报环形记录（仅事件与文本？播报文本落内存不落盘——日志纪律管日志，内存展示面 OK；R5 TTS 测试响应同理）
- 隐私联锁天然成立：privacy ON 时 on_wake 被引擎 wake_allowed 拒绝 → 端点如实返回"唤醒被拒绝（隐私）"
- 回放需显式确认（评审补：风险表承诺的"页面二次确认"落到实现）：请求缺 confirm 标记时拒绝执行、不触碰引擎
- 音频照常出声（真实链路验证语义）

**Patterns to follow:**
- e2e harness 的 say_text 通道；DeviceRuntime 现有 spawn

**Test scenarios:**
- Covers AE5 (origin AE5). Happy path: 回放"打开台灯" → transitions 含 listening→executing→standby、ir_sent 可见（mock IR）
- Edge case: 引擎非 STANDBY → 拒绝并返回当前状态
- Edge case: 缺 confirm 标记 → 不触碰引擎直接拒绝（二次确认门槛）
- Covers AE1 语义. 隐私 ON → on_wake 被引擎拒绝，端点如实展示"唤醒被拒（隐私）"，无指令执行

**Verification:**
- 控制台可见完整迁移链与播报；隐私期回放被拒

---

## System-Wide Impact

- **Interaction graph:** dev 端点与主循环同事件循环共享 runtime——所有 dev 操作串行于引擎/管线操作之间（无新锁需求）；服务端录音与主循环采集并存（PipeWire 多路），麦克风测试期间主循环 KWS/ASR 仍在吃同设备流（可能录到 TTS 提示音——如实告知）
- **Error propagation:** dev 端点异常一律折叠为 JSON 错误响应 + lira.dev 日志，不外抛（HAL 错误族转友好文案）
- **State lifecycle risks:** 临时 wav 覆盖复用 + 会话 cookie TTL；replay 在引擎占用时被 STANDBY 守卫拒绝（不与真实用户语音竞争）
- **API surface parity:** /status 新字段为纯增量（kiosk 语义不变）；老人 UI 页面不变（dev 入口仅 enabled 时出现）
- **Integration coverage:** AE1/AE2/AE5 语义进测试套件；真实后台联调随 M7 实机验证顺带覆盖（控制台 pull → 后台回快照）
- **Unchanged invariants:** 日志纪律（内容永不落盘）、隐私 fail-closed、单进程编排器、单实例运行时（不起第二套装配）

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| 服务端录音与主循环同时采集在板上异常（PipeWire 行为差异） | M7 实机验证时先单测该场景；失败退路 = 录音测试改为"暂停主循环采集 10s"（muted 窗口）——U3 预留实现开关 |
| dev 端点成为误触发面（真人在旁时 TTS 突然出声/回放执行 IR） | 操作留痕 + 页面二次确认（IR/回放类）；v1 IR 未入选即无发射风险 |
| 会话 cookie 鉴权弱于账号体系 | TTL 4h + 失败限速 + 局域网边界 + 区块默认关；与项目 LAN 信任模型一致 |
| runtime 状态面扩展泄露敏感信息 | /status 仅新增非敏感字段（状态名/布尔/版本）；识别内容只在 dev 响应往返中 |
| x86 测试环境无声卡/相机 | 测试用注入替身（FakeTts/录音失败源/FakeOcr），板上实机项进 M7 冒烟 |

---

## Documentation / Operational Notes

- SETUP.md 2.8 增补：dev_console 配置段、控制台冒烟用法（作为 M7 实机验证的辅助工具）
- origin 的 Outstanding（4 项 Deferred to Planning）全部在本计划 Resolved 中落定

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-10-03-dev-console-requirements.md](../brainstorms/2026-10-03-dev-console-requirements.md)
- Related code: `device/lira/ui/app.py`、`device/lira/main.py`、`device/lira/sync.py`、`device/tests/e2e/harness.py`
- Related review record: `docs/residual-review-findings/1222d13-m7-main-loop-wiring.md`（口令限速/CSRF 债）
