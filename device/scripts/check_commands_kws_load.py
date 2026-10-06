#!/usr/bin/env python
"""一次性验证：真实 sherpa-onnx KeywordSpotter 能加载 commands.txt（token 无 OOV）。

加载成功 = 手写拼音 token 全部有效；失败（exit -1 / 异常）= 有 token 不在
tokens.txt，需要修正 commands.txt 的拼音行。
"""
import sys
from pathlib import Path

sys.path.insert(0, "/mnt/d/Projects/LIRA/device")

from lira.audio._paths import KWS_MODEL_DIR  # noqa: E402
from lira.audio.kws import WakeWordKws  # noqa: E402
from lira.dialog.intents import COMMANDS_KEYWORDS_FILE  # noqa: E402

kws = WakeWordKws(keywords_file=COMMANDS_KEYWORDS_FILE, model_dir=KWS_MODEL_DIR)
print("LOAD OK:", COMMANDS_KEYWORDS_FILE.name)
stream = kws.create_stream()
print("STREAM OK")
