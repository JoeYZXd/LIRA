# Qwen3-TTS 开发板实测

用户要求用 Qwen3-TTS 替代当前试听音色，优先确认“山顶”的第三声与慢速朗读效果。本次完成真实开发板合成试听，没有启动新默写任务，也未更改生产播报配置。

设备：Orange Pi 5，aarch64，4×Cortex-A55 + 4×Cortex-A76，物理内存约7.7GiB。独立环境位于 /home/orangepi/lira/experiments/qwen3-tts，官方模型 Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice 从官方 ModelScope 仓库下载，本地完整模型约2.5GB。运行版本 qwen-tts 0.1.1、torch 2.9.1+cpu、transformers 4.57.3。未使用 CUDA、FlashAttention 或 Rockchip NPU。

CPU float32、4线程，绑定核心4–7，低优先级运行；临时用户单元设内存6GiB上限、swap512MiB上限和900秒时限。模型加载7.89秒，实测进程峰值5320.87MiB（约5.20GiB）。

| 文本 | 中文音色 | 合成耗时 | 原始音频 | RTF |
|---|---|---:|---:|---:|
| 山顶。 | Serena | 35.59秒 | 1.28秒 | 27.80 |
| 形状。 | Serena | 31.02秒 | 1.28秒 | 24.23 |

两份试听均由该开发板本地模型生成，再用 librosa 保持音高的 tempo=0.75 慢放；不是通过降低 WAV 采样率改变音高。文件含250ms起始与500ms末尾空白，总时长各2.4567秒，24kHz单声道PCM16，复制至工作区后SHA256验证一致。0.6B不应据此声称具备1.7B的完整自然语言韵律控制能力；中文声调质量仍由用户试听判断。

试验进程正常结束、内存已释放，LIRA用户服务仍active。依赖只安装在独立venv，未改动生产依赖。未完成对CPU量化、NPU或实时语音延迟的验证。结论：当前板可运行0.6B官方CPU路径，但这些实测时延不适合作为即时播报；若用于默写，应设计提前生成与缓存，不能直接阻塞当前同步任务准备链路。

凭据、脚本和试听：artifacts/qwen3-tts-board-20261021/。原始模型生成文件保留在设备 preview 目录；本次只向用户交付两个慢速试听，不部署整个默写流程。

官方来源：https://github.com/QwenLM/Qwen3-TTS 与 https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice 。
