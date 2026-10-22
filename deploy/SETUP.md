# LIRA 部署与运维手册

## 当前板子网络与 SSH（2026-10-22 更新）

- LIRA 板子当前 IP：`192.168.3.253`，后续 SSH 均使用此地址；原 `192.168.101.26` 已停用。
- SSH 用户：`orangepi`；本机别名：`lira`；连接命令：`ssh lira`（等价于 `ssh -i ~/.ssh/lira_board orangepi@192.168.3.253`）。
- 设备面板：`http://192.168.3.253:8080/dev`。
- 历史验证记录中的旧地址仅表示当时环境，后续连接以本节为准。

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
  同步会话直至进程重启（仅受信任后台连接可触发）。**M7 缓解**：设备主循环以监督
  循环挂载同步（断线/会话异常 → 指数退避重连，2s 起步封顶 60s），单次会话故障
  不再永久停同步；sync.py 内部语义未动。
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
.venv/bin/python -m app.main     # 入口 app/main.py:main -> uvicorn 0.0.0.0:8000
```

后台启动后访问 `http://<后台机IP>:8000/`。

**板上常驻（2026-10-04 起，推荐形态）**：后台与设备同机（Orange Pi 5B）常驻，
systemd user unit `deploy/system/lira-backend.service` → 板上
`~/.config/systemd/user/lira-backend.service`（Linger=yes 开机自启、
`Restart=always` 崩溃自拉起、ExecStartPre 清 `__pycache__`，与设备主循环
`lira.service` 同模式）：

```bash
# 板上：装依赖（一次）→ 装单元 → 自启
cd ~/lira/backend && python3 -m venv .venv
.venv/bin/pip install -e .   # 板上走 TUNA：加 -i https://pypi.tuna.tsinghua.edu.cn/simple
cp ~/lira/deploy/system/lira-backend.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now lira-backend
journalctl --user -u lira-backend -f
```

设备接入仍按 1.4：后台「注册设备」取一次性 token → 写入设备
`device/config.yaml` 的 `sync.ws_url`（同机部署可用 `ws://127.0.0.1:8000/ws/device`）
与 `sync.device_token` → 重启设备主循环。

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

**独立画质诊断（2026-10-04/05）**：使用 `python -m lira.diagnostics.camera_quality` 抓取受控 NV12/PNG 并重放处理阶段，步骤和实测状态见 [画质实验记录](../docs/camera-quality-experiment.md)。当前手动查询与抓取已返回 4224×3136，历史的 2688 宽记录不能替代实际格式读回；生产 HAL 仍请求 2688×3136。默认参数近黑和历史识别字符数量不足以确定画质根因，新诊断以同批处理对照及已知正文错误率核实。正式采集行为保持现状。

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

**⚠ 已知画质限制（2026-10-04 实测）**：裸 RAW 路径画面满幅细竖条纹 + 彩噪。
证据链：条纹为固定图案（双帧列剖面相关 1.0、主周期 ~2.5px、幅度大、σ1.5
模糊不可除）——疑似 MIPI 读出/打包层伪影，软件只能软化无法根治。已落地
软件抑制组合（曝光固定上限 3210 + Bayer 域高通列校正 + 2D 低通/反锐化 +
色度中值去噪 + 自动对比度 + gamma，`camera_raw.py`），色彩/对比度明显改善
但条纹仍可见。**根治路线 = 打通 ISP mainpath NV12（camera_rkisp.py）**，
届时 capture() 接口不变直接切换。OCR 场景待实测全分辨率下可读性。

**官方栈基准对照验证（阶段 A，2026-10-04）**：> 上文 ⚠ 段与"留给 camera_rkisp.py 打通"
所述为**历史状态**——ISP mainpath 已于 2026-10-03 打通并成为现行主路径
（`hal/board/camera_isp.py`，rkcif→sditf→rkisp0-vir2→mainpath/video11），
HAL 现状以本子段及 2.10 刷新后的 M3 行为准。

用法核对（只读查证，未安装任何东西）：

