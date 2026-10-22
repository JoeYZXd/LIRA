# 电视音频能力与第7课二会字真机验证

2026-10-20，已实施并部署。真实豆包实时云端输出经局域网 PCM 流送到电视内置扬声器；已核对第7课《古诗二首》的13个二会字按8+5实际投屏，等待“更多”后才准备下一页。经典链路未改，未开放通用扩展能力。

## 实施及代码依据

- `tv-receiver/src/AudioReceiver.java`：协议0.3，新增鉴权音频接收、限流、逐轨扬声器选择、AudioTrack播放、实际播放帧回执及按ID中止。队列最多4块，每块最多0.5秒，单流最多120秒；音频仅在内存。
- `tv-receiver/src/ReceiverService.java`：增加 /audio/start、/audio/chunk、/audio/end、/audio/stop 和 /status.audio；保留原文字/图片/清屏协议。
- `tv-receiver/src/Screen.java`：退出前台停止播放；音频焦点回调也只停止所属流，不误停新会话。
- `device/lira/tv_audio.py`：直接消费豆包会话输出PCM，无本地替代语音。分块顺序和回执校验；明确队列满可重试，网络结果不明不重播；只有电视实际播放帧满足要求才完成。失败不会偷偷切回设备扬声器。
- `device/lira/conversation_paths.py`：只在创建豆包实时会话时读取 `LIRA_REALTIME_AUDIO_OUTPUT=tv`；经典语音消费入口未改。部署前后的[精确差异](2026-10-20-tv-audio-realtime-routing.diff)可复核。
- `device/lira/tv_voice.py`：修复本轮遇到的完整语句确认误拒绝；只在本次问题已询问且等待回应时，接受明确肯定/投屏祈使句。否定、条件、疑问句不匹配；原有过期、内容绑定及下一轮要求保留。

本次没有更改教材内容、核对状态、课次数据、默写流程或模型默认教学指令，没有打开通用扩展能力。

## 最终真实云端运行

[完整工具调用、参数、返回、实际发送回执、电视载荷、音频回执和观察记录](2026-10-20-0826-lesson7-two-tv-audio-cloud.json)，[机器校验摘要](2026-10-20-0826-tv-audio-summary.json)。远端原件 `/tmp/lira-lesson7-two-tv-audio-20261020-082640.json`。

输入为本机Melo合成语音，经PCM上传豆包ASR；每次模拟答复根据真实模型提问选择。使用设备部署的 RealtimeSession、默认模型/工具、DictationTools、TvVoiceTools和真实教材；脚本没有直接发起教材/投屏工具动作，也没有改写模型调用参数或ASR文本。未加载家庭身份/个人记忆/红外，当前仅一本语文教材，无需多本记忆分支。

| 回合 | 模拟输入与实际模型动作 | 结果 |
|---|---|---|
| 1 | “给开心默写第七课”→check_dictation_tv | 返回真实接收端状态，并询问电视是否已打开 |
| 2 | “已经打开了”→show_dictation_ready(lesson=7)、get_tv_display_status | 第7课准备画面获匹配渲染ID及前台回执 |
| 3 | 确认准备画面，选二会字→list_practice_textbooks、get_dictation_material(7)、present_dictation_item(group=two, offset=0) | 单本语文教材，已核对第7课；本页8个，awaiting_consent |
| 4 | “可以，请把拼音显示在电视上”→confirm_tv_display、get_tv_display_status | 首屏显示8个拼音；自然语句ASR为“可以，你把拼音显示在电视上”，确认有效 |
| 观察 | 20秒不发送参与者输入 | 新工具调用0、新展示载荷0，电视渲染ID前后不变 |
| 5 | “更多”→present_dictation_item(group=two, offset=8) | 只在此指令之后准备下一页；剩余5个，awaiting_consent |
| 6 | 回答投屏询问→confirm_tv_display、get_tv_display_status | 第二屏显示5个，课次内容全部展示完成 |
| 观察 | 再次20秒不发送参与者输入 | 新工具调用0、新展示载荷0，第二屏保持 |

首屏：`dēng táng yī jìn yù qióng céng pù`；第二屏：`bù lú zǐ yān yáo`。

[电视首屏截图](2026-10-20-0826-two-page1.png) · [电视第二屏截图](2026-10-20-0826-two-page2.png)。截图已目视核查8个及5个拼音，无汉字替代。

注意实际交互仍为“更多”→询问本页投屏许可→模拟同意→显示下一页，并非收到“更多”后无需确认立即翻页。没有把额外确认隐去。

