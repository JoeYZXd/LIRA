---
title: "feat: 摄像头官方栈对照验证与 rkaiq v6.x 引入(A→B 分步)"
type: feat
status: active
date: 2026-10-04
origin: docs/brainstorms/2026-10-04-camera-official-stack-requirements.md
---

# feat: 摄像头官方栈对照验证与 rkaiq v6.x 引入(A→B 分步)

## Summary

计划把 origin 的 A→B 两阶段落成 4 个实现单元:先用零依赖参考工具在板上完成基准对照并按 SETUP.md 惯例落档(U1),再按研究修正后的获取顺序跑通 **rkaiq v6.x + OV13855 IQ 文件**(U2),同场景对比后按"明显更优才切换"判据决策(U3),决策为切换时给 `V4l2IspCamera` 加探测式让渡逻辑(U4)。仓库侧只动 `deploy/SETUP.md`、`device/lira/hal/board/camera_isp.py` 和一个新测试文件;大部分执行发生在板上。

---

## Problem Frame

自研采集路径功能可用但无外部参照,"不确定是否正确使用了摄像头";官方 3A 栈(rkaiq)是 Android 相机开箱好用的原因,也是画质上限所在。传输层已排除嫌疑(2688 钳位是已知驱动约束),差距在 3A/IQ 层。完整叙述见 origin(see origin: docs/brainstorms/2026-10-04-camera-official-stack-requirements.md)。

计划期研究补两个关键事实:板上内核实测 **6.1.99-rockchip-rk3588**(SETUP.md §2.10 M0 行),因此官方栈版本硬门槛是 **v6.x**(v5.x 配 5.10 内核,交叉组合有失败记录);板载拓扑 `rkisp0-vir2` 本身就是 SDITF 机制产物,rkcif→rkisp 内联是 rkaiq 官方支持形态,同板同传感器有成功先例。

---

## Requirements

- R1. 同场景参照抓帧:驱动原生输出(无自研 3A)与现管线输出同场景留存对比(→ origin R1)
- R2. 用法核对清单:媒体链路拓扑、传感器格式/分辨率、镜像内 rkaiq 相关组件查证,只查证不安装(→ origin R2)
- R3. 对照结论落档:过程进 SETUP.md §2.4 带日期子段,结论加 §2.10 新行(→ origin R3)
- R4. 来源顺序获取官方栈,按内核兼容性筛选(计划期已细化为 v6.x 门槛,见 Key Technical Decisions)(→ origin R4)
- R5. 配齐 OV13855 IQ 文件;不用 OV13850 改名冒充(有实测失败记录)(→ origin R5)
- R6. 控制权让渡:官方栈生效时自研路径停止写传感器曝光/增益,且跳过数字曝光补偿(→ origin R6)
- R7. 切换判据:同场景明显更优才切换主路径,否则回退(→ origin R7)
- R8. 失败落档:不可适配时记录原因,现状即终态(→ origin R8)

**Origin actors:** A1(开发者,执行者), A2(老人用户,受主路径切换影响)
**Origin flows:** F1(阶段 A 验证,含 escape path), F2(阶段 B 引入,含回退 escape path)
**Origin acceptance examples:** AE1(R1,R3), AE2(R2), AE3(R6), AE4(R7), AE5(R8)

---

## Scope Boundaries

(继承 origin,以下为计划期重申与新识别项)

- 实时视频查看(控制台 MJPEG/GStreamer 预览):不做;B 失败后如需远程查看另行立项
- 为适配 rkaiq 升级/更换内核或系统镜像:不做,不可适配即落档收工
- 阅读流程/OCR 管线改动:不动
- IQ 文件专业调优:只做"配齐能用"
- 守护进程生产态生命周期管理(systemd unit / 按需封装):本轮守护进程手动起停,生产化形态延后立项

### Deferred to Follow-Up Work

