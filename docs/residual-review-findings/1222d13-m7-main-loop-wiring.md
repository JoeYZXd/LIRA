# 已接受残留评审发现 — M7 主循环接线（1222d13）

> 来源：ce-code-review（autofix）@ 2211fce..1222d13，2026-10-03。
> 10 位评审员并行（reliability 超时未交付），P0/P1/P2 主体项已在提交
> 7277b23 / cfbd1b8 / 1222d13 修复。以下为用户决议「接受并记录」的残留项，
> 作为 Phase 2 候选清单（与 `deploy/SETUP.md` 1.1.1 已知残留问题同风格）。
> 全量 316（单元+e2e）通过时的已知状态。

## R1（P2）IR 发送失败对用户不可见

- 位置：`device/lira/main.py` DeviceCallbacks._send_ir（log-only）+ `state_machine.py` 乐观播报
- 场景：确认窗口内快照禁用设备 / 码值缺失 / 红外硬件故障 → 第二道闸拒绝后
  用户仍听到"指令已发出"（安全相关的信息失真）
- 评审员：adversarial (P2, conf 100)、correctness (P3, pre_existing)
- 未修原因：乐观播报是计划 R29「单向红外措辞纪律」的既有设计；修复需动状态机
  话术时序（send 结果回调 → 播报），并同步调整 e2e 断言
- 建议方向：_send_ir 携带结果回调 → 失败播报话术（phrasebook 新增）→ 状态机迁移不变

## R2（P2）ws:// 明文同步通道

- 位置：`device/lira/sync_ws.py`（ws_url 为 ws:// 时 token/快照明文传输）
- 场景：局域网 ARP 欺骗可窃取 device token 并伪造更高 version 的快照
  （epoch/version 回放保护本身成立，弱点在传输层；bootstrap 首同步须在可信 LAN）
- 评审员：security (P2, conf 75)，owner=release
- 未修原因：Phase 1 部署边界 = 家庭局域网（SETUP.md 1.1 已明示明文限制）；
  wss/TLS 或快照 HMAC 签名为 Phase 2 项

## R3（P2）TTS 并发播报无串行化

- 位置：`device/lira/audio/tts.py`（speak 每调用建任务，无互斥）+ main.py 四路播报源
- 场景：隐私/快照播报与朗读块重叠 → 两路 sherpa generate 并发（线程安全性未证实）
  + 两路 RawOutputStream 并发开（PipeWire 混音通常可接受；直连 hw: ALSA 设备会打开失败）
- 评审员：performance + adversarial（P2, 合并后 conf 75）
- 未修原因：需要先定重叠策略（排队 vs 顶替）+ 板上实测 PipeWire 行为（M7 实机验证项）

## R4（P2）麦克风拔出 → 静默失聪

- 位置：`device/lira/audio/mic.py` SoundDeviceMic（pre-existing U2）
- 场景：USB 麦拔出/流中断 → 队列耗尽后 read_chunk 永久挂起（无 EOF 无错误），
  进程存活但语音功能静默丧失
- 评审员：adversarial (P2, conf 75, pre_existing)
- 建议方向：回调侧记录活跃时间戳 + read_chunk 停滞阈值 watchfor → HalError →
  进程退出交 systemd 重启

## R5（P3）小项集合

- **sync_ws >4MB 快照**：websockets 1009 关闭 → 监督循环死循环重连（建议识别
  1009 并 ERROR + UI 告警，替代静默循环）
- **learn 阻塞帧循环**：sync.py _handle_learn 内联 await 学习任务，期间不处理
  心跳/帧（WS 断开留下孤儿学习任务）
- **SyncClient.connect() 无超时**：后台完成握手不应答 → 监督循环无限停留在 connect()
- **/status 缺 sync/route 字段**（agent-native，近零成本）：UiServices 加
  sync_connected/route 可调用注入即可让"后台下发→设备播报"可在不靠人耳的情况下验收
- **无界列表**：DialogEngine.transitions / PrivacyState.events 周级运行缓慢增长
  （cap 或环形缓冲）
- **测试缺口**：backoff 健康重置不可观测、_shutdown 顺序断言、sync_ws 守卫分支
  （open 失败折叠/前置守卫/非 JSON/超限帧）、main() exit 3 分支、_wakeword_text 回退分支

## 评审覆盖率说明

- reliability 评审员未交付（10 派 9 交；原因：API 429 限流）。其关注域的关键面
  （重连退避语义、停机顺序、uvicorn 端口失败降级）已由 adversarial/correctness/
  testing 覆盖并在 1222d13 修复；遗留缺口见 R5 测试项
- 其余 9 份产物含完整 residual_risks/testing_gaps；原 JSON 产物：
  `C:\Users\Joey\AppData\Local\Temp\claude-run-artifacts\ce-code-review\`（会话临时目录，已摘录入册）
- 另两条 P3 advisory 未入 R1–R5（低价值备案）：config 默认值三处可漂移
  （DEFAULTS/数据类字段默认/config.yaml 注释，建议 DEFAULTS 为唯一权威）；
  print_assembly 硬编码 '/dev/video0'（与 camera_raw.DEFAULT_DEVICE 重复，
  但惰性导入会破坏 dry-run 的仅 PyYAML 承诺，故保留并注明）
