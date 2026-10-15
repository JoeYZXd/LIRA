# 连续语音的设备音频配置

此配置针对当前 Orange Pi 的 PipeWire 0.3.65 与 WebRTC AEC。应用的输入和输出必须分别经过 `lira_echo_source` 与 `lira_echo_sink`，才能在反馈或朗读播放时继续收音。专用 PCM 不更改桌面默认设备。

## 配置与启动检查

1. 备份应用、`device/config.yaml`、`~/.asoundrc`、用户 PipeWire 配置及 `lira.service` 覆盖设置；记录原本不存在的文件。应用与配置须配套恢复。
2. 用 `pw-dump` 核对物理设备名称。当前 USB 输入是 `alsa_input.usb-Timesintelli_USB_Audio_Device_223484-00.analog-stereo`，扬声器输出是 `alsa_output.platform-es8388-sound.HiFi__hw_rockchipes8388__sink`；硬件变化后需要重新核对。
3. 将 `90-lira-echo-cancel.conf` 安装到 `~/.config/pipewire/pipewire.conf.d/`；将 `lira-asound.conf` 中专用 PCM 合并到 `~/.asoundrc`，保留原配置。不要设置 `PIPEWIRE_NODE` 或 `PIPEWIRE_ALSA` 覆盖变量。
4. 配套应用配置中的 `hal.mic_device`、`hal.speaker_device` 均为 `lira_audio`，`hal.echo_cancel_enabled` 为 `true`。停止 LIRA 后重启用户 PipeWire/WirePlumber，再启动 LIRA。检查用户服务日志和设备状态页面。
5. `pw-dump` 必须找到 `lira_echo_source`（`Audio/Source`）与 `lira_echo_sink`（`Audio/Sink`）。应用会检查节点类型和精确 ALSA PCM，缺失时拒绝启动，不退回普通设备。

仅有节点还不能证明路由或消回声正确。输入与输出同时打开时，使用 `pw-link -l` 或 `pw-dump` 确认四段连接：USB 输入 → `lira_echo_capture`；`lira_echo_source` → 应用采集；应用播放 → `lira_echo_sink`；`lira_echo_playback` → ES8388 输出。无需 `pactl`。

## 验证与恢复

先完成服务、摄像头、收音和输出检查，再按 `docs/voice-continuity-validation.md` 记录真人样本。连续完整指令、回声中的自身播报隔离、实际停止延迟不能靠模拟测试证明。

流式语音改造的同机对照、数值采集与回滚记录见仓库根目录的 [voice-streaming-validation.md](../../../docs/voice-streaming-validation.md)。新流水线继续使用 `lira_audio`，无需为连续 PCM 输出修改专用 PCM、PipeWire 或 service override。HAL 首帧消费和排空时间必须通过外部录音或回环校验，不能当作扬声器实测。

如果配置失败、音频绑定错误或出现自身播报误执行，停止试运行并成对恢复应用和音频配置。原本不存在的新增专用配置也应移除；不要删除用户后来添加的其他配置。重启用户音频服务与 LIRA，重新检查服务和原有读书路径。保留备份与失败记录。
