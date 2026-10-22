# 真机验证记录 · 2026-10-18

设备：MiTV4，Android 6.0.1/API 23，192.168.3.25。
应用：org.lira.receiver 0.1。范围仅接收展示，不实现唤醒。

| 检查 | 结果 |
|---|---|
| 编译、APK 签名验证与安装 | 通过 |
| 大字、图片显示 | 通过；真机截图检查 |
| 错误密钥、未获许可、坏图片、空文、超大正文 | 拒绝；屏幕内容不变 |
| 慢速未授权请求占用 | 四秒绝对读取期限后释放；正常状态请求可继续 |
| 强制停止后，仅用网络拉起与展示 | 通过，约 4.5 秒 |
| 仅靠 BOOT_COMPLETED 自启动 | 未通过；重启后包 stopped=true |
| 完整重启后，发送工具自动拉起、连接与展示 | 通过；新服务实例及匹配的 rendered_id |
| 重启后物理屏幕 | 用户确认正常显示、文字清楚 |
| 三项发送端回归测试 | 通过 |
| Python 语法、git diff whitespace | 通过 |

第二次重启：指令连接等待最多八秒，之后固定等待十五秒，再用发送工具等待
恢复；后半段耗时 27.4 秒。恢复中未使用 ADB 启动接收应用，也未要求用户
恢复调试开关。一次重启成功不是长期可靠性保证，普通待机循环仍待后续测试。

证据（本机生成，不入库）：`build/text-screen.png`、`build/image-screen.png`、
`build/launch-result.json`、`build/reboot-result.json`。

无原有 Android 测试框架，首版以实际电视编译安装、屏幕截图和局域网请求链
验证行为；后补发送端回归测试，不宣称先红后绿。未触及现有语音链路的代码，
没有运行无关的语音模型与设备测试。

审查：ce-simplify-code 三项行为保持改进已应用；ce-code-review 完成，
绝对读取时限、退出后重拉界面、首次离线后不再尝试拉起的问题已修复。
后两项经独立复核和 test_client.py 验证。审查未发送整个脏工作区到外部模型。

留在电视上的应用是本轮接收应用；临时唤醒诊断应用此前已清理。
下一步是把发送工具接入 LIRA 的语音许可和展示会话。当前 `--approved` 为
调用方声明，不能替代家庭场景的实际询问；未完成自动关机或原输入恢复。


## 2026-10-19 U6 协议 0.2 本地验证（尚未安装或真机验收）

- `/status` 追加 `version: 0.2` 和 `capabilities: [clear_content_id]`，旧展示字段保持兼容。
- 新 `/clear` 仅接受 `expected_content_id`。UI 线程核对屏幕已开、展示仍在前台、当前及已渲染 ID 匹配后，删除内容并渲染等待页；UI 提交后返回 `cleared_id`。不提交空白 `/show`，不启动 Activity，不唤醒、不抢前台。
- 已清 ID 的重试幂等；新内容替换旧 ID 后，旧 ID 不会清除新内容。接收器仅保存最近 64 个清除确认。
- Python 发送端和调试 client 都先探测能力；旧版、离线、屏幕关闭或退出展示不自动启动接收器，不伪报成功。响应 ID 不匹配视为失败。
- 实时扩展通过 `TvVoiceTools.register_capabilities` 注册 `probe_tv_receiver` / `clear_tv_content`；未加入旧 `TV_TOOLS`。功能开关默认关闭，经典链路未加入工具。
- 所有权来自本会话已确认成功的展示 ID；换身份或会话结束撤销所有权和待展示同意，不触发真实清屏。取消待展示请求与清除已显示内容分离。
- 最小 red：新增发送端 clear 测试 2 failed / 4 passed，失败原因为尚无 clear 方法。后续 Python 回归覆盖匹配确认、旧接收器、退出/关闭、离线、替换冲突、错误确认、同意、幂等、身份隔离和迟到确认竞争。
- 本地 `python -m unittest discover -s tv-receiver`：6 passed。
- 本地 `python tv-receiver/build.py`：编译及 APK 签名验证通过，输出 `build/lira-tv.apk`。未打印或修改配对凭据，未安装 APK，未操作电视或 ADB。
- 真实电视上的匹配清除、替换冲突、重复清除、关闭/退出行为仍须以新版接收器实测；本地测试和编译不能代替该门槛。