```bash
media-ctl -p -d /dev/media1  # 期望: driver rkisp0-vir2; rkisp-isp-subdev(v4l-subdev3)
                             #       SBGGR10 4224x3136 ← rkcif-mipi-lvds2 [ENABLED]
                             #       → mainpath(/dev/video11) + selfpath(/dev/video12) 均挂接 ✓
media-ctl -p -d /dev/media0  # 期望: rkcif-mipi-lvds2; csi2 → stream_cif_mipi_id0(video0) ✓
find /proc/device-tree -iname '*ov13855*'   # 期望: i2c@fec90000/ov13855-1@36 status=okay
#   module/lens = CMK-OT2016-FV1 / default → IQ 文件名 ov13855_CMK-OT2016-FV1_default.json
find / -name '*rkaiq*' -not -path '/proc/*' -not -path '/sys/*'  # 期望: 无命中
ls /etc/iqfiles               # 期望: 镜像自带 3 个 json（imx219/ov13850_CMK-CT0116/ov5647）
                              #       ——带 IQ 文件与目录，不带 rkaiq_3A_server/librkaiq.so
```

同场景参考帧对（驱动原生 = 传感器控制复位默认 exposure 2048 / gain 128 后 v4l2-ctl
直抓 NV12，无任何软件 3A；现管线 = `camera_isp.py` 完整软件 3A 路径；样张留存板上
`/home/orangepi/lira/u1-samples/`，md5：native `be513233...` / pipeline `04a35ba7...`）：

| 臂 | 亮度均值 | 拉普拉斯方差 | 说明 |
|----|---------|-------------|------|
| 驱动原生 | **2.96** | 0.8 | 默认增益下室内近全黑——无 3A 时传感器进光量差两个数量级 |
| 现管线（软件 3A） | **126.2** | 184.2 | 亮度锚定 120 生效；细节量正常 |

**阶段 A 结论**：传输层（节点/格式/链路/设备树命名）全部核对无误，摄像头用法正确；
画质差距的根源是**默认状态下无 3A**（原生帧近全黑），即 3A 才是决定性杠杆——
支持进入阶段 B（安装官方 3A 栈拿画质上限）。

**阶段 B 尝试记录（2026-10-04，结论：AE 反馈环不可适配，现状为终态）**：

按计划（`docs/plans/2026-10-04-001-feat-camera-official-stack-plan.md`）完成：

- 板上原生编译 rkaiq 两个版本：Firefly `rk3588/firefly`（**v5.0x4.1**，2023-10-23）与
  JeffyCN `rkaiq-2024_04_08` tag（**v6.0x6.1**，2024-03-03）；编译补丁三处
  （j2s 宿主工具 x86 flag、-Werror、j2s4b_dev 安装前缀）+ drm 头文件垫片
  （`/usr/include/libdrm` 平铺布局适配）
- IQ 文件 `ov13855_CMK-OT2016-FV1_default.json` 已装 `/etc/iqfiles/`（与设备树
  module/lens 名实测吻合；镜像自带 /etc/iqfiles 但无 ov13855 项、无守护进程二进制）
- 部署 `~/rkaiq-v6/{bin,lib}`（v5 在 `~/rkaiq/`）+ 手动启停脚本 `~/rkaiq-v6/start-daemon.sh`
  （**无自启单元**）

**失败定位（证据链）**：

1. v5.0x4.1：拓扑识别失败（"can't find sensor"）+ IQ 新代际枚举不认识——5.10 时代产物，
   预期排除
2. v6.0x6.1：init/prepare/start 全通、params 正常下发（内核 `first params buf queue`）、
   流启停联动正常、CPU 占用可忽略、与采集共存无冲突——但 **AE 冻结**（exposure 289/gain 128
   恒定，画面近全黑 luma 0.75）。debug 日志钉死断点：`AEC:D:ae_stat == NULL`——
   **守护进程打开了统计节点（video18）但从未 STREAMON**（内核无任何统计缓冲入队日志）；
   算法侧零统计输入 → 不更新
3. 内核统计路径**独立验证健康**：主链路流期间手动收 video18，3 个统计缓冲正常交付
   （48KB）——问题锁定在 rkaiq 引擎统计线程与本机 vendor 内核（rk35xx 6.1 BSP）的适配深度，
   非快速可修

**终态**：守护进程已停止、无自启单元；采集主路径维持软件 3A（`camera_isp.py`）。
板上产物保留（`~/rkaiq-v6/`、`~/build/rkaiq-v6-src/`），后续若要续查：断点是 rkaiq
统计线程适配，建议对照 Radxa/FriendlyELEC 同内核的打包差异（其 6.1 BSP 的 rkisp
驱动配置/补丁差异）——属于 follow-up,不在本计划范围。

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