- LIRA 常驻形态下 rkaiq_3A_server 的生命周期管理(常驻服务 vs 拍摄时按需起停):依赖 U3 的 CPU 占用实测数据与切换决策,单独立项
- GStreamer 硬件编码查看管线(mppjpegenc/mpph264enc):作为 B 失败时的兜底查看器或后续实时预览的基础,另行立项

---

## Context & Research

### Relevant Code and Patterns

- `device/lira/hal/board/camera_isp.py` — 现行主路径 `V4l2IspCamera`:open() 即写传感器 exposure(U4 让渡时机的硬约束);capture 链路 = 自适应 analogue_gain → 三帧择优(`_sharpness`)→ 数字曝光补偿(`_digital_exposure`,内部复用 `camera_raw._gray_world_wb`);`_sensor_set()` 经 v4l2-ctl 子进程写 `/dev/v4l-subdev2`
- `device/lira/hal/board/camera_raw.py` — 已不装配,保留为纯函数提供者 + 阶段 A"驱动原生输出"的历史参照;其模块注释记录了 2688 钳位实测
- `device/lira/main.py` — `build_board_hal`(:166-187)装配 `V4l2IspCamera(device=cfg.hal.camera_device or "/dev/video11")`
- `deploy/SETUP.md` — §2.4(M3 摄像头,含 ⚠ 已知画质限制段的记录格式)、§2.10(记录表,4 列;M3 行滞后仍写裸 Bayer 路径,需刷新);板上路径 /home/orangepi/lira/device,启动脚本 `deploy/start-lira.sh`(pkill + nohup 风格,无 systemd 先例)
- 测试惯例:板特有代码只测纯函数(test_dev_console.py:651-665 直接 import `camera_isp._apply_orientation`)+ 构造级装配测试(test_main_loop.py:412-429,构造不 open())

### Institutional Learnings

- 仓库无 docs/solutions/;SETUP.md §2.4/§2.10 即本仓库的 bring-up 知识载体,本计划落档沿用其格式

### External References

- ubuntu-rockchip issue #254(OPi5B + OV13855 全程记录,含 rkisp0-vir 拓扑成功日志、v5.0x1.3 多摄 bug、v5.0x3.0 修复):https://github.com/Joshua-Riek/ubuntu-rockchip/issues/254
- 兼容矩阵(Radxa 论坛 21422:rkaiq 版本↔内核版本实测,交叉组合失败细节):https://forum.radxa.com/t/camera-engine-rkaiq-sources/21422
- IQ 文件源(Firefly isp3x 分支,含 ov13855_CMK-OT2016-FV1_default.json):https://gitlab.com/firefly-linux/external/camera_engine_rkaiq/-/tree/rk3588/firefly
- rkaiq 源码快照 mirror(JeffyCN,tag rkaiq-2024_04_08 ≈ v6.0x6.1 及更新):https://github.com/JeffyCN/mirrors/tags
- 镜像内提取先例(CSDN:从 OPi 官方 img 提取 rkaiq_3A_server + librkaiq.so + IQ 文件补齐 3A):https://blog.csdn.net/weixin_52775369/article/details/161104294
- peyton-howe deb 文件布局(usr/bin / etc/iqfiles / init.d+systemd unit 结构,仅作布局参考;其 v5.0x4.1 为 5.10 时代产物):https://github.com/peyton-howe/camera-engine-rkaiq
- FriendlyELEC RK3588 更新日志(camera_engine_rkaiq 6.30.4 预装):https://wiki.friendlyelec.com/wiki/index.php/Template:RK3588-UpdateLog
- GStreamer 参考管线(Radxa Rock5B 验证,v4l2src→mppjpegenc):https://forum.radxa.com/t/how-to-use-radxa-camera-4k/13031

---

## Key Technical Decisions

