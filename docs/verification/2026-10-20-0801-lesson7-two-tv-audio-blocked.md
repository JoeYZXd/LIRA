# 第7课二会字真实电视测试：未通过，电视音频能力缺失

用户要求：模拟发送语音指令，真实电视显示拼音且由电视扬声器播报；每屏最多8个，等待“更多”再翻页，经典链路不动。

## 本轮结果

收到“必须从电视扬声器出声”的明确答复后，停止了使用 LIRA 实体扬声器的隔离测试会话。没有继续把 LIRA 出声视为电视音频验收，也没有安装新接收端、修改音频路由或部署源代码。

本轮模拟首句“给开心默写第七课的二会字，拼音显示在电视上”被真实豆包 ASR 识别为“你开心默写 17 课的二会子拼音显示在电视上”。模型实际调用 check_dictation_tv，随后 show_dictation_ready(lesson=17)，电视接收端返回前台且匹配 rendered_id，准备画面确实发起并获得渲染回执。其后 list_practice_textbooks、get_dictation_material(lesson=17) 返回 lesson_review_required，模型没有绕过核对门禁。未进入第7课的拼音页面，因此8+5分页、等待“更多”和下一页均未验收。中止时尚未发送课次纠正语音。

设备教材仍包含已核对第7课《古诗二首》，二会字13个；正确分页应为8+5。不能把这轮第17课的准备画面当成第7课测试成功。

## 真实记录

[云端调用、完整返回、发送回执、电视载荷与设备保护检查](2026-10-20-0801-lesson7-two-tv-audio-blocked.json)。共5条工具返回，实际发送标记均为 true。实体播放器输出393742帧，3个音频流完成，但输出到 LIRA 当前配置的扬声器，不能证明电视出声。

服务 MainPID=75305、ActiveState=active、NRestarts=0 前后相同；教材 JSON 哈希前后相同。未变更经典链路，扩展能力开关仍为 false。测试脚本为 tmp/teaching-deploy/probe-lesson7-two-physical.py，使用部署代码和真实云端；未编排工具调用。合成参与者的“电视已开机”答复不等于物理开机检测；显示以接收端回执为准，未取得面板照片或截图。

## 电视音频缺口及代码依据

- tv-receiver/src/ReceiverService.java：仅 /status、/show、/clear，Content 为文字/位图；无音频接收、播放器和播放完成回执。
- tv-receiver/client.py、device/lira/tv_display.py：文字/图片展示及匹配 rendered_id 的回执，无音频发送接口。
- device/lira/conversation_paths.py：现有实时语音播放器为 SoundDevicePlayer，使用配置的 lira_audio 输出。
- 实机 wpctl 显示 lira_echo_playback 链接 Built-in Audio Headphones + Speaker；并非电视远程音频。发现 HDMI 输出设备不证明其连接到这台网络投屏电视，故未擅自切换。

继续满足本次验收需要先补齐电视音频传输、播放/中止/完成回执，并把本次实时会话的输出明确路由到电视；这是尚未实现的基础能力。单独通过 ADB 播测试音或预制音频不能证明豆包实际返回音频已经进入电视，也不能替代端到端测试。