**到货后步骤**（2026-10-05 执行记录）：
1. ✅ RM4 Mini USB 供电 → 官方 App 配网入家庭 WiFi → 路由器固定 DHCP IP。
   发现探测（板上）：`ip=192.168.101.27 mac=ec:0b:ae:0c:74:e4 devtype=0x653a
   model='RM4 mini' name='智能遥控'`（0x653a 为 1.0.6 支持表内 RM4 mini 变体，
   自动映射 rm4mini 类）。
2. ✅ 板上安装（**版本锁定 ==1.0.6**）：
   ```bash
   .venv/bin/pip install --ignore-requires-python 'python-broadlink==1.0.6'
   ```
   上游 1.0.x 元数据要求 py>=3.13 且旧版 0.18.x 已从 PyPI 全部下架；实测 1.0.6
   在板上 py3.11 发现/认证全链路正常（asyncio 原生重写，无 3.13 专属语法），
   `--ignore-requires-python` 为元数据绕行。**不入 pyproject**（x86 开发/单测
   不需要此依赖；升级前必须重跑实机学习/回放冒烟）。发现探测命令：
   `python -m lira.hal.board.ir_broadlink`。
3. ✅ 传输层实现（`lira/hal/board/ir_broadlink.py`，feat/ir-broadlink 分支）：
   hello 发现 + MAC 核对 + auth 建会话；`send`=直发 base64 载荷，`learn`=进入
   学习模式轮询取码；错误统一转带修复指引的 HalError。U6 的 store/意图链路不变
   （码值为不透明字符串，直接换传输层）。板上 config.yaml 已加
   `hal.ir_host/ir_mac`。**IR open 失败（离线/锁定）自动降级 Mock**：家电控制
   不可用但语音/阅读照常，不进崩溃循环（R9）。
4. ✅ 设备解锁与实机冒烟（2026-10-05）：**锁定是 auth -1 的头号原因**——新固件设备
   默认锁到云账户（App 发现探测可见 `is_locked=True`）。官方 App → 设备 → 设置 →
   关闭「安全锁/Lock Device」后 auth 即通。实机冒烟：`learn` 轮询捕获 266 字节码
   （未按键期间持续 -5 StorageError 属正常等待，轮询按"还没录到"续等）→ 5s 后
   `send` 回放 → **目标设备有反应**。服务装配日志：`BroadLink IR 就绪:
   host=192.168.101.27 devtype=0x653a model=RM4 mini locked=False`。M5 ✅

**韧性注记**：R9 的"断网"指外网——RM4 走局域网，外网断不影响；家庭 AP 故障则 IR 失效
（open 失败自动降级 Mock，主循环不受影响），作为已知限制随本节记录（最终形态若不可接受，
备选 ESP32+LC 模块有线桥）。

### 2.7 M6：本地喇叭（2026-10-03 完成）

定案 = 原"方案 B"零成本落地：**USB 供电有源音箱**（自带功放），3.5mm 公对公线
直连板载 ES8388（card 2，线路输出）。PAM8403 功放板方案取消（裸板留档不用）。

- 接线：3.5mm 公对公 → 音箱 AUX IN；音箱 USB 供电（板载 USB 口或独立充电头均可）。
- 底噪注意：共地环路"滋滋"声 → 音箱改插独立充电头即消。
- 软音量路径（R21"大声点/小声点"）：PipeWire/ALSA 层实现；M7 固化设备号
  （capture = FY-SP003U card 3，playback = ES8388 card 2）。
- 蓝牙小爱音箱（2.3）转开发期备用/对比。验证通过 2026-10-03。

### 2.8 M7：软件上板与 provisioning

> **软件接线已完成（2026-10-03）**：`lira/main.py` 主循环（隐私门 → 路由分发 →
> 状态机 → TTS/IR/阅读管线）、`lira/sync_ws.py` 真实 WS 同步传输（断线指数退避重连）、
> UI in-process 挂载、`--dry-run` 装配图（含板上设备号与模型就绪探测）。x86 单元 + e2e
> 全绿；**语音唤醒→ASR→LLM 对话链路已于 2026-10-04 实机验证通过**（真麦 KWS 命中
> "小丽拉"→ 播"我在听"→ GLM 远程调用 200 OK/2.5s； systemd user unit 常驻 +
> 开机自启，见 ④）；阅读/视觉链路经开发者控制台实机使用中（vision/OCR 事件正常）。
> 同步（ws_url）仍待接；IR 已接通（2026-10-05 M5 冒烟通过，见 2.6）。