- **版本硬门槛 rkaiq v6.x**:板上内核 6.1.99(实测),Radxa 实测 v6.0x6.1@6.1 可用、v5.x@6.1 交叉组合失败(ioctl 结构体布局/IQ 字段改名 EnvLv→CtrlData)。v6.x 为必要条件,充分性由逐来源冒烟判定。peyton-howe deb(v5.0x4.1)只作文件布局参考,不安装
- **获取顺序**:① OPi 官方 6.1 系镜像内提取(CSDN 同板实证路线,也顺带回答"官方镜像里到底有没有")→ ② FriendlyELEC 6.30.4 提取(同为 6.1 BSP 系)→ ③ JeffyCN mirror v6.x tag 源码编译;每个来源先做 glibc/加载冒烟再全装(SDK 新工具链产物在 Debian 上有 glibc 不匹配先例)
- **让渡 = 运行期探测,不做配置声明**:沿用仓库"运行时可变状态不入 config"惯例(先例:隐私走 PrivacyState、相机方向走 store meta);探测点在 open() 写 exposure 之前,探测失败按"未探测到"处理(保守回现状——让渡是画质增强,不是安全边界)
- **让渡语义覆盖两层**:停传感器曝光/增益写入(origin R6 明示)+ 跳过数字曝光补偿(计划期补充:否则 levels/gamma/WB 会在硬件 3A 之上二次处理,污染对比与画质);三帧择优保留(抗抖动与 3A 无冲突)
- **对比期间守护进程手动起停**:命令固化进 SETUP.md;生产态生命周期延后(见 Scope Boundaries)
- **阶段 A 参考抓帧用 v4l2-ctl 直抓**:零新依赖,SETUP §2.4 已有同款命令先例;GStreamer(mppjpegenc)不进本轮
- **IQ 文件命名先核对设备树**:从 /proc/device-tree 实测 module/lens 名;与 json 名(ov13855_CMK-OT2016-FV1_default)不一致时改 json 文件名去匹配,不动设备树
- **rkaiq 用线性模式**:避开 HDR 模式配置错误导致不出图的已知坑

---

## Open Questions

### Resolved During Planning

- origin R4 来源顺序 → 细化为上述三步获取顺序 + v6.x 门槛(研究结论,含出处见 External References)
- origin R5 IQ 文件来源 → ov13855_CMK-OT2016-FV1_default.json(Firefly isp3x / peyton-howe deb / OPi 新镜像三处可得);OV13850 改名冒充被实测否决
- origin R1 抓取工具 → v4l2-ctl 直抓(零依赖优先)
- origin R3 落档位置 → SETUP.md §2.4 带日期子段 + §2.10 新行 + 顺带刷新滞后的 M3 行

### Deferred to Implementation

- DT 实际 module/lens 名:板上导出 /proc/device-tree 后核对(U1 执行时一锤定音)
- 所选来源产物的 glibc 兼容性:每个来源冒烟后才全装(U2)
- AE 收敛时长(守护进程启动到出图稳定):U2 冒烟时实测并写入 SETUP.md(数值供 U3 流程使用)
- rkaiq_3A_server 实际 CPU 占用:U3 采样落档,作为生产态生命周期立项的输入
- 让渡模式下灰世界 WB 是否仍需要保留(若 rkaiq 输出色偏明显再议):U3 对比数据说话
- /proc/device-tree 若导不出 module/lens 字符串,IQ 文件名退回按传感器名 ov13855 匹配;两种结局都落档;若 IQ 匹配还解析 DT 内字符串导致重命名不够,按 R8 落档,不改设备树
- 守护进程是否需要 root、SETUP.md 启停命令的表达形式(sudo 前缀 vs root shell):U2 敲定
- U4 探测原语选型与 open() 探测超时预算:实现期定(与纯函数拆分一致)

---

## Implementation Units

### U1. 阶段 A — 板上基准对照验证与落档

**Goal:** 零新依赖钉死"用法是否正确",核对与对比材料按 SETUP.md 惯例落档;产出 B 阶段的干净基准。

**Requirements:** R1, R2, R3(origin F1;AE1、AE2)

**Dependencies:** 无

**Files:**
- Modify: `deploy/SETUP.md`(§2.4 追加带日期子段;§2.10 新行;刷新 M3 行为现行 ISP 主路径事实)

