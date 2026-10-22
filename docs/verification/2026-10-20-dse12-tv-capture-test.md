# DS-E12 连接小米电视的视频与音频测试

2026-10-20。用户已连接摄像头并授权测试；没有进行系统升级、应用精简或经典链路修改。

结论：USB设备及麦克风识别通过，实际视频和录音采集未通过。不能仅凭识别结果标记为LIRA可用输入，也不能据此断定摄像头损坏或Android6必然不兼容。

## 设备识别

电视Android6.0.1。USB设备名1080P USB Camera，VID=11231（0x2bdf）、PID=640（0x0280），接口包含视频类14及音频类1。USB音频挂为card1/device0，支持捕获、不支持播放；16kHz、单声道、S16_LE。AudioManager将其列为输入id20、type11、名称USB-Audio - 1080P USB Camera。

相机服务枚举1个设备，厂商Amlogic HAL。尚未取得实际图像，故枚举信息不作为画面成功证据。USB识别记录见 [usb-identification](2026-10-20-dse12-usb-identification.txt)，已排除ADB授权公钥及无关USB调试信息。

## 实际采集

测试应用org.lira.avprobe为本轮临时构建，源代码在tmp/tv-av-probe；CAMERA和RECORD_AUDIO均已授予，appops为allow。录音目标约5秒、视频窗口6至7秒，未上传任何录音或画面到云端。

1. Camera1：可打开、请求640×480 NV21预览，6秒后帧数0，没有生成图像。日志出现Camera Error1。
2. AudioRecord MIC：初始化后选择USB设备被接受，但start出现status -22，read返回0；没有有效音频样本。
3. Camera2：采用640×480 JPEG ImageReader，打开后收到camera_error_code=1，未收到图像。
4. AudioRecord VOICE_RECOGNITION默认路由：初始化不可用。没有为测试切换或伪造麦克风源。
5. shell tinycap直接读card1节点被权限拒绝，0帧；此结果只证明shell访问受限，不证明硬件无法录音。

[第一组原始结果](2026-10-20-dse12-camera1-audio-preferred.json)；[第二组原始结果](2026-10-20-dse12-camera2-audio-default.json)；[相关错误日志](2026-10-20-dse12-capture-errors.txt)。没有可交付的有效照片或录音，未用其他设备样本替代。

## 占用与待查原因

系统com.xiaomi.mitv.karaoke.service进程在测试期间持续输出SoundRecorder录音日志，与/proc/asound/card1/stream0运行中状态一致；这是音频占用线索，尚未通过隔离测试证实它是失败根因。相机还需进一步检查固件HAL、驱动或设备占用。未停止或禁用小米K歌/系统语音组件，未修改USB节点权限或固件配置。

## 清理

临时应用已停止并卸载，pm包查询为空；临时应用的私有数据随卸载删除。保留本地诊断源码与失败记录。已重新打开org.lira.receiver/.Screen；结束时相机服务无活动客户端。未改正式LIRA应用、设备代码或经典链路，未开启云端识别或持续录音。


## 后续授权重测

用户随后授权停止K歌再测。MIC默认路由成功取得4.94秒低音量USB音频，视频仍未通过；后续结论以[停止K歌重测报告](2026-10-20-dse12-karaoke-stopped-retest.md)为准，不覆盖或删除本轮失败证据。