**① 依赖与安装**（extras 名以 `device/pyproject.toml` 为准）：

```bash
sudo apt install python3-venv portaudio19-dev libsndfile1
cd ~/lira/device && python3 -m venv .venv
.venv/bin/pip install -e ".[audio,llm,ui,sync]"
.venv/bin/pip install opencv-python-headless   # camera_raw/reading 的 cv2 依赖
```

**② 配置**（`device/config.yaml`）：

```yaml
hal:
  backend: board          # mock | board
  mic_device: "pipewire"    # 经 PipeWire 默认源=FY-SP003U（M4 实测 card 3）。
                            # ⚠ 勿填数字序号：PortAudio 序号≠ALSA 卡号（序号"3"是
                            # sysdefault，无输入通道）；USB 麦阵仅经插件可见（2026-10-04 实测）
  speaker_device: "pipewire"  # 经 PipeWire 默认汇=板载 ES8388 线路输出（M6 实测 card 2）。
                              # ES8388 hw 直开 22050 被拒（ALSA -22），必须走插件做格式转换
  camera_device: ""       # /dev/video0（OV13855@CAM1，M3）
ui:
  host: 0.0.0.0
  port: 8080
sync:
  ws_url: ws://<后台机IP>:8000/ws/device
  device_token: <一次性 token>   # 或环境变量 LIRA_SYNC_DEVICE_TOKEN
dev_console:
  enabled: true           # 开发者控制台（默认关；M7 实机验证期建议开）
```

> **开发者控制台**（`http://<设备IP>:8080/dev`，设备口令进入）：状态总览
> （状态机/路由/同步 (epoch,version)/熔断/模型就绪）、**语音交互实时日志**
> （1s 轮询：等待唤醒/收到唤醒/识别到指令/大模型交互与返回/语音输出中/
> 退出唤醒——条目含识别与回复片段，仅内存环形区不落盘，2026-10-04）、
> 环形日志、音量语速调整、语音（TTS/录音/ASR→意图）、视觉（拍摄→OCR→
> 分阶段耗时）、同步手动拉取、"唤醒→指令"场景回放（真实出声，需确认）。
> **隐私联锁**：隐私 ON 时采集类测试（录音/ASR/视觉）服务端拒绝。仅局域网；
> 采集数据全程内存不落盘。产线部署保持 `enabled: false`。

**③ 冒烟顺序**（对照 2.9 清单）：

```bash
# 1) 装配校验（模型/设备号就绪性一目了然）
.venv/bin/python -m lira.main --dry-run

# 2) 带硬件主循环（前台跑通再 systemd 化）
.venv/bin/python -m lira.main
```

- 预期：打印装配图 → HAL 逐个就绪日志 → "LIRA 运行中"；说唤醒词 → "我在听" →
  "读一下" → 拍摄引导 + 朗读；后台下发配置 → 设备播报（新禁用家电）。
- IR 板上传输层已交付（M5，`lira/hal/board/ir_broadlink.py`）：`hal.ir_host/ir_mac`
  已配置 → 装配 `BroadlinkIrController`（auth 失败自动降级 Mock 并 ERROR 告警）；
  未配置 → 回退 `MockIrController` 并告警（发射只记录不生效，冒烟跳过 2.9 第 2/3 项）。
  设备解锁与学习/回放冒烟见 2.6 第 4 步。
- 后台库重建/epoch 迁移：`python -m lira.sync reset-sync data/device.db`（SETUP.md 1.4）。
- systemd 化 + 存量纪律：`deploy/system/`（log2ram、journald `Storage=volatile`、
  fstab `noatime`、无 swap、thermal governor——R18 介质寿命）；`ExecStart` 以
  `python -m lira.main` 为准，实机核对后固化 unit 文件。
- 重启验证：开机自动进入待机，无人工干预。

**④ systemd 常驻（2026-10-04 完成）**：

- unit：`deploy/system/lira.service` → 板上 `~/.config/systemd/user/lira.service`
  （**user unit**：主循环依赖 orangepi 用户的 PipeWire 会话；镜像 `Linger=yes` 已开，
  开机自启无需桌面登录）。`ExecStartPre` 沿用 start-lira.sh 的清 `__pycache__` 纪律。
- 已执行：`systemctl --user daemon-reload && systemctl --user enable --now lira`；
  崩溃自拉起（`Restart=always`，3s）。手动重启入口 `deploy/start-lira.sh` 已改为
  `systemctl --user restart lira` 薄封装（勿再直接 nohup——会与 unit 双开抢麦）。