**Approach:**
- 核对项(命令与"期望"注释最终按板上实测确认,以 §2.4 既有格式落档):媒体拓扑(`media-ctl -p -d /dev/media1`——rkisp 拓扑所在,media0 仅 rkcif 侧;确认 rkcif→sditf→rkisp0-vir2→video11 链路并记录 vir2 的 stats/params 节点号)、传感器能力(`v4l2-ctl --list-formats-ext`)、/proc/device-tree 的 camera module/lens 名导出(IQ 文件名依据)、镜像内 rkaiq 组件查证(`find / -name '*rkaiq*'` 时 prune /proc /sys、`ls /etc/iqfiles`、/oem 分区)——只查证不安装
- 参考帧:v4l2-ctl 直抓"驱动原生"帧与现管线输出同场景各存一帧;记录亮度均值与锐度(拉普拉斯方差,可借 camera_isp 既有纯函数算)与主观差异描述
- 直抓前先停 LIRA(start-lira.sh pkill 风格,与 U2/U3 同一互斥约定);"现管线"一帧在管线会话(主循环或 dev console)运行时单独触发 capture 取得,随后停跑再直抓;该顺序写入 §2.4 落档步骤
- §2.4 新子段开头加显式 supersede 注记:上文 ⚠ 已知画质限制段与"留给 camera_rkisp.py 打通"所述为历史状态,HAL 现状以新子段及刷新后 M3 行为准
- 结论三选一落档:用法正确 / 发现偏差(描述+修正)/ 画质异常(描述)

**Test scenarios:**
- Test expectation: none — 文档与板上执行单元,无行为代码;各命令的期望结果直接写入 SETUP.md 行内注释

**Verification:**
- §2.4 新子段含全部核对项结果与同场景对比记录;§2.10 新增结论行;M3 行与 camera_isp 现状一致;"镜像内是否有 rkaiq"有明确查证记录

---

### U2. 阶段 B-1 — 获取并跑通 rkaiq v6.x + IQ 文件

**Goal:** 板上跑通 v6.x rkaiq_3A_server,配齐命名匹配的 OV13855 IQ 文件,守护进程手动起停命令固化。

**Requirements:** R4, R5(origin F2 前半;失败时转 R8 落档)

**Dependencies:** U1(需其设备树核对结果与镜像查证结论)

**Files:**
- Modify: `deploy/SETUP.md`(来源、版本、校验和、安装与启动/停止命令、冒烟记录;产物本体留在板上,不入库)

**Approach:**
- 按获取顺序尝试:① OPi 官方 6.1 系镜像提取 → ② FriendlyELEC 6.30.4 提取 → ③ JeffyCN v6.x tag 源码编译;每来源先 glibc/加载冒烟(ldd 依赖核验 + 短跑)再全装
- IQ:取 ov13855_CMK-OT2016-FV1_default.json,文件名按 U1 导出的 DT module/lens 名核对,不一致则重命名 json 适配;放入 /etc/iqfiles
- 线性模式配置;启动命令与 AE 收敛等待(实测"启动到出图稳定"时长)一并固化;版本记录避开 v5.0x1.3(多摄 bug)
- 冒烟/AE 验证期间按 pkill 风格停 LIRA 主循环(与 U1/U3 同一互斥约定)
- 验证 video11 格式固定性:守护进程运行下 get-fmt 仍为 2688x3136 NV12、单帧按该尺寸完整解析、LIRA 式连抓 3 帧成功(采集链路硬编码该尺寸并按字节断言)
- 验证守护进程附着 vir2 对应的 stats/params 节点(节点号以 U1 记录为准),防"绑错 vir 空转"
- 安装范围限定为守护进程二进制 + IQ 文件,不安装/不启用 init.d/systemd 自启单元(与手动起停范围一致)
- 提取路线:7z 解包镜像 rootfs 分区(Windows/WSL 皆可)或 WSL2 loop-mount,执行时按环境当场定;镜像用完即弃,产物只留板上;若 U1 查证发现板上已自带 rkaiq 组件,来源①改为就地取用

