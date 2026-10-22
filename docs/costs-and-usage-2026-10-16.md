# 火山账户余额与本机每日用量

已部署。入口：开发者控制台左侧「费用与用量」。显示账户可用余额、现金余额、欠费金额和更新时间，低余额阈值默认 50 元，可在当前浏览器修改。充值链接打开火山控制台，由用户手动操作。

## 配置与刷新

板子凭据位于 `~/.config/lira-volcengine-openapi.env`，变量为 `VOLCENGINE_ACCESS_KEY_ID`、`VOLCENGINE_SECRET_ACCESS_KEY`，文件权限 600。用户服务新增 `~/.config/systemd/user/lira.service.d/70-costs.conf` 加载该文件。程序只调用 QueryBalanceAcct，没有开通、充值、购买等写操作。

依据官方 V4 HMAC-SHA256 协议签名，查询费用中心 `QueryBalanceAcct`（版本 2022-01-01，billing 服务，cn-north-1）。仅在磁贴打开时读取，余额正常缓存 5 分钟，手动刷新与失败重试最短 1 分钟，避免重复查询。失败不伪装成零余额；保留上次成功数据并注明过期。InvalidAccessKey 和权限错误使用固定、脱敏提示，不输出原始远端报错或凭据。

官方接口：https://docs.volcengine.com/docs/BillingCenter/QueryBalanceAcct-Queryuseraccountbalanceinformation?lang=zh

官方签名实现：https://github.com/volcengine/volc-sdk-python/blob/master/volcengine/auth/SignerV4.py

## 用量口径

- 记录本机正式豆包路径与后台「豆包对话」实验室；两类分别统计，也显示今日合计。
- 成功连接计会话；输入转写完成事件计交互轮次；成功发送的 PCM 计上传音频；收到并解码的 PCM 计返回音频，包括取消后尚未播放的返回片段。
- 另计全零上传音频，主要反映保活和播报保护。它不是 VAD，不等于真实说话时长，环境声通常属于非零音频。
- 按北京时间跨天归档，显示最近 7 天，保留最近 90 天。仅存聚合数字与日期，不存对话、身份、密钥。
- 使用原设备 SQLite meta 中独立键 `doubao_daily_usage_v1`，每约 10 秒及会话开始/结束落盘。正常重启保留，意外断电可能丢失最后约 10 秒。
- 从部署起计算，此前历史无法补算。单独 CLI 探针、其他设备、火山体验台不在本机统计范围内。
- 这是本机近似用量，不是官方账单、Token 数或费用预测；未将会话轮数冒充内置搜索次数。

## 验证与部署

用户修正 IAM 凭据后，真实余额查询成功。143 项相关 Python 检查通过，覆盖跨北京时间午夜、SQLite 关闭重开、缓存、失效凭据、旧余额保留、鉴权与实际协议收发计数。7 项浏览器检查通过，包括手机与桌面视口、低余额阈值、表格滚动和 App 中心布局。截图保存在本次会话可视化目录的 `costs-390.png`、`costs-1440.png`。

真实后台验收：1 个测试会话、1 轮交互，上传 5.42 秒，其中全零 5.04 秒，返回 3.41 秒；会话结束后正常重启，计数全部保留。真实余额重新加载成功，页面与 JS 可访问。服务 active，PID 87800，NRestarts=0。验收属于后台测试用量，未从统计中抹除；临时对话文字记录已清理。

备份与带 systemd 配置恢复的回滚脚本：`/home/orangepi/lira/deploy-backups/doubao-costs-20261016-235732`。部署 9 个应用文件及一个 user service drop-in，没有改写用户凭据或清理历史数据。

本次仅针对相关文件及新统计模块进行人工检查；工作区包含先前未提交工作，未整理、提交或推送混合改动。检查了统计口径、签名与缓存、密钥隔离、鉴权、失败旧数据、资源关闭及原语音路径影响。没有执行整分支审查。
