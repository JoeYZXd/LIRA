---
title: "feat: 设备开发者控制台（远程状态监控 / 配置 / 独立测试）"
type: feat
status: active
date: 2026-10-03
origin: docs/brainstorms/2026-10-03-dev-console-requirements.md
deepened: 2026-10-03
---

# feat: 设备开发者控制台（远程状态监控 / 配置 / 独立测试）

## Summary

在现有设备 UI（8080）内以条件挂载的 dev 路由实现开发者控制台：口令一次验证发短时会话 cookie，提供整机状态总览（扩展现有 /status）、环形缓冲日志查看、运行参数调整，以及语音/视觉/同步三链路的服务端独立触发测试与整机场景回放——全部同进程直驱运行中的 DeviceRuntime 实例，采集数据全程仅存内存，严格服从隐私联锁。

---

## Problem Frame

M7 主循环接线完成（2026-10-03），设备进入板上验证期：所有冒烟验证都必须物理在设备旁（听喇叭、说话术、翻日志），单设备 bring-up 迭代成本高。设备已有两张 web 面（老人设置 UI、家属后台）都不承载"单链路独立触发"的调试语义。详见 origin Problem Frame。

---

## Requirements

- R1. 状态总览：状态机状态、音频路由、同步状态与 (epoch, version)、远程可用性（联网 + 熔断态）、语音栈/OCR 就绪性，一屏可见且保持可编程读取
- R2. 日志查看：进程内环形缓冲最近事件日志（日志纪律保证无识别内容）
- R3. 配置在线修改：音量/语速（复用既有设置端点，控制台内可达）+ 日志级别在线调整（作用于 lira 命名空间）
- R4. 语音测试：TTS 播报指定文本（等待完成并返回结果）；麦克风服务端录音 ~10s 并回放（单飞保护）；录音/上传 wav → ASR → 意图分类结果返回
- R5. 视觉测试：单帧拍摄 → OCR → 识别文本 + 分阶段耗时（拍摄/det/rec 分列）
- R6. 同步测试：手动触发快照拉取（会话存活期立即生效），显示应用结果与 (epoch, version)
- R7. 整机场景回放：模拟唤醒 + 指令文本注入 → 真实状态机执行 → 展示迁移与播报记录（带会话竞争守卫）
- R8. 隐私联锁：隐私 ON 时采集类端点（录音/ASR/视觉）一律拒绝并返回原因；TTS 播报类不受限
- R9. 鉴权：口令一次验证 → 短时会话 cookie（HttpOnly + SameSite=Strict），签名密钥随机生成持久化、改口令即轮换；验证失败全局限速；口令未设 fail-closed
- R10. 区块可整体隐藏：config 开关（默认关，启动时判定），关闭时无入口、路由不可达
- R11. 操作留痕：全部 dev 端点操作（含级别调整、pull 触发）以 dev-console 来源标记写入事件日志（内容不落盘）

### Origin ↔ Plan 映射（仅覆盖本 origin）

origin R1+R2→R1；R3→R2；R4→R3；R5+R6+R7→R4；R8→R5；R9→R6；R10→R7；R11→Scope 暂缓（M5 后）；R12→R9；R13→R8；R14→R10；R15→R11。
已确认取代：origin R7"或上传音频"与 AE4 的上传 Given → 由"夹具 wav / 上一步服务端录音"取代（浏览器端上传不做是用户确认的 v1 边界；上传文件 multipart 输入保留，见 U3），AE4 断言面（识别文本 + 意图 + 日志无该文本）不变。

**Origin actors:** A1 开发者、A2 老人用户、A3 家属（隐私状态来源）
**Origin flows:** F1 视觉测试、F2 隐私联锁拒绝、F3 整机场景回放
**Origin acceptance examples:** AE1–AE6（映射见各单元测试场景）

---

## Scope Boundaries

- 实时视频/音频流：明确不做（实时视频还需 ISP 打通）——Phase 2 候选
- 红外测试：M5 硬件到货后加入
- config 文件级参数在线修改：仍走 yaml + 重启
- 远程重启 / systemd 管理 / OTA、多设备、公网可达：不做
- 浏览器端录音：不做；**上传音频文件保留**（ASR 测试的第二输入，multipart wav）

### Deferred to Follow-Up Work

- 实时流（含 ISP 打通）、IR 测试面板：Phase 2 / M5 后迭代

