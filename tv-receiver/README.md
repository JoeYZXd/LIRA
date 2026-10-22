# LIRA 客厅屏幕（Android 6.0 验证版）

原生大字和图片接收应用，包名 `org.lira.receiver`，最低 API 23。
当前针对 MiTV4 / Android 6.0.1 验证。无需 WebView，无 APK 外部依赖。
不控制电源，不发送唤醒指令。

## 构建与首次安装

需要 JDK 17、Android SDK platform 33、build-tools 35.0.0 和 platform-tools。
设置 `ANDROID_HOME` 指向 SDK 根目录（兼容本机误设到 platform-tools 的情况）。
构建脚本只清理自身 `build/classes` 和 `build/dex`。

```powershell
python tv-receiver/build.py
adb -s 192.168.3.25:5555 install -r tv-receiver/build/lira-tv.apk
adb -s 192.168.3.25:5555 shell am start -n org.lira.receiver/.Screen
```

生成的 APK、配对配置 `build/client.json`、本地签名密钥都在被忽略的
`build/` 下。保留签名密钥以便覆盖升级。不要分享 APK 或配对配置：本验证版
每个本地构建目录生成一个配对密钥，嵌入 APK；不适合公开分发。
生产配对、TLS 和密钥轮换不在本次硬件验证范围。

## 发送内容

```powershell
python tv-receiver/client.py status
python tv-receiver/client.py --approved text "你好，我是 LIRA。"
python tv-receiver/client.py --approved image path/to/image.png --caption "图片说明"
python tv-receiver/client.py --approved --wait 150 text "电视上线后显示"
```

`--approved` 是此独立命令行工具的调用方许可声明，不是语音询问实现。
正式豆包语音路径已接入本地确认状态机，见 `../docs/tv-voice-display-2026-10-18.md`。
家人在看电视时，正式 LIRA 集成会先完成语音确认；不要用命令行绕过家庭许可。
测试安装和展示由用户本轮请求授权。

客户端直接连接电视 40100 端口，不经过代理。应用运行时通过 `/status` 查询
屏幕交互状态、前台状态、服务实例和最近渲染 ID。`/show` 只接受携带配对密钥、
明确 `approved=true` 且屏幕已开启的请求；图片为 Base64 编码，HTTP 正文上限
4 MiB，文本最多 8000 字符，图片说明最多 160 字符。图片按屏幕分辨率降采样。
本地 HTTP 未加密，只用于可信家庭局域网，不开放公网端口。

若接收端不可达，发送端在已获展示许可的前提下调用电视原有 6095 接口：
`/controller?action=startapp&type=packagename&packagename=org.lira.receiver`。
该接口在本机可能不返回响应但实际执行启动，因此启动结果只以接收端上线为准。
最多每 10 秒尝试一次，默认恢复预算 30 秒，可用 `--wait` 扩大。
预算控制重试窗口；正在执行的单次网络请求仍最多等待 5 秒。
状态查询本身不会拉起应用，也不把离线当成待机。

POST 返回 accepted 不代表展示成功；发送工具继续等待匹配 ID 且前台可见的回报。
这确认 Android 页面已提交布局，不能代替物理面板观测。返回键退出后不自动重放同一
请求 ID，不因展示确认超时而再次拉起界面。新展示需重新取得许可并使用新 ID。
关屏时退出 Activity，后台接收端尽可能保留；BOOT_COMPLETED 只启动服务，不抢占画面。
本固件重启会将第三方应用置于 stopped 状态，单靠开机广播未通过验证，故提供网络拉起路径。

## 验证与限制

```powershell
python -m unittest discover -s tv-receiver -p test_client.py
python tv-receiver/verify_live.py
```

`verify_live.py` 对已开机的配对电视运行拒绝请求与慢速请求测试，不改变当前内容。
Android UI 以真机截图和局域网回报验证；没有 Android 模拟器单元测试环境。
本模块独立于现有 Python 设备主程序，所以不运行与本次修改无关的语音测试套件。

已验证：大字、图片、错误密钥、缺少许可、坏图片、空文本、超大请求、慢速头部
超时；强制停止应用后，局域网发送可重新拉起并展示。截图和运行结果在 `build/`。
完整重启结果见 `build/reboot-result.json` 及同目录验证报告。

