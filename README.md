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
