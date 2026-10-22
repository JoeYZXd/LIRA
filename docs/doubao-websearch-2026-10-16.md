# 豆包联网配置接入与复测

2026-10-16，用户已在 `/home/orangepi/.config/lira-doubao-realtime.env` 中添加 `enable_volc_websearch`、`volc_websearch_type`、`volc_websearch_api_key`，授权重启。程序读取三个参数，正确位置为顶层 `extension.dialog.extra`，与 `session` 同级；关闭时不传搜索扩展。搜索密钥不进入配置 repr 或状态接口。支持 `web_custom_api` 与 `web_global_api`。

已部署并重启。备份：`/home/orangepi/lira/deploy-backups/doubao-search-20261016-221408`。服务 active，PID 82578，NRestarts=0。状态确认 enabled=true、configured=true、type=web_custom_api；未输出或改写用户密钥。

25 项实时路径及对话实验室检查通过，新增验证配置读取、发送的扩展参数及密钥不进入公开状态；默认关闭搜索的原会话不添加扩展。仅对本次差异进行人工检查，保留此前未提交工作，没有提交或推送。

首次部署的失败记录（下文已定位并修复）：

- 使用相同搜索密钥直接调用 Custom 搜索 API，HTTP 200，存在 Result，无 ResponseMetadata.Error。说明基本搜索接口可访问。
- 通过真实实时语音连接，合成输入「请联网查询今天杭州的天气，并告诉我查询的日期」，会话完成但回复仍称无法联网，未提供天气。
- 隔离测试强化联网提示词后，仍回复无法获取实时天气；该试验提示词未写入生产代码。默认提示词另一次复测结果相同。

## 根因及修复

用户指出 `extension` 与 `session` 应为同级。此前将扩展嵌入 `session` 的实现和测试期望均错误，云端没有据此开启搜索。隔离测试只改变这个层级，保留原密钥、提示词、业务工具和查询音频，模型即回答杭州当天「小雨转多云、20～25℃」，与独立搜索 API 中中国天气网结果吻合。移除业务工具、将 extra 改为 JSON 字符串均未解决问题，未采用这些试验变更。

修复将 extension 放入 session.create 事件顶层；回归测试明确断言扩展不在 session 内、且顶层字段完整。25 项实时路径与对话实验室检查通过。继续使用内置搜索，没有添加自定义搜索工具；前一轮准备但未实现的工具方案测试已移除。官方参数参考：https://docs.volcengine.com/docs/DoubaoVoice/endtoend-realtime-voice-full-duplex-version?lang=zh 。

修正已部署，备份 `/home/orangepi/lira/deploy-backups/doubao-search-fix-20261016-225915`，服务 active，PID 84680，NRestarts=0。后台实际 HTTP 对话实验室复测完成，返回「今天杭州是小雨转多云，气温在20到26℃之间……」。最初验收脚本把预先查询到的 25℃ 写成硬编码断言，因此该脚本断言失败；会话本身 completed、无错误。该数字差异意味着不能把此轮回答的所有气象数值都宣称为独立核实，需结合回答来源与发布时间判断。修复验证的重点是相同原配置在正确请求层级下能够执行天气回答，而不是用提示词要求模型声称支持联网。

追加要求日期及来源的后台复测通过，回复「今天是2026年10月16日，杭州天气阴有阵雨转多云，最高气温26℃，最低气温19℃，资料来源是杭州市气象台、都市快报以及中国天气网」。来源名称为模型回答内容，当前协议适配器未返回可供逐条核对的搜索引用 URL，因此不等同于每个数字已经核实。自动测试记录已清理。
