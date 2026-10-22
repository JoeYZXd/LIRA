# 开发者控制台 App 中心

2026-10-16，将控制台改为左侧 App 中心与右侧多窗口工作区。

## 行为

- 左侧 9 个 App 使用 SVG 图标、名称与打开状态；点击打开或关闭，不会互斥。
- 默认打开 TypeSafe Jev 测试和运行参数；右侧 flex 根据可用宽度排成一列、两列或三列。
- 关闭窗口保留表单输入；Escape 关闭当前窗口并把焦点返回对应 App 按钮。
- 全部关闭时显示空工作区。移动端保留左侧紧凑图标栏，右侧单列。
- 关闭实时区停止观察订阅；关闭视觉区停止相机预览；关闭 Jev 测试窗口取消正在进行的测试。后端无法连接时仍遵循既有错误提示与服务端到期机制，不假装已停止。

## 验证

- 控制台与 Jev：80 passed。
- 完整控制台浏览器组：31 passed，70.81 秒。覆盖 390、768、1440、1920 像素、多 App 同时显示、自动换列、关闭重开保留输入、空工作区、键盘焦点、实时观察与关闭 Jev 时取消。
- JS 语法与 git diff --check 通过。截图已人工检查桌面和窄屏。

## 部署

- 仅更新 dev.html、dev-console.css、dev-console.js、dev-jev-probe.js 四个文件。替换前全部匹配上次部署哈希或未修改的 HEAD 源码，替换后哈希一致。
- 源码包 SHA-256：`fa738eca7560a4a6a41f578827c67b75b7c17d07a6911850a48fc6d5db55126c`。
- 备份目录：`/home/orangepi/lira/deploy-backups/app-center-20261016-115726`，before.tar 保存原四文件，manifest.json 记录前后哈希。
- 无需重启服务，MainPID 保持 58187，NRestarts=0，active。新增资源版本参数 `app-center-r1`，刷新后请求新资源。
- 板上鉴权 GET /dev 已验证 App 中心、工作区和资源版本；三个静态资源均为新版本。测试端点 configured=true，/status standby，audio/OCR ready。未触发音频播放或 TypeSafe 付费请求。
- 配置、数据库、模型、密钥、音频设置和后端代码未改动。

## 周期收音日志压缩（2026-10-16）

用户反馈周期 audio 消息刷屏。前端将连续、同路由/播报占用/静音状态的正常收音合并为一行，显示起止时间和累计次数，可展开最近一次原始明细；刷新数据时保持该行展开。按摘要计入前端有界缓冲，避免重复项挤掉已接收的唤醒/识别事件。状态变化、非零削波、KWS 未装配或不匹配的消息保留独立行；模型和系统日志按原逻辑显示，服务端原始记录不变。

先复现两个浏览器失败，修复后摘要/轮次相关浏览器 6 passed，控制台 64 passed，JS 语法与 diff 检查通过；覆盖连续 601 次收音后保留关键事件、展开状态保持、路由改变、削波不隐藏。

已热更新 dev.html、dev-console.css、dev-console.js 三文件，资源版本 audio-summary-r1。备份 `/home/orangepi/lira/deploy-backups/audio-log-20261016-130237`。更新前后哈希均核对；板上真实页面和资源验证通过，PID 保持 62129、NRestarts=0，未重启服务。刷新控制台页面后生效。

## 唤醒应答日志重复（2026-10-16）

用户反馈“小鹿小鹿”后两条“我在”。板上真实遥测记录在 13:09:31.488 同时有 seq=2316（owner=null）和 seq=2317（owner=1.1）的相同播报预览，整条唤醒链仅一次唤醒处理。追踪 `_observe_speech` 确认先调用 VOICE_LOG.log，注册的 sink 自动镜像到 DevTelemetry，随后 safe_add_event 又发送同文带 owner 的事件；TTS.speak 在此之后只调用一次。

回归使用真实 DeviceCallbacks.chime、VoiceLog 镜像及 DevTelemetry，证明旧代码一次 TTS 请求产生两条事件。最小修复让 VoiceLog.log 支持 mirror=False，仅在已有显式带归属事件的播报观察处关闭镜像，保留旧日志环与单一归属事件。没有按文本全局去重；两次实际播报请求仍显示两条。修复后遥测、旧日志、流式对话、主装配与跨层观察测试 113 passed。

已部署 main.py 与 voice_log.py，修改前板上两文件匹配 HEAD 规范字节哈希，备份 `/home/orangepi/lira/deploy-backups/wake-log-20261016-131451`。板上通过注入 TTS 替身运行实际回调，确认提示音一次、我在入队一次、带归属日志一条；该检查没有让硬件播报，不等于外部声学录音。重启加载修复，缓存需重新预热。
