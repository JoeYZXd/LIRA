# 科目导航与卡片内操作验证

本轮根据设备试用反馈，移除教学管理标题中单独写入的“+”，新增语文、数学、英语一级导航；教材与原文、家长记录和能力状态在卡片内打开。同源嵌入视图复用现有编辑器，隔离与控制台重复的元素 ID 和样式，不改写后端鉴权或数据 API。

数学与英语新增场景只展示用途、依赖和未开放状态。语文默写、背诵扩展继续关闭。原计划 HTML/Markdown 同步至 r6，新增 N20、R21–R27；跨科目技术方案仍待规划，不继承旧版 implementation-ready 声明。

验证：

- 卡片导航与工作区浏览器回归：6 passed / 12.70s（390px、1440px）。
- 原教学编辑器和课本核对浏览器回归：5 passed / 34.15s；使用独立进程避免同步 Playwright 生命周期冲突。
- 四个改动 JS 文件通过 Node 语法检查。
- 真实设备浏览器验证：无多余“+”；课本、原文在卡片内；顶层 URL 不变；未保存编辑取消离开后保留；数学打印、英语每日单词显示规划；实际能力状态仍不可调用；手机无横向溢出；无 JS 错误。未保存测试内容，未修改家庭数据。
- 实际部署仅 9 个 UI 文件，SHA256 校验通过，范围外 122 个代码/页面/静态文件未变；PID 50644 保持，未重启服务，`LIRA_REALTIME_CAPABILITIES=false`。
- 备份：`/home/orangepi/lira/deploy-backups/teaching-hub-20261019-223113`。需要回退时依据回执恢复原文件、移除原本不存在的新文件；本次无数据库迁移或数据回退需求。

机器回执：[UI 部署](2026-10-19-teaching-hub-deployment.json)、[真实设备验证](2026-10-19-teaching-hub-device-smoke.json)。界面截图仅含科目首页，位于 `tmp/teaching-deploy/teaching-hub-device-desktop.png` 和 `teaching-hub-device-mobile.png`，不包含家庭资料。

使用设备控制台 `/dev` 的教学管理 App。旧独立编辑 URL 为兼容入口保留；本次卡片导航不使用顶层跳转。
