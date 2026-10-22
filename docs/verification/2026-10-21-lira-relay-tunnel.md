# LIRA WarmHouse 反向 SSH 隧道

2026-10-21，用户授权通过 WarmHouse 暴露 LIRA 本地服务，服务器端口 18191。

## 已部署

- 路径：外部 `http://152.136.21.84:18191` → SSH reverse forwarding → LIRA `127.0.0.1:8080`。
- LIRA：用户级 `lira-relay-tunnel.service`，enabled + active；orangepi 的 Linger=yes，无需登录开机启动。启动不依赖 Windows 电脑。
- SSH 心跳每 20 秒，连续 3 次失败退出；systemd 5 秒后重试；连接和远端绑定失败同样重试。
- 设备专用 Ed25519 私钥仅存在 LIRA `~/.ssh/lira_warmhouse_ed25519`，未复制用户电脑私钥。服务器主机公钥通过已有可信 WarmHouse SSH 连接读取并固定。
- WarmHouse 新增专用 lira-relay 用户，authorized_keys 与 sshd Match 仅允许远端转发、仅监听 `0.0.0.0:18191`；禁止 shell、PTY、代理、X11、Unix socket 转发。未修改 ubuntu 原有 18190 配置。
- 设备 unit 源码：`deploy/system/lira-relay-tunnel.service`；服务器配置 `/etc/ssh/sshd_config.d/lira-relay.conf`。
- 未修改或重启 LIRA 语音服务；操作前后主进程 PID 均为 103265。经典链路与教学扩展开关未改。

## 真实验证

- WarmHouse `0.0.0.0:18191` 监听已出现。
- 服务器访问 `http://127.0.0.1:18191/status` 返回 HTTP 200。
- Windows 外部网络路径访问 `http://152.136.21.84:18191/status` 返回 HTTP 200。
- 主动杀掉 SSH 隧道进程后，systemd 自动重启，NRestarts=1，公网 URL 再次返回 HTTP 200。
- systemd unit 校验及服务器 sshd 配置校验通过。
- 用户重新打开 ADB 后，成功重连 `192.168.3.25:5555`。电视使用设备上的 BusyBox wget 请求 `http://152.136.21.84:18191/status`，成功下载 1592 字节状态 JSON 到 `/data/local/tmp/lira-relay-status`，实际证明电视→WarmHouse→LIRA 通路已通。
- 真实完成回调及课级计数仍须等待 U15 事件服务部署验证；状态响应成功不等于任务回调验收。

## 使用与维护

电视后续回调基址为 `http://152.136.21.84:18191`；具体事件路由、认证仍须随 U15 服务落地后验证，当前未宣称接口已上线。

当前转发整个 8080 服务，没有额外 HTTPS 或代理访问控制；公网入口沿用应用自身认证。SSH 加密的是服务器到 LIRA 的隧道段，客户端到服务器仍为 HTTP。

在 LIRA 上：`systemctl --user status lira-relay-tunnel` 查看状态；`journalctl --user -u lira-relay-tunnel` 查看日志；`systemctl --user disable --now lira-relay-tunnel` 关闭并取消自启。无需重启语音服务。
