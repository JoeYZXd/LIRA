# 默写准备自动启动电视接收应用

用户已确认修改：仅发起默写时，LIRA通过现有小米网络控制接口6095启动接收应用；普通状态查询保持只读。经典链路不改。

## 实现

- `device/lira/tv_display.py`：新增ensure_ready，已就绪直接返回；已知关屏不启动；接收端离线或亮屏但未在前台时，最多发一次startapp，再轮询鉴权接收端确认。总等待15秒，可取消；不发ADB、IR或电源指令。鉴权错误不走启动分支。厂商响应不被当作接收端就绪证明。
- `device/lira/tv_voice.py`：新增只供显式任务准备使用的接缝；不改普通probe。
- `device/lira/dictation.py`：check_dictation_tv接入显式准备，不改变选课、教材核对、播报或下一条规则。
- 厂商接口不携带接收端配对密钥。

## 证据

- 新增readiness测试在实现前6项失败，原因为缺少ensure_ready。
- 部署候选在LIRA的Python3.11及已部署依赖上77项全部通过，范围为tv_display、tv_voice、dictation。覆盖启动、只读查询、关屏、已就绪、鉴权错误、失败、取消及工具装配。
- 工作区同范围有一项先前U12未部署改动引起的失败：DictationRepository.revoke_owner('member-a')遭遇INTEGER PRIMARY KEY datatype mismatch。这不经过本次修改路径；部署候选保留原生产conversation_paths，全部通过。该问题须在后续U12集成修正，本次未部署账本或身份遗忘代码。
- 三个生产源文件部署前与本地原始文件逐一完全一致；只替换这三个文件，已有备份。其他源文件哈希和原有环境变量不变，无APK升级。LIRA服务正常恢复，NRestarts=0。
- 真机先由ADB强制停止接收应用，再从LIRA仅通过HTTP调用真实check_dictation_tv。只读probe先返回unavailable且无厂商请求；随后任务检测发送一次6095/startapp，40100鉴权状态确认interactive=true、foreground=true，耗时0.517秒。之后普通probe没有重复启动。
- 真机审计脚本首次在观察器的headers=None处理处异常，厂商请求已实际发送；修正观察器后重新强制停止应用，从相同初态复测通过。不是业务源码修复失败。
- 未运行完整云端第8课流程，未把本次连接检查当作整任务或课级计数验收。

原始结果：`2026-10-21-tv-autolaunch-live.json`，部署回执：`2026-10-21-tv-autolaunch-deployment.json`。