**Test scenarios:**
- Test expectation: none — 板上操作单元;成功判据 = 守护进程启动且 /dev/video11 出帧亮度随场景自动变化(AE 生效证据),判据与对应命令写入 SETUP.md

**Verification:**
- rkaiq_3A_server 在板上可启动,video11 输出帧亮度随场景变化;来源/版本/校验和/启停命令落档

---

### U3. 阶段 B-2 — 同场景对比与切换决策

**Goal:** 三路同场景对比(驱动原生 / 现管线软件 3A / rkaiq),按"明显更优才切换"判据决策并落档;失败则落档收工。

**Requirements:** R7, R8(origin F2 后半;AE4、AE5)

**Dependencies:** U2

**Files:**
- Modify: `deploy/SETUP.md`(对比记录 + 决策行;CPU 占用采样数据)

**Approach:**
- 按腿时序采集(避免互斥矛盾与双重控制):(1) 守护进程**停止**状态下,LIRA capture() 抓"现管线"帧 → pkill 停主循环 → v4l2-ctl 直抓"驱动原生"帧;(2) 之后**启动守护进程**再抓 rkaiq 帧。守护进程运行期间现行代码不得写传感器控制(现管线臂必须采完再启守护进程),否则两套 3A 互相污染、"明显更优"判据从采集一刻起不可信;三路帧 U3 当场同场景重抓,不复用 U1 留存帧;记录标注每路采集时的守护进程状态
- 指标:亮度均值/分布 + 锐度 + 主观观感 + 同文字样张过既有 OCR 引擎比对识别字数/置信度(测量不改管线);附加守护进程 CPU 占用采样(top/pidstat + 采样时长,对 ASR/OCR 共存预算的输入)
- 决策:rkaiq 明显更优 → 决策"切换",进入 U4;不明显更优或失败 → 决策"回退",失败/依据落档,现状即终态(U4 取消)。回退时确认守护进程已停止且无开机自启,SETUP.md 记录;回退后板上 rkaiq 产物保留(不启用)还是卸载,当场定并落档

**Test scenarios:**
- Test expectation: none — 决策与记录单元

**Verification:**
- §2.10 有明确决策行(切换/回退 + 依据);CPU 占用数据在档;若失败,失败原因在档

---

### U4. 让渡逻辑 — 探测式外部 3A 接管(条件单元)

**Goal:** `V4l2IspCamera` 探测到官方 3A 守护进程运行时自动让渡:不写传感器曝光/增益,跳过数字曝光补偿,输出 = 原始帧 + 方向变换 + JPEG 编码。

**Requirements:** R6(origin AE3)

**Dependencies:** U3 且决策为"切换"(若回退,本单元取消并在计划留痕)

**Files:**
- Modify: `device/lira/hal/board/camera_isp.py`
- Test: `device/tests/test_camera_isp_handover.py`(新建,纯函数 + 桩测试)

**Approach:**
- 探测点在 open() 写 exposure 之前;探测一次(open 时),v1 不追守护进程中途起停的动态性(重启主循环即重探)
- 让渡决策逻辑抽为纯函数(输入探测结果,输出行为开关),便于主机测试;探测失败按"未探测到"处理(保守回现状)
- 探测匹配 rkaiq_3A_server 与 ispserver 两个进程名(实际落地名以 U2 落档为准)
- 让渡硬前提:IQ 就位且 U2 冒烟通过——守护进程驻留 ≠ 3A 生效,IQ 缺失时探测阳性会静默出暗帧;不只依赖进程存在性
- 让渡生效时:open() 跳过 exposure 写入;capture 链路跳过 analogue_gain 自适应与全部 `_sensor_set` 调用、跳过 `_digital_exposure`;保留三帧择优、方向变换、JPEG 编码

**Execution note:** 先写失败的纯函数测试再实现(轻量 TDD;与仓库板级纯函数测试惯例一致)

