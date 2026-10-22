# 停止小米K歌后重测DS-E12

2026-10-20。用户授权“停止K歌服务再试下”。通过am force-stop停止com.xiaomi.mitv.karaoke.service，没有卸载、禁用组件或修改系统设置。操作后进程查询为空，/proc/asound/card1/stream0从Running转为Stop。未测得实际声学回声，是否消失仍需用户反馈。

## 音频结果

停止K歌后，VOICE_RECOGNITION默认路由仍初始化失败；MIC配合setPreferredDevice虽然初始化成功，但start失败、recording_state=1、read=0。不能把这两种方式写成可用。

改用AudioRecord的MIC输入、不调用setPreferredDevice后成功。尽管准备了不同采样率/声道的候选，实际第一个16000Hz单声道方案已成功，未继续打开其他格式。getRoutedDevice确认实际type11，名称USB-Audio - 1080P USB Camera，与已枚举的DS-E12 USB音频设备一致，未使用LIRA设备麦克风替代。

短样本取得9760个采样，峰值6；随后相同方式取得79040个16位采样，即4.94秒，原始PCM158080字节。峰值95/32768，RMS约3.096/32768，音量非常低；只判定USB录音数据读取通过，不判定人声清晰、语音识别或边播边听通过。没有把样本上传云端。

[5秒采集原始结果](2026-10-20-dse12-after-karaoke-5s.json) · [本地WAV样本](2026-10-20-dse12-after-karaoke-5s.wav) · [短样本结果](2026-10-20-dse12-after-karaoke-audio-profile.json)。WAV由采集的原始PCM添加文件头生成，没有放大、降噪或替换音源。

## 视频结果

停止K歌后Camera2 JPEG采集仍出现in-use/disconnected错误，未收到有效图像；最后停用并发录音、单独Camera1 NV21预览仍报Fail to connect to camera service。没有图像文件，不认定视频可用。尚未定位相机HAL、驱动或相机服务状态问题，不将其归因为摄像头损坏。

[VOICE_RECOGNITION与Camera2结果](2026-10-20-dse12-after-karaoke-voice-recognition.json) · [强制USB路由结果](2026-10-20-dse12-after-karaoke-mic-preferred.json) · [最后单独视频结果](2026-10-20-dse12-after-karaoke-camera1.json)。

## 结束状态

临时诊断应用org.lira.avprobe已停止并卸载，私有数据随卸载删除；上述本地样本保留。电视已返回LIRA接收界面。K歌进程在结束查询时未运行，按用户要求保持停止，没有恢复K歌实时返听；不能保证它以后不会被系统重新启动。USB Capture最终为Stop。

LIRA服务PID91082、active、NRestarts=0，与上轮状态一致。未修改正式电视应用、设备运行代码、经典链路或系统优化配置。