---

## Context & Research

### Relevant Code and Patterns

- `device/lira/ui/app.py` — FastAPI + Jinja2 装配、UiServices 注入、口令 verify 模式、/status 只读 JSON、隐私联锁先例
- `device/lira/main.py` — DeviceRuntime（engine/tts/store/pipeline 属性面）；`_run_sync` 监督循环；`_NetworkProbe`（阻塞调用不上循环线程的成文纪律）
- `device/lira/audio/mic.py` — SoundDeviceMic callback+queue 模式（服务端录音与离线喂流的样板）；`lira/audio/asr.py` AsrStream；`lira/audio/tts.py` speak 返回 done 事件
- `device/lira/vision/reading.py` — `asyncio.to_thread(engine.read)` 先例；`lira/vision/engine.py` detect/recognize 为公开抽象方法（det/rec 可分列计时）
- `device/lira/dialog/state_machine.py` — on_wake/on_asr_text 文本注入通道；`device/tests/e2e/harness.py` wait_state 模式；`backend/app/auth.py` — 会话 cookie（HttpOnly+SameSite=Strict）+ `_session_secret` 随机生成入库 + 登录限速先例
- `device/lira/sync.py` — SyncClient.run_forever（heartbeat 语义）、store 的 current_epoch/current_version 访问器

### Institutional Learnings

- 本会话评审残留（docs/residual-review-findings/1222d13-...）：UI 口令无失败限速、CSRF 缺失为已知债——本计划的会话 cookie + 限速直接缓解口令爆破面
- 阻塞调用不上事件循环为本仓库成文纪律（M7 评审 P1 共识，`_make_network_probe` docstring）——dev 端点全部遵循

---

## Key Technical Decisions

- **同进程直驱运行时**：dev 端点直接调用运行中 DeviceRuntime（uvicorn in-process 同一事件循环）。**纪律**：一切阻塞/CPU 密集操作（录音等待、离线 ASR 喂流、OCR 推理）一律 `asyncio.to_thread` 或 callback+queue 异步模式，绝不阻塞共享循环（先例：reading.py:256、hal/board/audio.py:44、_NetworkProbe）
- **dev 互斥锁**：变更类测试端点（录音/ASR/视觉/回放）经单一 asyncio.Lock 串行（进行中 → 409 "测试进行中"）——双击/双标签页即真实故障面；TTS 重叠播报仍属已知残留（M7 评审记录），页面提供"测试时请避开真实使用"操作提示
- **采集数据全程内存化**：录音存内存缓冲（10s@16k/mono/int16 ≈ 320KB），回放由受会话保护的端点从内存返回，ASR 测试直接读同一缓冲——**零磁盘写入**（录音不落盘是本控制台新增的隐私不变式，与日志纪律同级）
- **结果渲染机制**：测试动作为 fetch JSON + 少量原生 JS 渲染（无构建链、无框架）；状态/日志区手动刷新——与老人 UI 的表单+重定向风格**有意分叉**（重定向无法承载 wav 播放/耗时表/迁移链）
- **口令会话**：vault.verify 一次 → itsdangerous 签名 HttpOnly+SameSite=Strict cookie（TTL 4h）；**签名密钥 = 首次启用时 `secrets.token_urlsafe(48)` 随机生成、持久化于 store meta（镜像 backend `_session_secret`），改口令时轮换**（旧会话全失效）；验证失败全局内存计数限速（5 次锁 5 分钟，重启清零——单口令现实下全局计数即可，LAN 锁定循环骚扰为接受风险）；未设口令 fail-closed。**文本参数一律 POST body，禁 query string**（防入访问日志/浏览器历史）
- **dev 路由条件挂载**：config `dev_console.enabled`（默认 false；env 覆盖需 `_apply_env` 增加 bool 分支——"1/true/yes"→True，**不可用通用 type 转换**（bool("false")==True 是 fail-open））；启动时判定，关闭时路由不注册
- **状态 = 扩展现有 /status JSON**：仅新增枚举态/布尔/数值字段（engine 状态名、route、sync 会话枚举、epoch/version、network 布尔、熔断态、模型就绪位）；自由文本（中断原因）与退避秒数仅入鉴权后的 dev 状态区
- **其dangerous 依赖**：加入 `pyproject.toml` 的 `ui` extra（现仅 dev extra 持有，板上 [ui] 安装会 ImportError——评审 P1）