- 日志改走 journald：`journalctl --user -u lira -f`（原 /tmp/lira.log 不再更新）。
- 实机验证（2026-10-04）：服务 active、`采集已启动 @ pipewire`（设备号钉死生效）、
  KWS/ASR/TTS/摄像头加载、UI 200；10 分钟 soak 复核通过（待机唤醒 4/4、漏触 0、
  误触 0，见 2.10 KWS 行）。
- 仍属后续的存量纪律项：journald `Storage=volatile` / log2ram / fstab `noatime`
  / thermal governor 核对（M0 附注：镜像自带 zram swap 与 /var/log zram1，
  已部分覆盖 R18 意图）。

### 2.9 冒烟清单（全部里程碑后执行）

1. 唤醒词唤醒（提示音 + "我在听"）→ 拍报纸 → 朗读；
2. "打开空调"（已学习码）→ 红外发射 → 家电响应；
3. 高危设备（取暖器）→ 语音二次确认 → 确认后才执行；模糊应答/超时不执行（fail-closed）；
4. **拔网线** → 再拍药品说明书 → 朗读 OCR 原文 + 免责提示（不静默、不拒读）；
5. 局域网 Web UI 登录 → 隐私模式开/关 → 设备语音/状态同步正确；
6. 麦阵拾音 + 蓝牙喇叭出声全链路（若仍在开发期）。

第 6 项仅开发期；**冒烟清单的最终配置必须为本地喇叭**（上电即出声）。

### 2.9.1 聊天走查（home-console 批次一）

浏览器（手机/平板/电脑均可，同局域网）访问 `http://<设备IP>:8080/`：

- **F1 选人对话**：首页聊天入口 → 选人页创建/选择成员（emoji 头像 + 称呼）→
  会话页发消息等回复 → **刷新页面停留在当前成员**（URL 即状态）→
  「加载更多」翻看更早历史。
- **F2 隐私降级**：首页隐私开关 ON（需设备口令）→ 回会话页再发消息 →
  收到降级提示。**文案核查**：提示须为"**没有发送到远程或云端**"式承诺
  （沿 phrasebook `CHAT_PRIVACY` 措辞精度）；不得出现"未离开设备"式过度
  承诺——局域网明文 HTTP 与设备本地存储使更强承诺不实。
- **db 路径核对**：聊天表（`members` / `messages`）幂等建在既有设备库
  `data/device.db`（板上 `/home/orangepi/lira/data/device.db`，按仓库根相对
  解析）——**不得出现第三份 db 文件**；`journalctl --user -u lira` 中聊天仅
  `chat event=...` 事件行（成员只记数字 ID），无任何消息内容。

### 2.10 实机参数记录表（每里程碑回填）

