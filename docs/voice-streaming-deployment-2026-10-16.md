# 流式语音部署记录

2026-10-16（Asia/Shanghai）用户明确授权部署后，将 `0ec978b205bc5e0ab1151a64caee116f847343ce` 的 18 个设备端源码/依赖声明文件部署到 `/home/orangepi/lira`。板上全部待更新文件均匹配 `1b396cf` 基线，没有覆盖额外改动。源码包 SHA-256：`fc4144d68a03f99647de9285521eeefb631fe0b4f621404daaed450ff9ef3be0`；部署后逐文件校验通过。

保留现有配置、依赖、模型、数据库、专用 PCM、PipeWire 和 service/drop-in。在线 TTS 继续关闭；本地为 Melo、4 线程，实际输出采样率 44100 Hz。LLM 仍使用已有 Kimi K3/搜索配置；此次检查未调用远程 LLM 或付费 TTS。

## 备份与恢复

停止服务后建立私有冷备份：`/home/orangepi/lira/deploy-backups/streaming-voice-20261016-001540`。`cold-backup.tar` 为 329574400 字节，包含设备应用、327 MB venv、配置、设备数据以及现存 ALSA/PipeWire/systemd 配置；备份清单同时记录缺失项和配置哈希。目录权限 0700，归档权限 0600，未将含秘密的配置或备份拉回开发机。

同目录 `before/` 保存被替换的原文件；`rollback-source.py` 仅恢复本次的源码/依赖声明，移除本次新增模块，不覆盖后续用户数据或配置。它在停止服务前拒绝覆盖不再匹配本次部署哈希的源码。必要时运行：

```sh
ssh lira 'python3 /home/orangepi/lira/deploy-backups/streaming-voice-20261016-001540/rollback-source.py'
```

此回滚命令已保存，未执行。真正回滚后仍须检查收音、播报和本地/离线阅读，不能以进程 active 代替验收。完整冷归档中的数据库不应在存在新数据时直接覆盖恢复。

## 已执行检查

- 板上源码编译、配置/模型装配 dry-run 均通过；既有依赖满足要求，未安装或升级包。
- 服务停止时，通过真实 `lira_audio` 输入和输出进行短测试。两段正文在输入尚未结束时开始输出，并用一个输出流完成排空；输出 147968 帧，麦克风共采集 132800 帧。
- 第二会话开始输出后取消，完成状态为 cancelled；确认停止后观察 200 ms，未再消费旧音频帧。
- 短测试设备欠载 0、生产者断供次数/时长 0。两段合成累计 2.733 秒，单样本 RTF 0.814；不含模型加载，不能作为固定语料 p50/p95、长期吞吐或 OCR 并行性能结论。
- 重启后 PID 从 45936 变为 48856，服务 active/running，NRestarts=0；`/status` 返回待机/wake、audio/OCR 模型就绪，隐私和勿扰与部署前一致。
- 新进程日志检查的 Traceback、ModuleNotFoundError、Audio output failed、设备 underflow、Microphone overflow 计数均为 0。
- `lira_echo_source` 为 Audio/Source，`lira_echo_sink` 为 Audio/Sink；配置文件哈希与部署前一致。

## 仍待完成

真人唤醒/对话/搜索/阅读体验、回声误触发检查、外部录音校验扬声器开播/停止、固定语料同机延迟与长阅读/OCR 负载对照尚未完成。上述声卡消费/排空检查不等于声学验收。在线 TTS 未启用，真实账号/音色和云端性能仍未验证。