---

## Open Questions

### Resolved During Planning

- 场景回放驱动 → 直驱运行时引擎文本通道；STANDBY 守卫 + 显式隐私预检 + LISTENING 等待 2s 超时（镜像 harness.wait_state）+ 会话竞争守卫（见 U6）
- 日志源 → 进程内环形缓冲（挂根 logger 收集全量，级别调整作用于 lira logger）
- 音频编码 → 服务端采集内存 wav + multipart 上传 wav（16k/mono 校验）
- 区块隐藏 → config 开关条件挂载（含 bool env 分支）
- det/rec 分列计时 → read() 是一体模板方法，改用公开的 detect()/recognize() 组合（复用 engine.py 的排序/裁剪函数）

### Deferred to Implementation

- dev 页面布局细节（单页分区即可）
- 限速计数阈值实测调优（起步镜像后台 5 次/5 分钟）
- 回放期间是否抑制真实路由分发（评审建议的设计补强；v1 以"避开真实使用"操作提示 + 竞争守卫缓解，M5 真实 IR 到货前升级评估）

---

## Implementation Units

### U1. dev 控制台骨架：config 开关 + 会话鉴权 + 路由挂载

**Goal:** dev_console.enabled 配置（含 bool env 分支）+ dev 路由条件挂载 + 口令→会话 cookie 鉴权（密钥治理、限速、fail-closed）+ dev 互斥锁骨架 + dev 首页骨架。

**Requirements:** R9, R10, R11

**Dependencies:** None

**Files:**
- Create: `device/lira/ui/dev.py`（dev 路由 + 会话鉴权 + 互斥锁）
- Modify: `device/config.yaml`、`device/lira/config.py`（dev_console 段 + bool env 分支）
- Modify: `device/lira/ui/app.py`（UiServices 增加 dev 句柄；create_app 条件挂载）
- Modify: `device/lira/main.py`（_run_ui 传入 runtime 句柄与 enabled 判定）
- Modify: `device/pyproject.toml`（itsdangerous 加入 ui extra）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- 会话：verify 成功 → 签名 cookie（HttpOnly+SameSite=Strict，TTL 4h）；密钥首启 `secrets.token_urlsafe(48)` 入 store meta，改口令轮换；失败全局内存限速（5 次锁 5 分钟）；未设口令 fail-closed
- **响应形态**：整页 GET 未鉴权 → 重定向口令流程；dev JSON 数据端点未鉴权（缺失/过期/伪造）→ 一律 401 JSON（绝不 302——fetch 按钮会把 HTML 当结果）
- disabled 时路由不注册、首页无入口（启动时判定）

**Patterns to follow:**
- backend/app/auth.py 的 cookie/`_session_secret`/限速形态；ui/app.py 的 verify 风格

