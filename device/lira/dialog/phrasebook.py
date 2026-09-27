"""语音话术集中管理（U3）：所有 TTS 播报文案唯一出处，便于统一审校。

纪律来源：
  - R29：家电指令只能陈述"指令已发出"，不得暗示家电真实状态（红外单向）；
  - R3/R27：医疗/降级内容必须附带免责提示；
  - R24：识别失败必须说出误解内容并给示例引导，绝不静默丢弃；
  - R28：远程处理 >2s 时的等待反馈；
  - 措辞面向老年人：短句、无术语、友好。

模板字段用 str.format 填充；文案修改不改动状态机代码。
"""

from __future__ import annotations

from typing import Sequence

__all__ = [
    "WAKE_ACK",
    "LISTEN_REMIND",
    "LISTEN_GOODBYE",
    "SESSION_CANCELLED",
    "GUIDANCE_RETRY",
    "GUIDANCE_GIVEUP",
    "EXAMPLE_COMMAND",
    "HELP_TEXT",
    "CONFIRM_PROMPT",
    "CONFIRM_RESTATE",
    "CONFIRM_CANCELLED",
    "IR_SENT",
    "DEVICE_DISABLED",
    "APPLIANCE_AMBIGUOUS",
    "APPLIANCE_NOT_CONFIGURED",
    "UNAVAILABLE_REMOTE",
    "WAIT_REMOTE",
    "DISCLAIMER_MEDICAL",
    "DISCLAIMER_OFFLINE",
    "OCR_GUIDANCE",
    "OCR_GIVEUP",
    "CAPTURE_GUIDANCE",
    "PRIVACY_ON",
    "PRIVACY_OFF",
    "ir_sent_text",
    "confirm_prompt_text",
    "guidance_retry_text",
    "disabled_announce_text",
]

# ---------- 唤醒与聆听（R25/R20） ----------

WAKE_ACK = "我在听。"
LISTEN_REMIND = "请说出您的指令，比如说：打开台灯。"
LISTEN_GOODBYE = "好的，想用我的时候，叫我小丽拉。"
SESSION_CANCELLED = "好的，已取消。"

# ---------- 识别失败引导（R24） ----------

EXAMPLE_COMMAND = "打开台灯"
GUIDANCE_RETRY = "我没有听清您说的“{misheard}”。您可以试试说：{example}"
GUIDANCE_GIVEUP = "真不好意思，我没听明白。您休息一下，需要时再叫我。"


def guidance_retry_text(misheard: str) -> str:
    return GUIDANCE_RETRY.format(misheard=misheard, example=EXAMPLE_COMMAND)


# ---------- 能力清单（R31） ----------

HELP_TEXT = (
    "我能帮您读报纸、读信、读说明书，"
    "也能控制电视、空调这些家电。"
    "您可以说：帮我读一下这个。"
)

# ---------- 高危确认（R23） ----------

CONFIRM_PROMPT = "您是要{desc}吗？确认请说：确认。"
CONFIRM_RESTATE = "我没有听明白。请说“确认”或者“不要”。"
CONFIRM_CANCELLED = "好的，没有执行。"


def confirm_prompt_text(desc: str) -> str:
    return CONFIRM_PROMPT.format(desc=desc)


# ---------- 家电控制（R29/R7 措辞纪律） ----------

IR_SENT = "好的，{desc}指令已发出。"
DEVICE_DISABLED = "这个设备已被家人禁用，暂时不能操作。"
# G12（U6 补）：别名命中多台设备，不猜，请老人说清楚
APPLIANCE_AMBIGUOUS = "有好几个设备都能这样叫。请说清楚是哪一台，比如说：打开客厅台灯。"
# U6 补：指令指向未配置设备，引导找家人在后台添加
APPLIANCE_NOT_CONFIGURED = "这个家电我还不认识。请家人在后台添加配置后就能用了。"


def ir_sent_text(desc: str) -> str:
    """R29：只陈述"指令已发出"，不得暗示家电真实状态。"""
    return IR_SENT.format(desc=desc)


#: 快照推送后被禁用的家电播报（AE7/R7：家人改配置立即生效的口语化通知）
DISABLED_ANNOUNCE = "{names}已被家人禁用，暂时不能用了。"


def disabled_announce_text(names: Sequence[str]) -> str:
    """被禁用家电名单的播报文案（names 为空返回空串=无需播报）。"""
    if not names:
        return ""
    return DISABLED_ANNOUNCE.format(names="、".join(names))


# ---------- LLM 路由与降级（R28/R27/R3） ----------

UNAVAILABLE_REMOTE = "远程服务暂时连不上，这个任务我暂时帮不了。"
WAIT_REMOTE = "正在为您仔细阅读，请稍等。"
DISCLAIMER_MEDICAL = "以上内容由机器转述，请遵医嘱，以原说明书为准。"
DISCLAIMER_OFFLINE = "当前没有网络，我按原文读给您听。"

# ---------- 拍摄与 OCR 引导（R26/R4，U4 使用，话术集中在此） ----------

OCR_GUIDANCE = "我没有看清，请把材料放平，再靠近一点。"
OCR_GIVEUP = "我还是没看清。请找家人帮忙，或者稍后再试。"
CAPTURE_GUIDANCE = "正在为您拍摄，请把材料放平，不要遮挡镜头。"

# ---------- 隐私模式后果播报（R12，U7 使用；含麦克风已关提示） ----------

PRIVACY_ON = "好的，隐私模式已开启。麦克风已经关闭，我不会再听您说话，也不会把任何内容发到网上。想让我继续工作，请让家人在手机上打开设备管理页面关闭隐私模式。"
PRIVACY_OFF = "隐私模式已关闭，麦克风恢复了。您可以随时叫我小丽拉。"