| 里程碑 | 参数 | 实测值 | 日期 |
|--------|------|--------|------|
| M0 | 镜像文件名 / 内核（`uname -a`） | debian_bookworm_server_linux6.1.99；6.1.99-rockchip-rk3588 #1.1.0（2026-08-20 构建）；根分区在 eMMC（mmcblk0p1，57G 已扩容） | 2026-10-02 |
| M1 | NPU 驱动 / librknnrt / rknn-toolkit2 版本 | 驱动 v0.9.8 ✓；librknnrt 2.3.0（c949ad889d@2024-11-07）✓；toolkit2 2.3.0 ✓（WSL2 Ubuntu，cp310 manylinux） | 2026-10-02 |
| M2 | 蓝牙音频链路延迟（主观） | A2DP 通（智能音箱 Pro-2639，MAC 50:FE:39:F8:83:8B，trust 已设）；aplay 全链路出声；延迟主观可接受（TTS 场景） | 2026-10-02 |
| M3 | 摄像头节点 / 抓帧格式与分辨率 | OV13855@CAM1（overlay `ov13855-c1`，sensor 7-0036）；ISP mainpath `/dev/video11` **2688×3136** NV12（4224 请求被 rkcif 驱动钳宽），HAL 走 ISP 管线+软件 3A（`hal/board/camera_isp.py`，2026-10-03 打通；CIF 裸 RAW 路径已退役）；链路 rkcif→sditf→rkisp0-vir2 经 media1 确认 ENABLED | 2026-10-02/04 |
| M4 | 麦阵 / 采集验证 | FY-SP003U 双麦（USB，card 3），`plughw:3,0` 16k/S16LE/stereo 录音 ✓；回放经蓝牙音箱 ✓。板载 ES8388 = card 2（3.5mm 口，M6 本地喇叭用） | 2026-10-02 |
| M5 | 红外学习/回放 | RM4 mini @192.168.101.27（0x653a）；python-broadlink==1.0.6 锁定安装；解锁后实机冒烟：学习 266B 码 → 回放设备有反应 ✅（见 2.6） | 2026-10-05 |
| M6 | 本地喇叭出声 | USB 有源音箱（3.5mm AUX）← 板载 ES8388 card 2 ✓；软音量经 PipeWire；PAM8403 方案取消 | 2026-10-03 |
| M7 | 唤醒→对话链路实机 + 常驻 | systemd user unit 常驻（Linger 开机自启、Restart=always）、音频设备号钉死 `pipewire` 子串；真麦 唤醒→ASR→GLM（200 OK/1~3s）→TTS 全链路 ✓；UI 200；详见 2.8 ④ | 2026-10-04 |
| KWS | 唤醒词误触/漏触（实机 10 分钟） | 夹具 0.1~0.25 四档阈值全命中、负样本零误触（2026-10-03）；**实机 soak：待机态唤醒 4/4 漏触 0、误触 0**——背景为持续大音量外来语视频语音（峰值削波），每次命中均完成唤醒→指令→GLM 回复全链路；另 2 次对话态重复唤醒被状态机按设计抑制（离线复核检出、实时正确不响应） | 2026-10-04 |
| ASR | 流式 RTF | confirm.wav → "确认" ✓（夹具）；**真麦切句实机验证通过**（2026-10-04 soak：嘈杂背景下 4 条唤醒后指令全部正确切句并路由 LLM）；RTF 精确值未单独测量 | 2026-10-04 |
| TTS | RTF / 首包延迟 | **RTF 0.27**（0.48s 合成 1.8s 语音），load 3.3s，matcha+vocos @CPU | 2026-10-03 |
| OCR | 端到端时延（拍摄→TTS 首音） | 图→文本 0.40s（900×700 中文测试图，6/6 行检出，det i8@core0 + rec fp16@core1，2026-10-03 冒烟）；软件接线已完成（2026-10-03），板上全链路实测待执行 | 2026-10-03 |
| 阶段A | 官方栈基准对照验证 | 传输层全部核对无误（media1 拓扑/传感器能力/DT 命名 CMK-OT2016-FV1→IQ 名吻合）；镜像**带 /etc/iqfiles（3 json）但不带 rkaiq 守护进程**；同场景对照：原生帧均值 2.96（近全黑）vs 现管线 126.2——用法正确，画质差距根源=无 3A，支持进阶段 B | 2026-10-04 |
| 阶段B | rkaiq 官方 3A 栈引入 | 板上原生编译 v5.0x4.1（Firefly）与 v6.0x6.1（JeffyCN rkaiq-2024_04_08）；v6 守护进程 init/IQ/params/流联动全通、CPU 可忽略，但 **AE 反馈环断**（守护进程不 STREAMON 统计节点→`ae_stat==NULL`→曝光冻结 289/128 画面近黑）；内核统计路径独立验证健康。按 R8 落档收工：守护进程停止、无自启、主路径维持软件 3A；产物留板 `~/rkaiq-v6/`。详见 §2.4 阶段 B 记录 | 2026-10-04 |
| 画质 | 噪点治理（软件 3A 三杠杆） | 15fps 长曝光档（vblank 3300→曝光上限 6400，模拟增益 1984→~1700）+ 单流批抓三帧对齐均值（√3 信噪比，失配回退择优）+ 双边滤波。同场景 OCR 有效字符 319→433（+26%），文本由乱码变连贯；板上实测 capture ≈4~6s（原 ~3s，朗读场景可接受）。顺带发现：噪声帧拉普拉斯方差高于干净帧——旧"三帧择优"在暗光下偏向选噪声最重的帧 | 2026-10-04 |
| 资源 | 内存峰值 / NPU 占用 / 温度（含散热壳） | 待填 | — |
| 语音机制 | 命令 KWS 误触/喊停、勿扰进出、逃生舱、真关机、三路算力（2026-10-06 计划 U10） | 主机全量回归通过（含真实 KWS/ASR e2e：命令通道喊停、退出短语、唤醒前缀关机）；**声学验收与三路负载实测待板测**，清单见 `docs/voice-continuity-validation.md`「语音退出/勿扰/控制机制验收」 | 待板测 |