**Test scenarios:**
- Covers AE3 (origin AE3). Error path: 未设口令访问 /dev → 引导设置，测试功能全部不可达；错误口令 5 次 → 锁定提示
- Happy path: 设口令 → POST 正确口令 → 得会话 cookie（断言 Set-Cookie 含 HttpOnly+SameSite=Strict）→ /dev 可达
- Covers AE6 (origin AE6). Happy path: enabled=false → /dev 404、首页无入口
- Error path: cookie 过期/伪造 → 整页 GET 重定向；数据端点 POST → 401 JSON
- **路由级守卫回归**：parametrize 遍历全部已注册 /dev/* 路由 → 未鉴权请求一律 401/303（新增路由静默绕过鉴权即被套件抓住）
- Error path: env 设 LIRA_DEV_CONSOLE=false 覆盖 yaml true → 关闭（bool 分支语义）

**Verification:**
- 开关关闭时设备 UI 与现状等价；未鉴权不可达任何 dev 功能（路由级测试钉住）

---

### U2. 状态总览 + 环形日志 + 运行参数

**Goal:** runtime 状态面扩展（/status 新字段 + dev 状态区）、环形日志视图、日志级别调整、音量/语速控件。

**Requirements:** R1, R2, R3, R11

**Dependencies:** U1

**Files:**
- Modify: `device/lira/main.py`（DeviceRuntime.status()：engine.state/route/sync_status（U5 起数据）/network 布尔/熔断态（llm.breaker 可观测面）/模型就绪位（复用 _audio_models_ready/_ocr_models_ready）；环形 logging handler 挂根 logger）
- Modify: `device/lira/ui/app.py`（/status JSON 新增枚举/布尔/数值字段）
- Modify: `device/lira/ui/dev.py`（状态区（含隐私 ON/OFF、sync 自由文本详情）+ 日志视图 + 级别端点 + 音量/语速控件）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- 环形 handler：logging.Handler + deque(maxlen=500) 挂根 logger；级别端点作用于 `lira` logger（非 root——三方库 DEBUG 噪声不冲刷缓冲）
- 状态区：隐私 ON/OFF 一眼可见（采集被锁时的诊断第一站）；/status 仅枚举/布尔/数值字段，自由文本仅入 dev 状态区
- 音量/语速控件复用既有设置端点（不新增端点）；级别/pull 等 dev 端点操作经 lira.dev logger 留痕

**Patterns to follow:**
- ui/app.py 的 _index_context 组装风格；main.py 的模型就绪探测函数

**Test scenarios:**
- Covers origin R1（origin AE 集未覆盖状态总览，无对应 AE）。Happy path: runtime.status() 返回全字段（engine 状态名、epoch/version、network 布尔、熔断态、模型就绪位）
- Happy path: 注入日志 → 缓冲按序可见；>500 条最旧被挤出
- Happy path: 级别调整为 DEBUG → lira 命名空间后续 debug 记录进入缓冲
- Happy path: dev 页提交音量 → 既有设置端点被调且值生效
- Edge case: 无模型/无同步配置 → 字段降级为明确"未配置"而非缺失
- Happy path: 级别调整操作在 lira.dev 留痕

**Verification:**
- /status 与 /dev 页展示全部 R1 维度（含隐私与熔断态）；日志视图只含事件类内容

---

### U3. 语音链路测试（TTS / 服务端录音 / ASR→意图）

**Goal:** 三个语音测试端点 + privacy 联锁 + 互斥 + 内存化采集。

**Requirements:** R4, R8, R11

**Dependencies:** U1

**Files:**
- Modify: `device/lira/ui/dev.py`（speak / record / asr 端点）
- Modify: `device/lira/main.py`（runtime 暴露 tts/settings/privacy；服务端录音助手 dev_record()）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- speak：调 runtime.tts.speak 后 **await done 事件（超时 ~10s）**，返回 {spoken|failed, elapsed, 播报记录}——HTTP 响应是远程开发者唯一结果通道；文本仅经 POST body
- record：**互斥锁**（进行中 → 409）→ 按 mic.py callback+queue 模式开短时 InputStream **异步**等 ~10s → 内存 wav 缓冲（BytesIO，零磁盘写入）；页面进行态提示（~10s 倒计时）
- asr：输入 = 上一步内存缓冲 **或 multipart 上传 wav**（16k/mono 校验，大小上限）→ `asyncio.to_thread` 离线喂新建 AsrStream（不动运行中的 runtime.asr_stream）→ 文本 + 本地规则意图分类展示（不执行）
- 联锁：record/asr 先查 privacy.is_on → 拒绝；speak 不受限；record/asr 与引擎并发由互斥 + 单循环串行化
- origin R7/AE4 的上传输入由 multipart 保留（Scope 取代说明见映射段）；AE4 用例按 e2e 模式 requires_asr skipif 门控

**Patterns to follow:**
- mic.py:59-73 callback+queue；e2e WavQueueSource 离线喂流；e2e conftest requires_asr skipif

**Test scenarios:**
- Covers AE1 (origin AE1). 隐私 ON：record/asr → 拒绝含原因，采集助手未被调用
- Covers AE4 (origin AE4, requires_asr 门控). Happy path: 夹具 wav 注入 → 识别文本 + 家电-打开意图；环形缓冲无该文本
- Happy path: speak → 等待完成返回 spoken + elapsed（FakeTts 立即 set）；speak 文本不出现在环形缓冲
- Edge case: 未先录音且无上传 → 明确提示；录音进行中再点 → 409
- Error path: 录音设备打开失败（注入失败源）→ 友好错误不炸进程
- **回放端点鉴权**：未带会话 cookie GET 录音回放 → 401/303，响应体无音频字节

**Verification:**
- 三端点板上可独立触发；AE1/AE4 语义与"内容零落盘"由测试钉住

---

### U4. 视觉链路测试（单帧→OCR→分阶段耗时）

**Goal:** 视觉测试端点 + privacy 联锁 + det/rec 分列计时。

**Requirements:** R5, R8, R11

**Dependencies:** U1

**Files:**
- Modify: `device/lira/ui/dev.py`（vision 端点）
- Modify: `device/lira/main.py`（runtime 暴露 camera/ocr 访问器）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- 流程：privacy 检查 → 互斥锁 → camera.capture() 计时 → 解码计时 → **detect() 计时 → engine.py 排序/裁剪 → 逐行 recognize() 计时**（read() 是一体模板方法拆不开；detect/recognize 是公开接口）→ 文本行 + 三段耗时
- OCR 推理 `asyncio.to_thread` 执行（先例 reading.py:256）；不创建朗读会话、不动 speaker

**Patterns to follow:**
- reading.py _grab 解码与 to_thread 先例；engine.py sort_reading_order/crop 函数

**Test scenarios:**
- Covers AE2 (origin AE2). 隐私 ON → 拒绝；隐私 OFF → 文本 + 三段耗时（fake 相机/OCR）
- Happy path: 耗时键齐全（capture/decode/det/rec）且非负
- Edge case: 互斥持有中（录音进行时）→ 409
- Error path: 相机 HalError（x86 无 /dev/video0）→ 友好错误

**Verification:**
- AE2 语义测试钉住；x86 fake 全流程绿

---

### U5. 同步测试（手动拉取 + 同步状态展示）

**Goal:** 会话存活期立即生效的手动拉取 + 同步状态区。

**Requirements:** R6, R1, R11

**Dependencies:** U1, U2

**Files:**
- Modify: `device/lira/main.py`（runtime 持有当前 SyncClient 句柄：_run_sync connect 后设 `self._sync_client`、finally 清空）
- Modify: `device/lira/sync.py`（run_forever 的 heartbeat 等待分支于 {transport.receive, pull_event}：pull 事件到 → 立即 heartbeat_once）
- Modify: `device/lira/ui/dev.py`（pull 端点 + 状态区）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- **双路机制**（会话存活期 supervisor 阻塞在 run_forever 内，仅退避事件够不着）：runtime 暴露 pull 句柄 → 会话存活 → spawn `client.heartbeat_once()`（仅 send，与 run_forever 的 receive 并发安全）；未连接 → 置 pull_event 缩短退避等待
- 状态区：会话枚举态、(epoch, version)、自由文本中断原因与退避（仅鉴权后展示）
- pull 端点操作经 lira.dev 留痕

**Patterns to follow:**
- sync.py run_forever 的 wait_for(receive, period) 结构（本单元扩展其等待集合）

**Test scenarios:**
- Happy path: 会话保持时点 pull → 伪造 transport 收到 PullMsg（经 client 句柄直发路径）
- Edge case: 会话中断退避期点 pull → 提前进入下一轮重连尝试
- Happy path: 未配置 ws_url → 端点返回"未配置"而非报错
- Happy path: pull 操作在 lira.dev 留痕

**Verification:**
- 后台改配置 → 控制台点拉取 → (epoch, version) 前进可见（含会话存活场景）

---

### U6. 整机场景回放（唤醒→指令注入）

**Goal:** 场景回放端点：直驱运行时引擎文本通道，带竞争守卫与超时，展示迁移与播报。

**Requirements:** R7, R8, R11

**Dependencies:** U1, U2

**Files:**
- Modify: `device/lira/ui/dev.py`（replay 端点 + 结果区）
- Modify: `device/lira/main.py`（DeviceCallbacks.speak 增加最近播报环形记录，供回放/状态区展示）
- Test: `device/tests/test_dev_console.py`

**Approach:**
- 请求缺 confirm 标记 → 拒绝（二次确认门槛，不触碰引擎）
- **显式隐私预检**（on_wake 被门拒时引擎静默返回，不可等状态区分原因）→ privacy ON 直接返回"唤醒被拒（隐私）"
- STANDBY 守卫（非待机拒绝并返回当前态）→ spawn on_wake → **轮询 LISTENING，~2s 超时**（镜像 harness.wait_state；超时返回"唤醒未落地"失败结果与已采集迁移）
- **会话竞争守卫**：注入前复核仍处于本回放唤醒的会话（引擎无会话标识——实现期以"STANDBY→LISTENING 迁移发生后 X 秒内 + 注入前复核 state==LISTENING"落地；评审提出的引擎会话序号守卫列为实现期设计补强，真实唤醒词中途插入 → 竞争守卫中止并如实报告"会话竞争，回放中止"）
- 注入 on_asr_text → 采集 transitions 增量 + 最近播报记录返回；音频照常出声（真实链路语义）
- 全程经 U1 互斥锁；文本参数 POST body

**Patterns to follow:**
- e2e harness say_text 通道与 wait_state 超时模式；runtime.spawn

**Test scenarios:**
- Covers AE5 (origin AE5). Happy path: 回放"打开台灯" → transitions 含 listening→executing→standby、mock IR 记录可见
- Edge case: 引擎非 STANDBY → 拒绝并返回当前状态
- Edge case: 缺 confirm → 不触碰引擎直接拒绝
- Covers plan R8（origin R13）语义. 隐私 ON → 显式预检返回"唤醒被拒（隐私）"，无指令执行
- Edge case: 引擎不迁移（注入失败源）→ 2s 超时返回失败结果而非挂起

**Verification:**
- 控制台可见完整迁移链与播报；竞争/超时/隐私路径均有如实结果

---

## System-Wide Impact

- **Interaction graph:** dev 端点与主循环同事件循环共享 runtime——变更类端点经 U1 互斥锁串行，其余操作串行于引擎/管线 await 点之间；服务端录音与主循环采集并存（PipeWire 多路捕获；录音窗口内主循环 KWS/ASR 仍在吃同设备流，测试页面注明）
- **Error propagation:** dev 端点异常折叠为 JSON 错误 + lira.dev 日志，不外抛；鉴权失败是 401 JSON 而非异常
- **State lifecycle risks:** 录音内存缓冲随响应释放；会话 cookie TTL + 改口令轮换；replay 竞争守卫防真实语音交错
- **API surface parity:** /status 新字段纯增量且仅枚举/布尔/数值（自由文本不入不鉴权面）；老人 UI 不变（dev 入口仅 enabled 时出现）
- **Integration coverage:** AE1/AE2/AE4/AE5 语义进测试套件；真实后台联调随 M7 实机验证顺带覆盖
- **Unchanged invariants:** **内容零落盘升级为采集也零落盘**（录音内存化）；隐私 fail-closed（服务端逐端点权威 + 引擎唤醒门双层）；单进程编排器、单实例运行时

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| 服务端录音与主循环同时采集在板上异常（PipeWire 行为差异） | M7 实机验证先单测；退路 = muted 窗口（暂停主循环采集 10s，期间 engine.tick 停 → 恢复后 LISTENING 会话超时播报属已知后果） |
| dev 端点误触发（真人在旁时 TTS 出声/回放执行） | 互斥锁 + 留痕 + 回放二次确认 + 页面"避开真实使用"提示；v1 无 IR 即无发射风险 |
| 会话 cookie 鉴权弱于账号体系 | TTL 4h + SameSite=Strict + 全局限速 + 密钥随机持久化 + 局域网边界 + 区块默认关；LAN 锁定循环骚扰为接受风险（单口令全局计数的固有代价） |
| runtime 状态面泄露敏感信息 | /status 仅枚举/布尔/数值；识别内容只在 dev 响应往返；录音零落盘 |
| x86 测试环境无声卡/相机/模型 | 注入替身（FakeTts/录音失败源/FakeOcr/夹具 wav + requires_asr 门控），板上实机项进 M7 冒烟 |
| 回放与真实语音竞争 | 竞争守卫 + 如实报告；M5 真实 IR 到货前升级评估（Deferred） |

---

## Documentation / Operational Notes

- SETUP.md 2.8 增补：dev_console 配置段、bool env 覆盖语义、控制台用法（M7 实机验证辅助）；2.9 冒烟清单注明可经控制台远程执行的项
- origin 的 4 项 Deferred to Planning 全部在 Resolved 中落定

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-10-03-dev-console-requirements.md](../brainstorms/2026-10-03-dev-console-requirements.md)
- Related code: `device/lira/ui/app.py`、`device/lira/main.py`、`device/lira/sync.py`、`device/tests/e2e/harness.py`、`backend/app/auth.py`
- Related review record: `docs/residual-review-findings/1222d13-m7-main-loop-wiring.md`（口令限速/CSRF 债、TTS 并发残留）
