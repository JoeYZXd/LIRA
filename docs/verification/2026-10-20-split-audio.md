# 实时语音音频分流恢复

- 普通豆包回答使用原 LIRA SoundDevicePlayer 音箱输出；现有默写工具的指定文本播报使用配对电视 TvAudioPlayer。每条音频在创建播放任务时固定出口。
- 电视离线不会阻止普通会话启动；指定电视播报失败不回退到音箱。停止、关闭同时清理两个出口。
- 只部署 conversation_paths.py、realtime_audio_output.py 和独立音频输出配置。部署回执确认其他源码、课本数据、链路选择均未改变，扩展能力仍关闭。
- 设备 Python 环境执行音频、投屏、电视确认、默写、会话与新增分流测试：130 passed。Windows Python 3.10 为 64 passed / 1 个既有 TimeoutError 兼容性失败，设备环境全部通过。
- 部署后实际启动豆包实时会话：listening，cloud_active=true，无错误，输出模式 device_with_tv_dictation。音箱输出设备初始化成功。本次未再次发起默写，也未获得用户听觉确认，不据此宣称已听到实际回答。
- 电视显示及已有指定内容播报继续使用现有实现。新设计的电视内置默写流程尚未实现。

回执：[部署](2026-10-20-split-audio-deployment.json)、[真实会话启动](2026-10-20-split-audio-session.json)。
