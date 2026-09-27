# LIRA 部署与运维手册

> 本文件在 U8（子女管理后台）阶段创建，承载后台服务的部署与安全说明；
> U10（硬件集成）阶段将补充镜像烧录、overlay 编译、systemd、冒烟清单与实机参数记录表。

---

## 1. 子女管理后台（U8）

### 1.1 已知安全限制（Phase 1 范围内明示，不做公网暴露）

- **LAN 明文 HTTP**：后台仅监听局域网 HTTP（无 TLS）。会话 cookie 已启用
  `HttpOnly + SameSite=Strict`，但传输层为明文，同网段的被动嗅探可读取
  cookie / 设备 token / 学习码值。Phase 1 部署边界为**家庭局域网**，不配置
  端口转发、不做公网暴露；若需远程访问，应在网络层（VPN/WireGuard）解决，
  而非直接暴露 8000 端口。
- **设备新 token 明文一次性展示**：注册/吊销重发生成的新 token 仅在
  页面展示一次，后台库只存 SHA-256 哈希；请设备侧立即持久化。

### 1.1.1 已知残留问题（评审接受，低危低概率，Phase 2 候选）

以下为分支代码评审（安全 + 正确性双审）后确认接受、未在本阶段修复的低危项：

- **登录/登出接口无 CSRF token**：其余全部变更类管理接口均校验 CSRF，
  仅 `/login`、`/logout` 未加；`SameSite=Strict` 已使经典跨站登录/登出 CSRF 基本失效，
  影响仅限登出骚扰，记录以保持一致性。
- **新 token 经 URL query 一次性下发**（`/?token_once=...`）：注册/重发页用查询串
  传一次性 token，可能残留于浏览器历史与访问日志；库内仅存哈希。
- **设备端 SQLite 未 chmod 0600**：设备库（含口令 scrypt 哈希；device token
  按开发期预置约定存放于 `device/config.yaml`）权限继承进程 umask；
  板上单用户环境下影响有限。
- **sync 循环无重连边界**：`SyncClient.run_forever` 内处理器抛非协议异常会终止
  同步会话直至进程重启（仅受信任后台连接可触发）。
- **学习请求可滞留 sent 态**：WS 在"领取学习请求→下发"之间断开时，该请求无超时重发
  路径，后台轮询显示 sent 直至人工处理。
- **登录锁定过期后计数器在过期瞬间才复位**：锁定过期后的首次失败从 1 重新计数
  （已修复为正确语义）；攻击者仍可蓄意刷失败使管理员持续处于锁定（可用性型 DoS，
  设计固有）。
- **熔断器 HALF_OPEN 无单飞限制**：并发恢复期可能放行多个试探请求；CLOSED 期
  迟到的失败会顺延冷却时钟。仅影响恢复速度，不影响 fail-safe 方向。

### 1.2 启动步骤

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 首次启动（库文件 data/lira-backend.db 自动创建，权限 0600，WAL 模式）
.venv/bin/python -m app          # 等价于 uvicorn app.main:create_app，0.0.0.0:8000
```

后台启动后访问 `http://<后台机IP>:8000/`。

### 1.3 管理员首启引导

- 数据库中不存在管理员时，**一切页面请求强制重定向到 `/setup`**；
  系统不存在空密码/默认密码状态。
- `/setup` 要求：用户名 ≥ 2 字符，密码 ≥ 8 位；不满足返回 400 重新填写。
- 管理员创建后 `/setup` 永久失效（再访问重定向 `/login`）。
- 登录限速：连续 5 次失败锁定 5 分钟；改密码会使全体会话立即失效。

### 1.4 设备接入 / epoch 迁移（开发期预置，无运行时配对流程）

> 2026-09-27 决议：取消设备配对流程。token 与 epoch 由开发期直接配置；
> 设备仍拒绝一切陌生 epoch 快照（回放保护不变）。

1. **首次接入**：后台「注册设备」→ 页面一次性展示 `device_token` →
   将 token 写入设备配置 `device/config.yaml` 的 `sync.device_token`
   （或环境变量 `LIRA_SYNC_DEVICE_TOKEN`）→ 设备用 token 经 WS 首帧
   `hello` 或 HTTP `X-Device-Token` 完成鉴权。设备本地库无 epoch 时
   （bootstrap）接受收到的首个快照并落库 (epoch, version)。
2. **epoch 迁移（后台库重建 / 恢复后）**：后台库重建即自动换新 epoch
   （version 归零），旧 token 同样失效，需在后台重新注册设备并把新 token
   写入设备配置；然后在设备端执行同步状态重置并重启：
   ```bash
   python -m lira.sync reset-sync data/device.db
   ```
   该命令仅清除 (epoch, version) 同步元数据，保留本地配置与已学红外码；
   重启后设备按 bootstrap 语义接受新 epoch 快照。

### 1.5 运维要点

- SQLite：WAL + `synchronous=FULL`，单文件库 `backend/data/lira-backend.db`
  （创建即 `chmod 0600`）；备份直接冷拷贝该文件即可（含 -wal/-shm 时先停服）。
- 设备通信面：HTTP `GET /api/device/snapshot`（离线兜底）、
  `POST /api/device/ack`、WS `/ws/device`（首帧必须 hello，在线变更 0.5s
  内推送，60s 心跳拉取兜底）。
- 红外学习：后台 POST `/admin/learn`（落库即返回）→ 设备 WS 收
  `learn_start` → 设备回传码值 → 入库且 version+1 → 快照推送；
  状态经 `GET /admin/learn/{learn_id}` 轮询。

---

## 2. 硬件上板（U10，待补充）

镜像烧录、版本锁定检查、`deploy/dt-overlays/gpio-ir-overlay.dts` 编译、
服务 systemd 化、冒烟清单与实机参数记录表 —— 待 U10 单元补充。
