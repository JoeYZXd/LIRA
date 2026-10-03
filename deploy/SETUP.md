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

## 2. 硬件上板（U10 runbook）

> 执行原则：**里程碑推进**（M0→M7），每个里程碑末尾把实测值回填 2.10 记录表；
> 异常即停排查，不带病前进。物理按键无（2026-09-27 决议，无 button_gpio）。

### 2.0 前置状态与纪律（2026-10-01）

**到货状态**：主板（5B 8GB，板载 eMMC + WiFi6/BT）、OV13855 MIPI 摄像头、USB 麦阵
（FY-SP003U）、USB 有源音箱（3.5mm AUX）、LC 红外收发模块（转备用）、杜邦线、
PAM8403 裸板（闲置）—— 已到；BroadLink RM4 Mini 在途（M5）。
**音频链路（2026-10-03 定案）**：播放走板载 ES8388 → 3.5mm → USB 有源音箱（M6 ✅，
见 2.7），PAM8403 补购取消；蓝牙小爱音箱（2.3）转开发期备用。
**红外（2026-10-03 改道）**：gpio-ir 方案废弃（内核无 RC_CORE，见 2.6），改 BroadLink RM4 Mini。

**版本锁定纪律**（计划风险表头号项）：
- 镜像烧录后锁定：不跑 `apt upgrade` 升内核、不追新固件；
- RKNN 三件套（NPU 内核驱动 / librknnrt / rknn-toolkit2）以**实测驱动版本为锚**，
  runtime 与转换器向其同大版本对齐，锁定后不再变更；
- 所有版本号实测后回填 2.10 记录表。

**装配纪律**：散热壳带导热胶属一次性装配（装后不宜再拆），**总装放最后**——
M0 烧录需按住板上 MASKROM 按键（救砖同），MIPI 排线与 GPIO 杜邦线需裸板插接。
顺序：裸板完成 M0–M5 点亮验证 → 装壳（风扇线一并接）→ M7 软件固化。
Phase 1 摄像头接 **CAM1**（30pin 4-lane，镜像默认使能通路）；CAM2 默认未使能勿用；
第二路摄像头为 **CAM3**（官方双摄组合 Cam1+Cam3，Phase 3 预留，需求 Scope Boundaries）。

### 2.1 M0：烧录、首启与 SSH（只需板子 + 电源 + 网线）