后续生产集成还需：展示会话所有权、展示结束通知、遥控器接管检测、
故障可观测性、长期待机恢复与网络变化。接收端不会自动关电视，也不保证恢复此前
任意播放器／HDMI 输入；返回键遵循 Android 任务栈行为。

卸载：`adb -s 192.168.3.25:5555 uninstall org.lira.receiver`。


## 电视扬声器播放（协议 0.3）

接收端新增 `pcm_audio_v1`。设备的豆包实时语音可配置
`LIRA_REALTIME_AUDIO_OUTPUT=tv`，使用现有配对配置和真实云端返回的
24kHz、单声道、16位小端 PCM；默认 `device` 保持原设备扬声器输出。
该选项只作用于豆包实时会话，经典链路不读此配置。

电视接收端必须已开启且处于前台。音频不会拉起应用、唤醒电视、调整全局音量，
也不会在失败后改用设备扬声器。电视静音、音量为零、焦点被拒绝或扬声器路由
不成立时返回失败。正常使用前先进入 LIRA 电视接收界面；离线失败不等于待机。

鉴权与现有展示一致。`POST /audio/start` 接收唯一 `id`、`approved=true`、
`sample_rate=24000`、`channels=1`、`format=s16le`；`/audio/chunk` 接收
顺序 `sequence` 和 `pcm_base64`（每块最多24000字节），`/audio/end` 校验
`total_bytes`，`/audio/stop` 只停止匹配的 ID。最多120秒，内存队列最多4块，
没有音频文件持久化或任意 URL 拉取。网络回执不明时不重播数据，只对明确的
队列满拒绝重试。音频结束后轮询 `/status.audio`，只有 `completed` 且实际
播放帧达到写入帧才视为完成，HTTP 接收成功不等于播完。

`audio` 状态包含流 ID、接收字节、写入/播放帧、音频会话 ID、实际路由类型、
音量和静音状态；`routed_device_type=2` 表示 Android 内置扬声器。
使用逐轨首选设备，不改电视全局输出。退到后台、关屏、失去音频焦点或本次
会话取消时停止并释放 AudioTrack；迟到的焦点回调不能停止新的音频流。

## 语文认读与默写 v2（本地实现，尚未家庭部署）

能力 `dictation_task_v2` 使用独立严格任务合同；旧 v1 保留读取与测试兼容。接收前完成 PCM 资源校验，冻结 task/hash 后先等待孩子确认。二会字组词由应用自适应大字整词分页；四会词语按配置实际播放后等待孩子确认，超时仅提醒。重播、暂停、恢复和确认通过 run/revision/item 绑定；暂停停止播放器并持久化后才发实际回执。问题交流需要设备回答排空及真实继续确认，不能用 HTTP accepted 冒充已暂停或口头声称已完成。

任务/事件检查点和 durable outbox 支持重复投递与 ACK 丢失；离线租约失效、重启、失焦先暂停，恢复须新心跳和确认。日志显示当前动作及播报次数，四会默写状态与电视日志不显示答案。管理端 `/dev/dictation` 可查看完整任务、版本、选词理由、事件及家长照片/错词核对。

本地 H5 仅替代终端边界；浏览器 AudioContext 完成回调不证明实体电视声学。9 个 Java 测试入口和 APK 编译/签名本地通过；本轮未安装 APK 或操作家庭电视。首次真实 case 因过严澄清断言停止，失败记录保留；用户随后要求使用 KIMI，新建单次 KIMI + H5 case 已完整通过（7 次模型调用、28 次交互、1 个任务/1 次完成），完成即退出。未使用豆包语音，播报来自本地 TTS。详见 [本轮验收记录](../docs/verification/2026-10-21-dictation-v2-acceptance.md)。


2026-10-21 实测修订：新增能力 `dictation_recognition_words_v1`，二会字认读项支持覆盖目标字的完整组词，保留单字旧快照读取兼容；旧接收端缺少组词能力时明确要求升级。短词 PCM 在 LIRA 端做有上限增益，TaskAudioPlayer 保留 `LiraTaskAudio` 元数据日志（帧数、路由、音量、失败原因）。