**Patterns to follow:**
- 纯函数直接 import 测试:test_dev_console.py:651-665(`camera_isp._apply_orientation` 先例)
- 构造级装配测试(构造不触硬件):test_main_loop.py:412-429

**Test scenarios:**
- Happy path: 探测到守护进程 → 让渡开关闭合;open 不发曝光写入;capture 不发增益、不跑数字补偿;输出仍带方向变换与 JPEG 编码
- Happy path: Covers AE3. Given 守护进程在跑,when capture() 执行,then 全程无任何传感器控制写入(以桩记录子进程调用断言零次)
- Happy path: 未探测到守护进程 → 行为与现状逐项一致(曝光写入 + 自适应增益 + 数字补偿)
- Edge case: 探测调用失败/超时 → 按"未探测到"处理,不抛新异常
- Edge case: 让渡模式下三帧择优仍生效(择优保留是有意决策,防回归误删)

**Verification:**
- 主机 pytest 全绿(让渡/非让渡两模式行为均有断言);真板上(切换态)LIRA 拍摄观感与 U3 的 rkaiq 参考帧一致,无二次提亮痕迹

---

## System-Wide Impact

- **Interaction graph:** `V4l2IspCamera.capture()` 同时服务主循环阅读管线与开发者控制台视觉测试(ui/dev.py)——让渡逻辑对两者一致生效,无需分别改动
- **Error propagation:** 探测失败静默回现状,不新增异常面;HalError 语义不变
- **State lifecycle:** 现行为 open() 固定 exposure=3210;让渡后传感器状态归守护进程管,若守护进程中途退出,画面曝光停留在其最后设定——重启 LIRA 主循环即重探,bring-up 期可接受,生产态生命周期另行立项
- **API surface parity:** 无配置键新增、无公开 API 变化;hal.backend 选择逻辑与 mock 相机不动
- **Integration coverage:** 让渡模式 x 隐私联锁不受影响(控制台视觉测试在隐私 ON 时仍结构性拒绝,采集行为变化不绕过联锁)
- **Unchanged invariants:** `Camera` ABC 协议、mock 相机、装配选择(cfg.hal.backend)、隐私模型全部不变

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| rkaiq 版本与 6.1.99 内核不匹配(交叉组合失败先例) | v6.x 硬门槛 + 获取顺序按兼容证据排序;失败即 R8 落档收工 |
| 提取产物 glibc/依赖不兼容(SDK 工具链先例) | 每来源先冒烟(ldd + 短跑)再全装 |
| IQ 文件名与 DT 实际 module/lens 名不符 | U1 先导出 DT 实测;不一致重命名 json 适配,不动设备树 |
| HDR 模式配错不出图(已知坑) | 线性模式配置 |
| 守护进程常驻 CPU 挤压 ASR/OCR | U3 实测采样落档;生产态形态以此为据另行立项 |
| 对比时 video11 被主循环占用 | 对比 procedure 与主循环互斥(pkill 风格,写入 SETUP.md) |
| v5.0x1.3 多摄 bug | 版本记录明确避开 |
| 守护进程运行但 3A 未生效(IQ 缺失/命名不匹配)→ 让渡态静默暗帧 | 让渡以 U2 冒烟通过为硬前提;U4 探测不只依赖进程存在性 |

---

## Documentation / Operational Notes

- `deploy/SETUP.md` 是唯一落档载体:§2.4 带日期子段(过程)、§2.10(结论行)、M3 行刷新;不新增 systemd unit(本轮守护进程手动起停)
- 若 B 全线失败,SETUP.md 的失败记录即终态交付——防止后来者重复踩坑

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-10-04-camera-official-stack-requirements.md](../brainstorms/2026-10-04-camera-official-stack-requirements.md)
- 相关代码:`device/lira/hal/board/camera_isp.py`、`device/lira/hal/board/camera_raw.py`、`device/lira/main.py`、`deploy/SETUP.md`
- 外部出处:见 Context & Research 的 External References
