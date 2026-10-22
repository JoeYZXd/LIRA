# 板端语音延迟对比测试

目标：比较相同固定文本的板端 Melo 实时合成、已缓存 PCM 播报和火山引擎流式 TTS。固定文本覆盖短回应、录入提示和较长说明，每种路径每句重复三次。

## 测量口径

- 开始：完整文本提交给播报路径；不含 ASR、Jev 或大模型生成文字的耗时。
- 首次输出：音频驱动第一次消费有效 PCM；未使用麦克风测量实际扬声器发声。
- 完成：播放路径排空并返回完成；包含音频本身的时长。
- 本地模型初始化、缓存预生成单独处理，不计入每次响应。
- 同一板子、输出设备、当前音量与语速；云端使用 24 kHz PCM，本地保持模型原生采样率。两种音色的自然语速不同，因此完成时间应结合播放时长判断。
- 这是独立播报基线测试，主循环暂时停止，不能代表与 ASR/视觉任务并行时的负载表现。

## 云端配置

板子文件：`/home/orangepi/.config/lira-tts-benchmark.env`，权限 600，属主 orangepi。它不加载到正式服务。

```dotenv
LIRA_ONLINE_TTS_API_KEY=填写火山语音控制台的APIKey
LIRA_ONLINE_TTS_RESOURCE_ID=seed-tts-2.0
LIRA_ONLINE_TTS_SPEAKER=填写与资源匹配的音色ID
```

API Key 与音色 ID 来自火山语音控制台；不要将密钥贴入聊天、提交到仓库或附在结果文件中。资源 ID 必须与开通的模型、选定音色匹配。

协议依据：[火山引擎双向流式 TTS 官方文档](https://docs.volcengine.com/docs/DoubaoVoice/bidirectional-streaming-text-to-speech-websocket?lang=zh)。

## 运行

在板子上：

```bash
cd /home/orangepi/lira/device
.venv/bin/python scripts/benchmark_tts.py
# 配置云端后，重新运行三路对比：
.venv/bin/python scripts/benchmark_tts.py --cloud
```

会真实播报测试语料。脚本读取当前服务运行配置与音量、语速，停止服务，完成后在 finally 中恢复服务。请避免手动中断进程；若 SSH 断开，检查 `systemctl --user status lira`，必要时执行 `systemctl --user start lira`。

结果位于 `/home/orangepi/lira/data/tts-benchmark-local.json` 或 `tts-benchmark-cloud.json`。云端直接调用现有 VolcengineProvider，失败会报错，不会回退本地后冒充云端结果。

## 结果

本地基线已完成，每格为三次测量的中位数，单位秒。板端 Melo、4 线程、44.1 kHz，音量与语速均为 1。

| 语料 | 本地首次输出 | 缓存首次输出 | 本地完成 | 缓存完成 |
|---|---:|---:|---:|---:|
| 我在。 | 0.638 | 0.058 | 1.374 | 0.793 |
| 录入提示，20 字（含标点） | 2.786 | 0.055 | 6.100 | 3.556 |
| 较长说明，43 字（含标点） | 2.166 | 0.056 | 10.976 | 7.708 |

18 次播报全部成功，未记录设备 underflow 或指标丢失。长句实时合成三次各记录一次供数不足；其余为零。缓存使用整句合成 PCM，实时路径按生产逻辑分段合成，停顿与音频时长可能不同，因此完成时间差不能全部解释为首次等待差。模型初始化与固定反馈预热共 7.32 秒，未计入响应时间。

精简原始结果：[tts-benchmark-local-2026-10-16.json](tts-benchmark-local-2026-10-16.json)。完整逐回调事件仍保存在板子原始结果文件中。

服务已恢复运行。云端等待测试密钥与音色配置，尚无实测结果，不能据此判断云端与本地实时合成谁更快。
