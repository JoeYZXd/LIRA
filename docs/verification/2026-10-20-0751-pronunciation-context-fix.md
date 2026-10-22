# 课文读音自主判断修复与真实复测

## 变更

按用户要求，明确的“依靠的依”不再要求重复确认。读音优先由模型结合已核对课文、扩词、课文标题与常识判断；只在确实缺少信息或有实质矛盾时澄清。

- 四会字提示兼容“yī，依靠的依”“读cháng，长江的长”等前缀，提取实际词语提示，不再要求提示逐字出现在用户同意文本中。
- 没有提供必要提示时返回 pronunciation_context_required，指示模型自行补齐后重试，不再误报需要用户确认。
- 二会字的有效候选拼音也允许模型结合上下文选择，不逐字征求同意；字表核对、候选有效性、电视展示同意和等待孩子下一条等边界仍保留。
- 材料返回已核对课次的标题与最多4000字的现有课文/扩词，作为读音判断依据；没有编写或修改家庭课文。

本次只部署lira/dictation.py，备份 /home/orangepi/lira/deploy-backups/pronunciation-context-20261020-075003。服务PID72724→75305，模式不变，扩展开关false；部署前后其他代码与家庭教材文件哈希一致。经典链路没有改动。

## 真实部署版复测

使用真实已核对第7课《古诗二首》，真实豆包实时云端、部署的默认指令和工具，没有修改模型工具参数或用脚本编排产品动作。输入仍为合成语音，输出由静音播放器消费。电视使用真实接收端。未加载家庭身份/个人记忆，当前单本语文教材无需多本选择。

| 步骤 | 真实调用及结果 |
|---|---|
| 发起“给开心默写第七课” | ASR正确转写为“给开心默写第7课”；check_dictation_tv → needs_input，询问电视开了吗 |
| 回答电视已打开 | show_dictation_ready(lesson=7) → displaying；get_tv_display_status → displayed，内容编号a8e441270ebc445f84e19d116666d0f4 |
| 回答画面结果并选四会字 | list_practice_textbooks → ok（1本）；get_dictation_material(lesson=7) → ready |
| 首字 | present_dictation_item(offset=0,pronunciation_hint="依靠的依") → queued → 静音播放器观察completed；没有读音确认问题 |
| 等待 | 播后6秒无输入，没有新工具调用、没有新精确语音输出 |
| 明确说“请读下一个字” | ASR正确识别；present_dictation_item(offset=1,pronunciation_hint="尽头的尽") → queued → completed；没有读音确认问题 |
| 再次等待 | 再观察6秒，没有新工具调用或自动播下一字 |

实际发送到云端的精确播报请求：

1. “依靠的依。依靠的依。依靠的依。”
2. “尽头的尽。尽头的尽。尽头的尽。”

两次均收到云端音频并由测试播放器消费完成。已额外保留两段24kHz单声道PCM WAV，分别4.21秒、4.79秒；不是把普通对话音频当默写。所有记录的role=tool回传均由真实session.send发送成功，call_id、完整工具结果及精确语音请求保留在JSON。

本次四个用户话轮完成测试，没有为读音追加“好的”确认，也没有绕过工具手工修正参数。测试结束时服务仍PID75305、active、NRestarts=0，课本文件哈希未变。

## 验证边界与证据

核心检查在本轮通过：电视询问/准备显示、单本课文读取、上下文自主判断、前两字各三遍精确播报、只有下一条指令后才继续。静默观察仅覆盖每次6秒，不是无限等待的稳定性保证。completed证明云端音频由静音测试播放器消费，不替代实体扬声器、真实孩子麦克风、实际读音听感和书写验收。读音提示前缀兼容由回归测试覆盖；本轮云端模型直接提供了干净的“依靠的依”。

原始记录：2026-10-20-0751-lesson7-context-retest-cloud.json。
录音：2026-10-20-0751-lesson7-first-speech.wav、2026-10-20-0751-lesson7-next-speech.wav。
部署回执：2026-10-20-pronunciation-context-deployment.json。

相关自动化回归75项全部通过。回归包含无用户读音确认即可播报、前缀归一化、非法二会拼音拒绝与有效候选允许、独立投屏同意、播报完成/打断/去重以及原有教材与电视行为。未暂存、提交或推送。


本地音频与原始记录：

- [首字三遍音频](/D:/Projects/LIRA/docs/verification/2026-10-20-0751-lesson7-first-speech.wav)
- [下一字三遍音频](/D:/Projects/LIRA/docs/verification/2026-10-20-0751-lesson7-next-speech.wav)
- [原始工具调用与实际回传](/D:/Projects/LIRA/docs/verification/2026-10-20-0751-lesson7-context-retest-cloud.json)
