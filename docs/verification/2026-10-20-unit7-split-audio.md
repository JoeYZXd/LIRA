# “默写第7单元”真实云端与设备验证

结果：未通过完整默写验收。没有改成第7课，没有发起未核对教材的内容播报。

## 方法与边界

模拟语音“默写第7单元。”经设备本地 TTS 生成，实际送到豆包实时云端 ASR；使用已部署的真实教材、电视工具和分流播放器。测试独立会话，不加载生产家庭身份/个人记忆/红外处理器，因此不是完整生产会话的身份与记忆验证。生产实时会话在测试前停止、结束后恢复 listening；没有重启服务、改变链路模式、改动经典链路或课本。

首轮接收端前台状态为 false。用 ADB 打开既有接收应用后重新开始完整独立会话；“电视已打开”是依据实时 interactive=true、foreground=true 生成的模拟回应，不是用户现场口述。没有预填课本、课文或默写类别。

## 实际交互

1. 原始输入“默写第7单元。”；云端 ASR 为“我写第 7 单元。”，存在首词识别偏差。
2. 模型调用 check_dictation_tv，返回 needs_input，并从 LIRA 音箱询问“电视已经打开了吗？”
3. 接收应用在前台后，模拟输入“电视已打开。”；ASR 一致。
4. 模型依次调用 list_practice_textbooks → show_dictation_ready(lesson=18) → get_tv_display_status → list_practice_textbooks。
5. 电视真实显示“准备开始默写第18课”。返回 rendered_id 与发送 id 一致，截图也确认该画面。
6. 模型从 LIRA 音箱说明：“第七单元对应第18到21课，目前只有第7、8课核对完成，第七单元的课还没核对好，暂时没法直接默写哦。”

## 结果与问题

- 音箱播放器完成 302100 帧 / 12.5875 秒云端 PCM；本轮未发起电视音频，tv_audio_frames=0。普通回答分流得到实际播放器完成证据，电视默写音频分流没有本轮证据。
- 5 个实际工具调用及其返回均保存；5 个工具结果均真实回传云端，sent=true。
- 目录实际显示第七单元是第18、19、20课，三课 reviewed=false；第21课属于第八单元。模型说“18到21课”与工具数据不符。
- 模型说“只有第7、8课核对完成”也不准确：目录中第1、2课同样 reviewed=true。
- 在明确第七单元未具备可用材料前就显示了第18课准备画面；没有完整询问准备画面是否可见，也没有单元范围的明确准备展示。当前 show_dictation_ready/get_dictation_material 接口以单课为中心，尚不能据此宣称单元默写流程已实现。
- 未调用 get_dictation_material 或 present_dictation_item，没有开始未核对内容的默写，因此不能验证三遍播报、下一条等待或电视内容音频。
- 服务 PID/状态与课本文件哈希均未改变；结束后生产会话恢复为 doubao_realtime / listening / cloud_active=true，输出模式 device_with_tv_dictation。

## 原始证据

- [初轮真实调用与返回](2026-10-20-unit7-split-audio-first.json)
- [打开电视接收应用后的完整真实调用与返回](2026-10-20-unit7-split-audio.json)
- [电视现场截图](2026-10-20-unit7-tv.png)
- [生产会话恢复状态](2026-10-20-unit7-restored-session.json)

原始 JSON 包含工具 arguments/result、云端工具结果回传、实际电视 payload/ack、音频帧计数与测试限制。审计目录中的第7课观察字段源自旧验证脚本，仅为旁路观察，没有作为模型输入或本轮请求目标。