> **M0 实测附注**（2026-10-02）：镜像自带 zram0 swap（3.9G，RAM 介质，不磨损 eMMC——
> 与"无 swap"纪律不冲突，该条防的是磁盘 swap 写穿介质；swappiness 实时性调优留 M7）。
> `/var/log` 已挂 zram1（200M）——M7 评估 log2ram/journald volatile 是否冗余。

---

## 3. 声纹候选自动积累（开发者控制台改版 U6）

> 面向部署/运维：候选积累随设备主循环自动进行，无需页面开启。数据只存本地
> SQLite（`voice_candidates` / `voice_candidate_evidence` 两张新表，与既有
> `members` / `voice_profiles` 相互独立），**不存原始音频**——只有归一化向量、
> 段数/时长统计与去重哈希。

### 3.1 门控与装配

- 功能随 `LIRA_PERSONAL_MEMORY=1` 一起启用（与既有声纹识别同一开关）：
  主循环在识别到有效语句后，把已完成提取的声纹向量交给候选存储；
  未启用时该路径完全不存在（`speaker_candidates=None`），主链路零变化。
- **已建立声纹的可靠匹配继续走现有身份识别**，不建候选、不受本功能影响。
- 候选表确认前没有成员权限：候选不产生个人记忆读写（U7 才提供确认晋升）。

### 3.2 候选积累参数（config.yaml `speaker` 段 / 环境变量覆盖）

| 参数 | 默认值 | 环境变量 | 含义 |
|------|--------|----------|------|
| `target_segments` | 3 | `LIRA_SPEAKER_TARGET_SEGMENTS` | 就绪目标：独立合格语音段数 |
| `target_seconds` | 10.0 | `LIRA_SPEAKER_TARGET_SECONDS` | 就绪目标：累计有效秒数 |
| `min_segment_seconds` | 1.0 | `LIRA_SPEAKER_MIN_SEGMENT_SECONDS` | 单段最短时长（秒） |
| `max_candidates` | 20 | `LIRA_SPEAKER_MAX_CANDIDATES` | 未绑定候选上限 |
| `candidate_ttl_days` | 7.0 | `LIRA_SPEAKER_TTL_DAYS` | 无新证据的过期天数 |
| `match_threshold` / `match_margin` | 0.80 / 0.10 | —（改 yaml） | 已建立成员匹配门槛/歧义边际 |
| `assign_threshold` | 0.73 | —（改 yaml） | 候选指派门槛 |
| `consistency_threshold` | 0.85 | —（改 yaml） | 段间一致性门 |
| `min_rms` / `max_clip_ratio` | 0.01 / 0.05 | —（改 yaml） | 能量下限 / 削波比例上限 |
| `strategy_version` | 1 | —（改 yaml） | 采集策略版本 |

**声明**：以上均为**工程初值，不代表识别准确率**——板端质量阈值与样本目标
的校准属于 U8 交付门槛；校准不合格不得对外宣称就绪 100% 或识别准确率。

### 3.3 状态与生命周期

- 进度 = min(段数/`target_segments`, 秒数/`target_seconds`)，重复片段、
  过短、低能量、削波、歧义（与多个已知声音边际不足）不计入进度；
  未过段间一致性门时进度封顶 99%，三项条件齐备才 `ready`（仍待身份确认）。
- 候选状态机：`accumulating`（积累中）→ `ready`（采集就绪待确认）；
  `stale`（模型指纹或 `strategy_version` 变化，旧证据不删除、不混算，
  需重新积累）；`expired`（超过 ttl 无新证据）。重复片段靠样本哈希去重
  （跨重启稳定），全局滚动保留最近 100 个标识。
- **隐私联锁（AE5）**：隐私开启即停写——提取结果提交前经主循环复检
  （代次/隐私/状态）与存储内第二道隐私检查；迟到结果不落库。
- 升级前备份设备库（§1.5 冷拷贝方式同样适用）；回滚关闭新功能后新增表
  保留不删，家庭历史不受影响。

## 4. 开发者控制台改版：升级、验证与校准交接（U8）

### 4.1 升级流程（源码部署路径）

板上主循环以源码 checkout 运行（`/home/orangepi/lira/device`，`lira.service`
user unit 直启 `.venv/bin/python -m lira.main`）。升级三步：

