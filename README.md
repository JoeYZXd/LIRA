# LIRA — 低视力智能阅读与家电助手

Orange Pi 5（RK3588S）上的老年人语音阅读 + 红外家电控制设备。本地优先：核心功能
（唤醒词 → ASR → 意图 → TTS / OCR 阅读 / 红外控制）断网可用；复杂文本经熔断降级
的远程 LLM（GLM OpenAI 兼容端点）转白话。配套子女管理后台（FastAPI + SQLite）。

- 需求权威：`docs/brainstorms/2026-09-26-lira-requirements.md`（R1–R32, AE1–AE7）
- 实施计划：`docs/plans/2026-09-26-001-feat-lira-phase1-device-backend-plan.md`

## 仓库结构

    device/     设备端 Python 包（lira/：config、hal、audio、dialog、vision、llm、appliances…）
    backend/    子女管理后台（FastAPI + SQLite + Jinja2/HTMX）
    assets/     mock 样张（mock_images/）与唤醒词拼音表（keywords/）
    deploy/     板上部署（DT overlay、systemd、SETUP.md — U10）
    models/     推理模型（脚本下载，gitignore）

## x86 开发快速开始（全 mock HAL）

```bash
cd device
python3 -m venv .venv
.venv/bin/pip install -e . dev    # 或 .venv/bin/pip install pytest pyyaml

# 装配校验（打印模块装配图后退出，不要求 api_key）
.venv/bin/python -m lira.main --dry-run

# 运行单元测试
.venv/bin/python -m pytest
```

## x86 主循环冒烟（mock HAL + 真实语音栈）

装配 `.[audio,ocr-x86,llm,ui,sync]` extras 并下载模型后，可在开发机上以 mock
外设跑通真实主循环（隐私门 → 路由分发 → 状态机 → TTS/阅读管线；mock 麦音 wav
播放完毕后自动退出）：

```bash
cd device
.venv/bin/pip install -e ".[audio,ocr-x86,llm,ui,sync]"
LIRA_LLM_API_KEY=sk-xxx .venv/bin/python -m lira.main   # 设备面板挂 0.0.0.0:8080
```

配置：`device/config.yaml`（默认值）+ 环境变量覆盖（`LIRA_LLM_BASE_URL` /
`LIRA_LLM_API_KEY` / `LIRA_LLM_MODEL` / `LIRA_HAL_BACKEND` / `LIRA_LOG_LEVEL`）。
真实运行必须提供 `LIRA_LLM_API_KEY`；`--dry-run` 豁免。

## 模型下载

```bash
python3 models/download_models.py          # sherpa-onnx KWS/ASR/TTS + PP-OCRv4 → models/
python3 models/download_models.py --only kws
```

## 测试

```bash
cd device && .venv/bin/python -m pytest       # 单元测试
cd backend && python3 -m pytest               # 后台测试（U8 起）
```

## 端到端验收（U9：AE1–AE7，模拟环境）

`device/tests/e2e/` 在 x86 开发机上跑通全部验收样例：整机以全 mock HAL 装配
（麦克风 → wav 注入、LLM → stub、TTS → 录音假件），AE5/AE7 与对抗用例在
同进程内拉起**真实后台**（FastAPI TestClient）+ 真实设备端 `SyncClient`。

```bash
# 0) 模型就绪（真实 KWS/ASR 用例需要；缺失时相应用例自动 skip，文本注入已覆盖逻辑）
python3 models/download_models.py

cd device
.venv/bin/pip install -e ".[dev,audio,ocr-x86,llm,ui]"

# 1) 一次性生成音频夹具（离线 TTS 合成唤醒词/指令 wav，随仓库提交；缺失时可再生）
.venv/bin/python tests/e2e/make_audio_fixtures.py

# 2) 一键跑全部端到端验收（37 个用例）
.venv/bin/python -m pytest tests/e2e/ -q
```

文件布局：`conftest.py`（全 mock 装配 + SyncPump 帧泵）、`harness.py`（整机
DeviceHarness）、`test_ae1_privacy.py` … `test_ae7_sync.py`（每个 AE 一个文件）、
`test_sync_adversarial.py`（旧 epoch 回放 / 重复投递 / 应用中崩溃 / 库重建换新 /
多次离线变更）、`audio_fixtures/`（TTS 合成 wav）、`make_audio_fixtures.py`。