- 最终设备侧定向回归：`tests/test_tv_voice.py tests/test_tv_display.py tests/test_conversation_paths.py`，79 passed（15.11 秒）；包含实际 executor 成功状态映射及 epoch/track 换人隔离。APK manifest 同步为 versionName 0.2 / versionCode 2。
# 单次真实模型 / 本地 H5 回归（TC1）

入口：`device/scripts/run_dictation_h5_regression.py`。H5 仅替代电视终端，任务准备、模型 function calling、知识上下文、暂停门控和家长照片/核对继续调用正式 Python 服务。H5 使用真实 `AudioBufferSourceNode.onended` 记录 PCM 播放完成；不以计时器伪造排空。它不证明 Android 生命周期、实体电视路由或家庭声学通过。

离线预检（不调用云模型）：

```powershell
python device/scripts/run_dictation_h5_regression.py --preflight
python -m pytest device/tests/test_dictation_regression_runner.py device/tests/browser/test_dictation_h5_long_flow.py -q
```

普通 pytest 中的完整浏览器预检明确使用合成模型响应与合成音调，其报告标注 `OFFLINE`，不能作为真实模型 TC1 证据。真实集成入口默认跳过，不能通过 pytest 自动重试插件反复运行。

真实单次运行使用显式、唯一 `run_id`。运行目录以独占创建锁定，重复 ID 会拒绝；任何完成、失败或 15 分钟超时都会停止并保留报告，最多 60 个孩子输入，不从头重跑、不创建第二个本次业务任务。隔离历史核对 fixture 独立于本次业务任务增量。程序接口：

```python
result = await run_once(
    client=actual_client,  # AsyncOpenAI(timeout=90, max_retries=0)
    model=actual_model,
    synthesize=actual_pcm_provider,  # async (text, speed) -> 24 kHz mono s16le bytes
    run_id="TC1-unique-id",
    artifact_dir=artifact_root,
    request_options=provider_options,
)
```

注入的模型客户端和 TTS 由调用方在 `finally` 关闭；运行器关闭自身浏览器、loopback 服务、TV worker、音频和定时器。CLI 从 `LIRA_TEST_LLM_API_KEY` / `LIRA_TEST_LLM_BASE_URL` / `LIRA_TEST_LLM_MODEL` 读取配置，`--tts-command` 接入实际 JSON-line PCM 子进程。凭据仅驻留内存/环境，不写报告。先准备真实 provider，再显式执行一次；不能把预检结果写成真实云模型通过。

证据位于 `<artifact_dir>/<run_id>/report.json`，包含实际 SDK 响应 ID、模型工具参数/结果、版本哈希、按回执推进的每步状态、TV/服务事件、实际排空、唯一任务计数与收尾状态。另存合成数据 SQLite、任务快照和关键阶段截图。报告须同时满足一个初始请求、一个新业务 task_id、一次完成、ready 后零 prepare 和终态无待执行输入；家长核对后仅通过管理接口读取知识，不发起下一次任务。

## 2026-10-21 实际验收状态

首次真实运行 `dictation-tc1-20261021-01` 已终态失败并完成清理：1 次实际模型调用、0 个新业务任务、0 次完成。模型先询问教材合理；旧断言错误要求澄清前已读取 context，导致提前停止。已离线修正为准备前必须读取 context，没有改写或重跑这个 run ID，也没有把离线通过改称真云通过。

修正后离线 H5 最长流程通过（2 passed、1 显式真实集成 skip）；9 个 Java 宿主测试及 APK 编译/签名通过。副本迁移测试保持真实旧 v1 任务与普通聊天数据，连续两次迁移幂等，注入失败可回滚 v2 schema/seed。清记忆后的可信新 session 可以重新练习，旧 session 和在途任务仍不能复活。

完整 R1–R16 / F1–F3 / AE1–AE9 证据与未完成门槛见 [验收记录](../docs/verification/2026-10-21-dictation-v2-acceptance.md)。首次真实 case 的失败结论保留。用户随后要求使用 KIMI，新 case `dictation-kimi-tc1-20261021-02` 已完成真实 KIMI + H5 软件闭环：7 次 KIMI 调用、28 次孩子交互、1 个任务、1 次完成，未循环，未使用豆包语音。**实体家庭电视验收仍未执行；本轮没有部署。**