**镜像**：[Orange Pi 官方 5B 支持页](http://www.orangepi.org/html/hardWare/computerAndMicrocontrollers/service-and-support/Orange-Pi-5B.html)
→ **`debian_bookworm_server_linux6.1.99`**（已选，2026-10-01）。
排除理由：bullseye 太老（PipeWire/蓝牙栈、Python 3.9）；trixie 太新无增益（内核相同）；
desktop 镜像白占内存与 eMMC（设备无头）。6.1 BSP 内核的 rknpu 驱动为 0.9.8+，
满足"NPU 驱动 ≥0.9.8"约束（5.10 老镜像为 0.9.6/0.9.7，勿选）。

**烧录**（二选一）：
- **TF bootstrap（推荐；需任意 ≥16GB TF 卡 + 读卡器）**：balenaEtcher 把镜像写入 TF →
  TF 卡启动进系统 → `lsblk -d -o NAME,SIZE,MODEL` 按容量识别 eMMC 设备号（**写错盘 = 毁盘**，
  当前根分区所在盘用 `findmnt /` 排除）→
  `xz -dc <镜像>.xz | sudo dd of=/dev/mmcblkX bs=4M status=progress && sync` →
  关机拔卡，从 eMMC 重启。这张 TF 卡从此留作救援卡（BOM 可选项 ✓）。
- **Windows 线刷**：RKDevTool + Rockchip 驱动进 Maskrom/Loader 烧录；Loader 文件用镜像包内附；
  **镜像路径不能含中文**（[官方 Wiki 线刷方法](http://www.orangepi.cn/orangepiwiki/index.php/使用_RKDevTool_烧录_Linux_镜像到TF_卡中的方法)、
  [orangepi.net 5B 安装指南](https://orangepi.net/guide-to-install-operation-system-on-orange-pi-5b.html)）。

**首启**：网线入家庭局域网（DHCP）→ 默认账密 `orangepi/orangepi`，首登强制改密 →
配置 SSH 密钥并禁密码登录 → `df -h /` 确认根分区已自动扩容到 eMMC 实际容量。

**锁定**：记录 `uname -a`；`sudo apt-mark hold` 内核相关包（或纪律上不跑 upgrade）。

### 2.2 M1：RKNN 三件套核对（跑任何模型之前）

| 组件 | 查法 | 实测值 |
|------|------|--------|
| NPU 内核驱动 | `sudo cat /sys/kernel/debug/rknpu/version`（debugfs 未挂载先 `sudo mount -t debugfs none /sys/kernel/debug`） | 待填 |
| librknnrt 运行时 | 装好后 `strings /usr/lib/librknnrt.so \| grep -i version` | 待填 |
| x86 rknn-toolkit2 | `pip show rknn-toolkit2`（Windows/WSL 侧） | 待填 |

- librknnrt 从 [rknn-toolkit2 releases](https://github.com/airockchip/rknn-toolkit2) 同系列
  runtime 包取，装入 `/usr/lib/`；
- **x86 并行任务**：OCR 模型转换环境同机搭建（paddle2onnx → rknn-toolkit2 转 rk3588s，
  产出 det/rec 两个 `.rknn`）。det 绑 NPU core0、rec 绑 core1（计划 U10）。
  转换器与 librknnrt 同版本对齐。
- **x86 转换环境实况（2026-10-03，已完成）**：WSL Ubuntu 24.04 + **py3.11 venv `~/rknn311`**
  （toolkit2 2.3.0 cp311 wheel 仓库自带 + **onnx==1.15.0 钉版** + opencv-headless，
  pip 走 TUNA）。两个坑入册：① onnx≥1.16 删了 `onnx.mapping` 且 toolkit2 优化器在
  rec 图上**原生 abort**（SIGABRT 无报错），而 py3.12 装不上 onnx≤1.15 → **必须 py3.11**；
  ② WSL apt 曾被残废代理 `/etc/apt/apt.conf.d/proxy.conf`（指向不可达的 192.168.101.24）
  卡死 → 移为 `.bak` 并切 TUNA 源。模型源用 `models/download_models.py`（rknn_model_zoo
  网盘 CDN 的 ONNX + ppocr_keys_v1.txt 字典）；det i8 校准集用 rknn_model_zoo 自带
  `datasets/PPOCR/imgs/dataset_20.txt`。产物：`models/ppocrv4_det.rknn`（2.6MB i8）、
  `models/ppocrv4_rec.rknn`（7.1MB fp16）。

### 2.3 M2：音频出声（蓝牙临时喇叭，小爱音箱 A2DP）

米家 App 打开音箱的**蓝牙音箱模式**（或语音"打开蓝牙"）；注意部分型号断电重启退出该模式。

板上：
```bash
sudo apt install bluez pipewire pipewire-alsa pipewire-pulse wireplumber libspa-0.2-bluetooth
sudo rfkill unblock bluetooth && sudo systemctl enable --now bluetooth
bluetoothctl   # scan on → pair <MAC> → trust <MAC> → connect <MAC>
wpctl status   # 确认小爱音箱出现在 sinks
aplay /usr/share/sounds/alsa/Front_Center.wav   # 出声即通过
```

- 延迟 100~300ms 对 TTS 无影响；软音量经 PipeWire 生效（R21"大声点/小声点"路径不变）。
- 代码零改动：播放器为注入式（`device/lira/audio/tts.py`），sounddevice → PortAudio →
  ALSA → PipeWire → A2DP。
- **板上排查**：`hciconfig`/`dmesg | grep -iE 'rtw|bluetooth'` 看 hci0 与固件加载
  （板载模组多为 Realtek 8852 系，固件在 `linux-firmware`/`firmware-realtek`；
  若固件折腾不出，**USB 蓝牙适配器兜底**）；`python -c "import sounddevice as sd; print(sd.query_devices())"`
  确认 PortAudio 侧可见该 sink。
- 朗读期间小爱同学通常停用（蓝牙模式下），声学干扰小；仅作开发期方案。

### 2.4 M3：MIPI 摄像头（实到 OV13855，CAM1，2026-10-02 验证）

**断电接排线**（金手指朝板、蓝膜朝上，两端同面，插到底压紧锁扣）→ 上电。

**① 使能 overlay**（Linux server 镜像默认不使能摄像头节点——dmesg 无任何 sensor 探测
且无 video 节点即此因，不是接线问题）。`/boot/orangepiEnv.txt` 加一行
（配合已有 `overlay_prefix=rk3588`）：

```
overlays=ov13855-c1
```

（`-c1/-c2/-c3` 对应 CAM1/2/3 座位；`opi5max/pro/ultra` 等前缀是别的板型的，5B 用通用组。）

**② 重启后验证**：

```bash
dmesg | grep -i ov13855     # 期望: ov13855 7-0036: Detected ... sensor
v4l2-ctl --list-devices     # rkcif(video0-10, media0) + rkisp_mainpath(video11-17, media1)
media-ctl -p -d /dev/media0 # ov13855 7-0036 → csi2-dphy0 → mipi-csi2 → stream_cif_mipi_id0
```

**③ 抓帧（CIF 裸 RAW；Bayer 未经 ISP，偏绿/偏暗属正常）**：

```bash
v4l2-ctl -d /dev/video0 --set-fmt-video=width=2688,height=3136,pixelformat=BG10 \
  --stream-mmap --stream-count=1 --stream-to=/tmp/frame.raw
ls -lh /tmp/frame.raw       # ≈16MB 即成功（注意：请求 4224 宽会被驱动静默钳到 2688）
```

ISP 处理路径（media1 → NV12，rkcif→sditf→rkisp）留给真实 HAL `camera_rkisp.py` 实现时打通。
`v4l-utils` 需 `apt install v4l-utils`。

### 2.5 M4：USB 麦阵

```bash
arecord -l                            # USB 声卡在列
arecord -D plughw:<卡号>,0 -f S16_LE -r 16000 -c 2 -d 10 /tmp/test.wav   # 录 10s
aplay /tmp/test.wav                   # 经蓝牙喇叭回放验证全链路
```

`sounddevice` 设备序号记录入 2.10（板上音频配置将按它固定）。

### 2.6 M5：红外家电控制（2026-10-03 改道：BroadLink RM4 Mini）

**改道背景**（gpio-ir 方案在官方镜像上不可行的完整证据链）：
overlay 应用成功（`lira-ir-*` 节点进入活设备树），但官方 Debian 内核 6.1.99
**未启用 RC_CORE**（`# CONFIG_RC_CORE is not set`，IR 协议栈整层缺失），无驱动可 probe、
无 lirc 设备；apt 亦无版本匹配 headers，补编模块不可行。经决策（2026-10-03）弃
gpio-ir/ir-ctl 路线，改用 **BroadLink RM4 Mini**（约 40~90 元）：USB 供电、WiFi 入家庭局域网，
`python-broadlink` 走**局域网 UDP 本地协议（不经云）**。已购 LC 红外模块与杜邦线转备用
（备选 ESP32 自研桥方案可复用；`deploy/dt-overlays/gpio-ir-overlay.dts` 留档）。
遗留物清理：`/boot/orangepiEnv.txt` 的 `overlays=` 移除 `gpio-ir`（保留 `ov13855-c1`）。

**到货后步骤**：
1. RM4 Mini USB 供电 → 官方 App 完成配网入家庭 WiFi → 路由器上**固定其 DHCP IP**
2. 板上 `pip install python-broadlink` → 发现设备，记录 IP / MAC / 设备密钥
3. 学习：后台发起 → 设备调用 broadlink 学习 API → 码值存本地库
   （U6 的 store/意图链路不变，仅传输层换成 `lira/hal/board/ir_broadlink.py`）
4. 验证：学习电视"音量+" → 语音/后台发送 → 电视有反应 = M5 ✅

**韧性注记**：R9 的"断网"指外网——RM4 走局域网，外网断不影响；家庭 AP 故障则 IR 失效，
作为已知限制随本节记录（最终形态若不可接受，备选 ESP32+LC 模块有线桥）。

### 2.7 M6：本地喇叭（2026-10-03 完成）

定案 = 原"方案 B"零成本落地：**USB 供电有源音箱**（自带功放），3.5mm 公对公线
直连板载 ES8388（card 2，线路输出）。PAM8403 功放板方案取消（裸板留档不用）。

- 接线：3.5mm 公对公 → 音箱 AUX IN；音箱 USB 供电（板载 USB 口或独立充电头均可）。
- 底噪注意：共地环路"滋滋"声 → 音箱改插独立充电头即消。
- 软音量路径（R21"大声点/小声点"）：PipeWire/ALSA 层实现；M7 固化设备号
  （capture = FY-SP003U card 3，playback = ES8388 card 2）。
- 蓝牙小爱音箱（2.3）转开发期备用/对比。验证通过 2026-10-03。

### 2.8 M7：软件上板与 provisioning

```bash
# 依赖与安装（具体 extras 名以 device/pyproject.toml 为准）
sudo apt install python3-venv portaudio19-dev libsndfile1
cd ~/lira/device && python3 -m venv .venv
.venv/bin/pip install -e .

# 真实 HAL 切换（device/config.yaml）
hal:
  backend: board        # mock | board

# provisioning（开发期预置，无配对流程）
# 后台「注册设备」→ 一次性 token → 写入 device/config.yaml：
sync:
  ws_url: ws://<后台机IP>:8000/ws/device
  device_token: <一次性 token>     # 或环境变量 LIRA_SYNC_DEVICE_TOKEN
```

- 后台库重建/epoch 迁移：`python -m lira.sync reset-sync data/device.db`（SETUP.md 1.4）。
- systemd 化 + 存量纪律：`deploy/system/`（log2ram、journald `Storage=volatile`、
  fstab `noatime`、无 swap、thermal governor——R18 介质寿命）；`ExecStart` 以
  `python -m lira.main` 为准，实机核对后固化 unit 文件。
- 重启验证：开机自动进入待机，无人工干预。

### 2.9 冒烟清单（全部里程碑后执行）

1. 唤醒词唤醒（提示音 + "我在听"）→ 拍报纸 → 朗读；
2. "打开空调"（已学习码）→ 红外发射 → 家电响应；
3. 高危设备（取暖器）→ 语音二次确认 → 确认后才执行；模糊应答/超时不执行（fail-closed）；
4. **拔网线** → 再拍药品说明书 → 朗读 OCR 原文 + 免责提示（不静默、不拒读）；
5. 局域网 Web UI 登录 → 隐私模式开/关 → 设备语音/状态同步正确；
6. 麦阵拾音 + 蓝牙喇叭出声全链路（若仍在开发期）。

第 6 项仅开发期；**冒烟清单的最终配置必须为本地喇叭**（上电即出声）。

### 2.10 实机参数记录表（每里程碑回填）

| 里程碑 | 参数 | 实测值 | 日期 |
|--------|------|--------|------|
| M0 | 镜像文件名 / 内核（`uname -a`） | debian_bookworm_server_linux6.1.99；6.1.99-rockchip-rk3588 #1.1.0（2026-08-20 构建）；根分区在 eMMC（mmcblk0p1，57G 已扩容） | 2026-10-02 |
| M1 | NPU 驱动 / librknnrt / rknn-toolkit2 版本 | 驱动 v0.9.8 ✓；librknnrt 2.3.0（c949ad889d@2024-11-07）✓；toolkit2 2.3.0 ✓（WSL2 Ubuntu，cp310 manylinux） | 2026-10-02 |
| M2 | 蓝牙音频链路延迟（主观） | A2DP 通（智能音箱 Pro-2639，MAC 50:FE:39:F8:83:8B，trust 已设）；aplay 全链路出声；延迟主观可接受（TTS 场景） | 2026-10-02 |
| M3 | 摄像头节点 / 抓帧格式与分辨率 | OV13855@CAM1（overlay `ov13855-c1`，sensor 7-0036）；CIF 裸 RAW `/dev/video0`，**2688×3136** BG10（4224 请求被驱动钳宽），单帧 17M ✓；无 SDITF 实体 → ISP 内联不可用，HAL 走裸 Bayer+灰世界 WB（`hal/board/camera_raw.py`） | 2026-10-02/03 |
| M4 | 麦阵 / 采集验证 | FY-SP003U 双麦（USB，card 3），`plughw:3,0` 16k/S16LE/stereo 录音 ✓；回放经蓝牙音箱 ✓。板载 ES8388 = card 2（3.5mm 口，M6 本地喇叭用） | 2026-10-02 |
| M5 | 红外学习/回放 | 待 BroadLink RM4 Mini 到货（2026-10-03 改道，见 2.6） | — |
| M6 | 本地喇叭出声 | USB 有源音箱（3.5mm AUX）← 板载 ES8388 card 2 ✓；软音量经 PipeWire；PAM8403 方案取消 | 2026-10-03 |
| KWS | 唤醒词误触/漏触（实机 10 分钟） | 白名单@pause.wav 命中"暂停"✓；wake@非唤醒语 None ✓（夹具验证）；真麦 10 分钟误触/漏触待冒烟 | 2026-10-03 |
| ASR | 流式 RTF | confirm.wav → "确认" ✓（夹具）；流式 RTF 与真麦切句待冒烟 | 2026-10-03 |
| TTS | RTF / 首包延迟 | **RTF 0.27**（0.48s 合成 1.8s 语音），load 3.3s，matcha+vocos @CPU | 2026-10-03 |
| OCR | 端到端时延（拍摄→TTS 首音） | 图→文本 0.40s（900×700 中文测试图，6/6 行检出，det i8@core0 + rec fp16@core1，2026-10-03 冒烟）；全链路（含拍摄/TTS）待 M7 接线后测 | 2026-10-03 |
| 资源 | 内存峰值 / NPU 占用 / 温度（含散热壳） | 待填 | — |

> **M0 实测附注**（2026-10-02）：镜像自带 zram0 swap（3.9G，RAM 介质，不磨损 eMMC——
> 与"无 swap"纪律不冲突，该条防的是磁盘 swap 写穿介质；swappiness 实时性调优留 M7）。
> `/var/log` 已挂 zram1（200M）——M7 评估 log2ram/journald volatile 是否冗余。
