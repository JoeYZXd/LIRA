# 豆包 Vivi 2.0 试听

按用户本轮指定调用豆包单向流式语音合成 API，生成两个试听文件。仅发送公开词语“山顶。”与“形状。”及用户给定语音指令，没有发送孩子资料、教材历史或照片。此次授权仅测试语音合成，不改变 KIMI 的任务调度角色，不启动电视任务，也未更换正式运行配置。

请求：POST https://openspeech.bytedance.com/api/v3/tts/unidirectional ，resource=seed-tts-2.0，speaker=zh_female_vv_uranus_bigtts（Vivi 2.0）；audio_params 中 speech_rate=-20、loudness_rate=30、format=pcm、sample_rate=24000。additions JSON 字符串含 post_process.pitch=0 与 context_texts=["朗读 播音 拉长音 着重强调 标准普通话"]。指令保持用户原文，不拆成多个数组元素；没有另加音频增益、保调慢放或改采样率。

使用设备已有豆包 API 凭证，密钥仅驻留设备进程内存，不出现在命令行或凭据文档。顺序调用各一次，收齐所有音频块后要求20000000终止成功，并验证非空偶数字节，再封装24kHz单声道PCM16 WAV；本地复制后验证SHA256。

| 词语 | 请求到完整音频 | 首音频块 | 音频时长 |
|---|---:|---:|---:|
| 山顶 | 0.634秒 | 0.577秒 | 1.250秒 |
| 形状 | 0.571秒 | 0.495秒 | 1.462秒 |

这些是当前网络与这两条短文本的实测，不能推算并发或长文本保证。服务端成功说明请求完成，不等同于人耳确认每一处语音指令效果；待用户试听确认发音、声调和节奏。

文件、脱敏请求参数、耗时和可重复脚本在 artifacts/doubao-vivi2-preview-20261021/。完整源文件另保留设备 /home/orangepi/lira/experiments/doubao-vivi2-preview。

官方音色列表：https://docs.volcengine.com/docs/DoubaoVoice/Tonelist-1?lang=zh
官方参数文档：https://docs.volcengine.com/docs/DoubaoVoice/HTTPChunkedSSEUnidirectionalStreaming-V3?lang=zh