1. **备份**（升级前必须）：
   ```bash
   ssh lira 'tar -czf ~/lira/deploy-backups/device-$(date +%Y%m%d-%H%M%S).tar.gz \
     -C ~/lira device --exclude device/.venv --exclude device/lira/__pycache__ \
     && cp -a ~/lira/data/device.db ~/lira/deploy-backups/device.db.$(date +%Y%m%d-%H%M%S)'
   ```
2. **同步源码**：`rsync -av --delete device/lira/ lira:~/lira/device/lira/`
   （先 `-n` 干跑核对增量；`--delete` 只作用于包目录内部，清掉板上旧文件），
   同步 `device/pyproject.toml`（若依赖有变需在板 venv 补装）。
3. **重启与检查**：`ssh lira 'systemctl --user restart lira'`，随后跑 §4.2。

wheel 打包验证（KTD8，开发机执行）：`pip wheel . --no-deps` 构建后，在**干净
venv** 离线安装（`--no-index --find-links=<wheels>`），断言
`site-packages/lira/ui/{templates,static}` 齐全且 `dev.html` 可渲染——控制台
资产必须来自安装包而非源码路径；无 CDN、无构建链。

### 4.2 部署后自动化检查（无需在场）

```bash
ssh lira 'systemctl --user is-active lira && \
  journalctl --user -u lira --since -2min | grep -iE "traceback|error" | grep -v ERROR_LEVEL_EXPECTED || true'
curl -s http://<board-ip>:<ui-port>/status        # 只读状态 JSON（不鉴权）
curl -s -o /dev/null -w "%{http_code}" http://<board-ip>:<ui-port>/dev   # 401/303/200 均为挂载证据
```

判定：服务 active、日志无 traceback、`/status` 200、`/dev` 非连接拒绝；
浏览器打开 `/dev` 走完登录 → 磁贴全渲染 → 实时区曲线随语音出现包络。

注（U8 审计）：`/status` 的 `models` 字段是**启动期快照**（装配定型，
250ms observe 轮询免每拍 8 次文件系统 stat）——运行中补放模型文件不会
反映，重启后生效。另：示例里的 `grep -v ERROR_LEVEL_EXPECTED` 是占位
过滤器，按需替换成期望中的良性错误关键词。

### 4.3 校准交接清单（需家庭成员在场，未完成前不得宣称就绪）

板端校准是交付门槛（KTD5/Verification Contract）。以下每项记录到
`docs/` 校准笔记（含日期、参数版本、结论）：

- [ ] **有效语音判定**：不同家人交替交谈（A/B/A ≥3 轮），核对候选归组稳定、
  重复/短句/噪声不虚增进度（对照实时区声纹磁贴与 `voice_candidate` 事件）。
- [ ] **质量门槛**：低声/远场/削波（贴麦克风喊）各说数句，核对
  `low_energy/clipped/too_short` 拒绝原因如预期；不合适则调 §3.2 参数并
  **bump `strategy_version`**（旧证据自动失效重积，不会静默混算）。
- [ ] **同名关联**：对已有成员称呼说"我是X"（X 已存在），核对会话不绑定、
  不读记忆、声纹磁贴出现"同名待确认"；控制台确认后身份生效且既有声纹未被
  覆盖；再对无同名的新声音走自然命名全程。
- [ ] **性能基线**：同一音频场景对比观察开/关（实时区折叠 vs 展开）各
  ≥10 分钟：无第二采集流、无 TTS 重复、无额外断档；记录 CPU/内存差异
  （`pidstat -p $(pgrep -f lira.main) 60`）。主音频线程超 100ms 块周期即失败。
- [ ] **观测时延**：说话到曲线可见 p95 ≤1s（250ms 轮询单飞）。
- [ ] **隐私联锁实测**：交谈中开隐私——磁贴立即停更、候选停止积累、
  已开页面 ≤1s 清空敏感展示。
- [ ] 校准结论回填 §3.2 声明处（达标/未达标如实记录；未达标保留
  "工程初值"标注，不得对外宣称识别准确率）。

### 4.4 可选 3D 决策记录（R6）

U8 决策：**不采用 Three.js**。R6 的"温馨动效"已由 CSS 过渡/按压反馈覆盖
（`prefers-reduced-motion` 全量支持，信息不依赖动画），3D 作为非必要点缀
引入本地 vendor 资产与维护成本不成比例；数据与操作路径不受影响。后续若
重评，须满足：本地随包、按需加载、可关、静态回退，且不改变验收路径。
