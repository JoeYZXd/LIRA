# U6 电视清屏与协议能力执行证据

本地实现通过，未安装电视、部署或提交；真实硬件验收尚未通过，不能声称新版clear实机可用。

接收器协议0.2追加version/capabilities，sender/client通过实际status探测clear_supported。clear持expected_content_id，UI线程确认当前内容与已渲染ID匹配后清除、回执cleared_id；不会启动Activity、唤醒或抢前台。旧版/关闭/退出/替换冲突明确拒绝，不用空白show代替。重复clear幂等，旧清除迟到确认不覆盖新展示所有权。

实时扩展工具probe_tv_receiver/clear_tv_content只随新能力registry启用，未加进旧TV_TOOLS。保留逐次内容同意；cancel只取消待展示，clear另行执行。新能力开启且换人时撤销本地同意及展示归属，不自动清除实际屏幕。工具成功status=ok，display_status/cleared_id保留领域结果。

Red：sender缺clear，2failed/4passed。复用现有TV及实际registry/executor测试，覆盖版本不支持、关闭/退出、替换、幂等、无同意不show、取消区别、换人及迟到确认竞态。

Root权威验证：

```text
.venv/bin/python -m pytest tests/test_tv_voice.py tests/test_tv_display.py tests/test_conversation_paths.py -q
79 passed in10.22s
python -m unittest discover -s tv-receiver
6 passed
```

Worker本地APK编译及签名验证通过，AndroidManifest版本0.2/code2。产物tv-receiver/build/lira-tv.apk，SHA256 fa2a948e87945725423b97551d1ce7e636a80321dec697eaae8063b28e4088b7。

改动：device/lira/tv_display.py、tv_voice.py、conversation_paths.py；tv-receiver/src/ReceiverService.java、Screen.java、AndroidManifest.xml、client.py、test_client.py、VERIFICATION.md；device/tests/test_tv_voice.py、test_tv_display.py、test_conversation_paths.py。保留原有用户在途改动，不能将整个文件原始diff当作本单元新增。

后续实机门槛：新版安装配对后验证匹配clear、替换冲突、重复请求、关闭/退出不唤醒，记录接收器制品指纹。原计划及经典链路未扩展。
