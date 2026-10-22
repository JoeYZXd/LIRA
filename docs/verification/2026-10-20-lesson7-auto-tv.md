# 第7课：电视自动检测与音频分流实测

## 已部署变更

- check_dictation_tv 真实检测接收端。status=ok、interactive=true、foreground=true 时返回 ready，不再询问“电视打开了吗”。
- 亮屏状态明确为 false 时提醒打开电视；接收应用不在前台时提醒打开接收应用；连接失败或状态缺失只说明无法确认，不推断关机。
- 删除电视开启的口头确认状态及门槛；show_dictation_ready 仍要求先调用检测工具。投屏准备画面的确认、课本选择与核对要求保留。
- 仅部署 device/lira/dictation.py，沿用 device_with_tv_dictation 输出模式。受保护源码、课本文件、链路选择不变，扩展能力仍关闭，经典链路未修改。
- 设备 Python 环境 134 项相关测试通过，含新增的四种电视状态测试。

## 真实测试方式

使用已部署代码、真实豆包云端 ASR、真实课本工具、实际 LIRA 音箱和电视音频接收端。输入是本地 TTS 模拟语音，不是用户现场录音。独立审计会话不装载家庭身份、个人记忆或红外处理器，不代表完整生产身份链路验收。

原始请求为“默写第7课。”，不再使用第7单元。根据真实接收端已显示的准备画面，模拟确认已看到，并模拟选择四会字；没有预填教材或工具参数。模型自行选择唯一语文教材并读取已核对第7课。测试后恢复生产豆包会话 listening，输出模式不变。

## 第一轮：保留失败

电视检测返回 ready，模型没有询问开机，成功投屏准备画面。但随后重复查询课本、读取课文，没有调用 present_dictation_item，等待超时。本轮证据完整保留，不视为成功。

## 第二轮：部分通过，完整流程未通过

1. check_dictation_tv 返回 ready；show_dictation_ready(lesson=7) 实际投屏，get_tv_display_status 返回 displayed。没有电视开机的口头确认。
2. 普通回复由 LIRA SoundDevicePlayer 输出。
3. 确认准备画面并选择四会字后，模型真实调用 list_practice_textbooks、get_dictation_material(lesson=7)、present_dictation_item(group=four,offset=0,pronunciation_hint="依靠的依")。
4. 精确语音提交文本为“依靠的依。依靠的依。依靠的依。”，sent=true。电视 receipt completed，received_bytes=201034，written_frames=played_frames=100517，routed_device_type=2（电视扬声器），未静音；get_dictation_status=completed。
5. 静默 20 秒期间无新工具调用、无新精确播报、无新投屏，显示 id 与电视音频 id 均保持不变。
6. 模拟“下一条。”被云端识别成“哪一条？”。模型尝试 offset=1，工具返回 awaiting_user，未发起下一段电视音频。
7. 审计脚本随后模拟“四会字，请开始。”，ASR 为“自会字，你开始。”，并非有效下一条指令。模型再次调用 offset=1，工具仍返回 awaiting_user；但模型普通回复自行念出“尽头的尽。尽头的尽。尽头的尽。”，经普通音箱出口输出。这违反了默写内容必须通过指定工具走电视的约定，不能宣称完整分流流程通过。

本轮共 8 个实际工具结果，均回传云端 sent=true。电视实际完成 100517 帧；本地音箱总输出 432752 帧，包含上述错误的普通回复播报，不能把这部分全部算作合规普通对话。未自动推进，没有第二个电视精确播报 receipt。

## 待解决

- 工具结果已回传但模型偶发重复查询，尚无已证实根因。
- 短语音“下一条”的 ASR 稳定性。
- 模型收到 awaiting_user 后仍在普通回复中朗读默写内容，尚未修复。单纯音频分流按播放类型工作正常，但不能约束模型普通回复的语义。

## 证据

- [部署回执](2026-10-20-auto-tv-deployment.json)
- [首轮失败：完整工具与返回](2026-10-20-lesson7-auto-tv-first.json)
- [第二轮：完整调用、回传、电视音频与静默观察](2026-10-20-lesson7-auto-tv-retest.json)
- [恢复生产会话](2026-10-20-auto-tv-restored-session.json)