最终共11条真实工具返回，wire实际发送均为true。6段豆包云端语音均在电视回报completed，PCM共603016帧，约25.13秒；每段播放帧=写入帧=发送字节数/2，实际路由类型均为2（Android内置扬声器），电视音量6，未静音。回执保留音频会话ID、逐流PCM SHA256及字节数；没有把合成输入语音或预制文件当作豆包输出。未另做外部麦克风听感录音。

## 失败证据与修复边界

- [08:16完整句确认误拒绝](2026-10-20-0816-two-tv-audio-consent-failure.json)：电视音频已播放，但“可以先把拼音显示在电视上”被旧短词白名单拒绝，未通过分页验收。
- [08:22开机答复ASR失败](2026-10-20-0822-two-tv-audio-asr-failure.json)：“电视机”被识别为“延时机”，开机确认未通过；未把真实电视前台状态冒充用户确认。后续模拟答复改为自然短答“已经打开了”。
- [08:23肯定前缀丢失](2026-10-20-0823-two-tv-audio-imperative-failure.json)：ASR仅保留“你把拼音显示在电视上”，同样是当前询问后的明确指令，补齐祈使句识别后再测。

最终首句ASR文本仍为“你开心默写力气课”，虽然模型实际正确调用第7课并完成本轮；因此本报告只认定电视音频、真实教材与分页/等待的本轮验证通过，不把语音识别稳定性或真实孩子麦克风场景宣称为整体通过。各次原始识别文本未修改。

## 验证与部署

- 设备Python3.11隔离候选代码：128项回归全部通过，涵盖音频传输、教材、分页、电视确认及经典/实时路由；[原始输出](2026-10-20-tv-audio-tests.log)。未将测试依赖安装到服务虚拟环境。
- 现有电视客户端6项单元测试通过；接收端APK构建、签名校验成功，安装包SHA256与本机最终构建一致，见[接收端部署证据](2026-10-20-tv-audio-receiver-deployment.json)。
- [真实接收端拒绝请求](2026-10-20-tv-audio-live-rejections.json)：无许可、错误采样率、无效流所有权均拒绝且不改变音频状态。
- [真实接收端中止和所有权](2026-10-20-tv-audio-live-ownership.json)：实际AudioTrack上使用短静音PCM验证按ID中止、重复ID不重播、旧stop不影响新流以及12000帧排空；该项独立于最终豆包音频验收，不以静音数据代替真实云端播报。
- [代码复核](2026-10-20-tv-audio-code-review.json)：最终无未解决发现；已修复迟到焦点回调竞态、最后排空窗口路由复查和疑问标点被归一化丢弃的问题。简化检查无额外修改；没有独立项目lint配置，Python编译及相关diff空白检查通过。
- Windows Python3.10曾因缺少测试依赖、既有asyncio TimeoutError版本差异失败；补齐本地依赖后61项直接相关测试通过，完整相关组合以设备Python3.11的128项结果为准，未为通过测试放宽断言。

[最终设备部署回执](2026-10-20-tv-audio-deployment.json)。服务PID91082，active，NRestarts=0；最终云端测试前后服务状态完全相同，教材JSON哈希完全相同。`LIRA_REALTIME_CAPABILITIES=false`；只用systemd独立drop-in设置实时音频输出tv，没有变更全局ALSA/PipeWire、电视音量或经典链路配置。

原始音频接线前备份：`/home/orangepi/lira/deploy-backups/tv-audio-20261020-081504`。最终本轮部署前备份：`/home/orangepi/lira/deploy-backups/tv-audio-20261020-082605`。原电视APK备份位于本机 `tv-receiver/build/pre-audio-receiver.apk`（包含本地配对信息，不公开分发）。未git暂存、提交或推送，既有索引SHA256保持不变。

## 运行边界与后续真实体验

当前实时输出明确设为电视，因此正常唤醒使用前应保持已配对的LIRA电视接收界面在前台、电视已开机且音量非零；不可用会失败，不自动回退到设备扬声器。经典链路继续使用原有输出。最终测试结束保留第二页画面。

正式测试时观察豆包是否从电视发声、电视工具是否displayed、音频是否completed及两页内容；如持续failed、前台丢失或实际路由异常，停止本次会话并保留对应回执。可将独立drop-in中的实时输出显式改为device并重启服务作为维护回退，但这会恢复设备扬声器而不满足本轮电视出声要求，不能将其算作验收通过；本轮未执行该回退。未验证所有Android焦点/硬件路由切换和长时间网络波动组合。
