# 经典链路回归边界

Root 全量非浏览器测试：**1546 passed，3 failed，182.39秒**。未将全量结果标为通过。三项单独复现仍失败（22.15秒），原始摘要见 `tmp/teaching-classic-audio-check.txt`。

失败共用真实唤醒前置条件：

- `tests/test_audio_stack.py::TestWakeWordDetection::test_wav_injection_hits_and_reset_allows_next_hit`：注入音频未命中 KWS。
- `tests/e2e/test_ae5_learn.py::test_ae5_learn_via_real_voice`：唤醒后仍 STANDBY，未进入 LISTENING。
- `tests/e2e/test_ae8_voice_mechanisms.py::test_shutdown_real_wake_prefix`：唤醒前缀未触发，speak_log 为空。

只读核查发现，在本次工作开始前的 HEAD `a20791d15dbd679df2b5abed8491c9356e006cc6` 中已存在测试输入与实际关键词不一致：

```text
git show HEAD:assets/keywords/wakeword_raw.txt
x iǎo l ù @小鹿

git show HEAD:device/tests/test_audio_stack.py
WAKEWORD = "小丽拉"

git show HEAD:device/tests/e2e/harness.py
WAKEWORD = "小丽拉"
```

`device/tests/e2e/make_audio_fixtures.py` 同样以“小丽拉”生成 wake 和 wake_shutdown。`lira.audio._paths.WAKEWORD_KEYWORDS_FILE` 指向上述“小鹿”词表。这解释了三个共享唤醒前置条件的失败；相关测试直接使用经典 KWS/TTS/MockAudioIO，不使用新增豆包能力。

本次未调整词表、经典 KWS/ASR/TTS、路由或这些旧测试及音频，遵守经典链路不动的范围。此前用户已有 TTS、路由等工作区改动保持原样，不将它们混入本次修复。256项原计划专项回归全部通过（19.09秒），包含新能力关闭、PersonalMemory、既有课本与电视兼容分支；它不抹去上述全量失败。
