# TypeSafe Jev 缓存语音测试部署记录

2026-10-16，用户明确授权部署后，更新 `/home/orangepi/lira` 下 7 个文件：TTS、控制台路由、新测试服务、CSS、新测试 JS、HTML 和 pyproject。未部署测试/脑图文件。修改前逐文件核对全部匹配仓库 HEAD 基线，新文件原先不存在。

- 源码包 SHA-256：`7f7e3643c784ca2cf6b80869c25da797b0a57b63b905e63934bf33e80eb09062`。
- 备份：`/home/orangepi/lira/deploy-backups/jev-cache-probe-20261016-114541`，目录 0700；`before.tar` 为 0600，包含旧文件和部署前 venv，另有哈希清单、原依赖列表及依赖安装记录。
- 板上原先缺少 httpx，已补装 `httpx 0.28.1` 及所需依赖。未修改密钥、systemd 单元及 drop-in、配置、模型、数据库、音频设备或音量/语速。
- 7 文件替换后哈希全部匹配；Python 编译与新服务模块导入通过。
- 服务 PID 57028 → 58187，active/running，NRestarts=0。
- `/status` 返回 200：standby/wake，隐私关闭，audio/OCR ready。
- 未鉴权 `/dev/api/jev-probe` 返回 401，新 JS 返回 200。板内只读使用既有控制台签名密钥生成短期会话完成部署核验，未读取口令、未输出 cookie/密钥或修改数据库。鉴权后测试端点返回 200，configured=true，11 个语料、5 个阶段、无测试会话，缓存未预热。
- 已核对真实 `/dev` 顶部测试区与 flex 静态资源。重启时间窗内 Traceback、ModuleNotFoundError、Audio output failed、环境文件加载失败计数为 0。
- 没有调用 TypeSafe 在线模型、没有触发预热或扬声器测试；密钥有效性、在线中文结果、设备首声/排空延迟仍待通过控制台测试，未把配置存在当成接口认证成功。

## 使用

刷新开发者控制台（资源若缓存可强制刷新），点击“预热固定语音”，选择阶段，再“开始文本测试”；输入期间查看分段判断，点击“输入完成并播报”后观察板子发声和时间统计。

## 回滚

备份目录附 `rollback-source.py`。需要回滚时执行：

```sh
ssh lira 'python3 /home/orangepi/lira/deploy-backups/jev-cache-probe-20261016-114541/rollback-source.py'
```

该命令尚未执行。脚本先检查当前 7 文件仍匹配部署哈希，拒绝覆盖部署后的新改动；停止服务、恢复旧源码并移除本次新增的两个模块后重启。保留配置、用户数据和补装依赖。完整旧 venv 另在私有备份中，需恢复依赖时另行核对后操作；源码回滚不自动覆盖它。
