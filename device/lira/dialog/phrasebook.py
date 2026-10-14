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

from lira.dialog.context import StatusFacts

__all__ = [
    "WAKE_ACK",
    "INPUT_NOT_HEARD",
    "UNDERSTANDING_FAILED",
    "PLAYBACK_FAILED",
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
    "OCR_COMPLETE",
    "PRIVACY_ON",
    "PRIVACY_OFF",
    "SHUTDOWN_FAREWELL",
    "POWEROFF_FAILED",
    "DND_EXIT_ACK",
    "DND_STATUS_ON",
    "DND_STATUS_OFF",
    "DND_ESCAPE_PROMPT",
    "DND_EXEC_PROMPT",
    "dnd_escape_prompt_text",
    "dnd_exec_prompt_text",
    "VOLUME_ACK",
    "status_report_text",
    "ir_sent_text",
    "confirm_prompt_text",
    "guidance_retry_text",
    "disabled_announce_text",
    "CHAT_PRIVACY",
    "CHAT_UNAVAILABLE",
    "CHAT_BUSY",
    "CHAT_FAILED",
    "CHAT_MEMBER_GONE",
    "CHAT_TOO_LONG",
    "CHAT_EMPTY",
    "CHAT_GREETING",
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
OCR_COMPLETE = "识别已完成，可以把书或材料从摄像头前移开了。我现在读给您听。"

# ---------- 隐私模式后果播报（R12，U7 使用；含麦克风已关提示） ----------

PRIVACY_ON = "好的，隐私模式已开启。麦克风已经关闭，我不会再听您说话，也不会把任何内容发到网上。想让我继续工作，请让家人在手机上打开设备管理页面关闭隐私模式。"
PRIVACY_OFF = "隐私模式已关闭，麦克风恢复了。您可以随时叫我小丽拉。"

# ---------- 真关机（R4，2026-10-06 计划 U4） ----------

SHUTDOWN_FAREWELL = "好的，我休息了。您也早点休息，再见。"
#: R4 失败反馈（M7 残留 R1 模式防线：不可逆动作失败必须可感知，不得只进日志）
POWEROFF_FAILED = "不好意思，我没有关掉。请让家人看看设备，或者再叫我试一次。"

# ---------- 勿扰模式（R8-R12，2026-10-06 计划 U5） ----------
#
# 进入勿扰零提示音（R8 有意设计）；以下仅豁免集话术。

DND_EXIT_ACK = "好的，已退出勿扰模式。"
DND_STATUS_ON = "是的，我在勿扰模式，先不说话。您说「退出勿扰模式」就能叫醒我。"
DND_STATUS_OFF = "没有，我正常听着呢，随时可以吩咐。"

#: 三次逃生舱（R10，2026-10-06 计划 U6）：主动开口询问是否退出
DND_ESCAPE_PROMPT = "您连着说了几遍「{desc}」。我在勿扰模式，先不应答。要退出勿扰吗？确认请说：确认。"
#: 退出后的执行问句（低危；高危走具名确认话术 confirm_prompt_text）
DND_EXEC_PROMPT = "要现在为您{desc}吗？确认请说：确认。"


def dnd_exec_prompt_text(desc: str) -> str:
    return DND_EXEC_PROMPT.format(desc=desc)

#: R16 全局音量调整确认（以调整后的音量播出，自然演示效果）
VOLUME_ACK = "好的。"

# ---------- 状态自查（R17，2026-10-06 计划 U9） ----------
#
# 措辞纪律：老人能懂的话，不报技术细节（IP/术语）；事实缺失降级表述不阻塞播报。

STATUS_NETWORK_OK = "网络正常，能连上网。"
STATUS_NETWORK_BAD = "现在连不上网。"
STATUS_NETWORK_UNKNOWN = "网络情况我一下子说不准。"
STATUS_CLOUD_OK = "云端服务也连着。"
STATUS_CLOUD_BAD = "云端服务暂时连不上，读书认字不受影响。"
STATUS_MIC_OFF = "麦克风已经关闭，听不见您说话。"


def status_report_text(facts: StatusFacts) -> str:
    """状态自查播报文案。facts 键：network/cloud/mic_on（True/False/None）。"""
    parts = []
    network = facts.get("network")
    if network is True:
        parts.append(STATUS_NETWORK_OK)
    elif network is False:
        parts.append(STATUS_NETWORK_BAD)
    else:
        parts.append(STATUS_NETWORK_UNKNOWN)
    cloud = facts.get("cloud")
    if cloud is True:
        parts.append(STATUS_CLOUD_OK)
    elif cloud is False:
        parts.append(STATUS_CLOUD_BAD)
    if facts.get("mic_on") is False:
        parts.append(STATUS_MIC_OFF)
    return "".join(parts)


def dnd_escape_prompt_text(desc: str) -> str:
    return DND_ESCAPE_PROMPT.format(desc=desc)

INPUT_NOT_HEARD = "收音没有完成，请再说一遍。"
UNDERSTANDING_FAILED = "理解指令时出了问题，请再说一遍。"
PLAYBACK_FAILED = "回答没有播完，您可以再问一次。"

# ---------- 文字聊天降级文案（home-console 计划 U2，R8/R10） ----------
#
# 措辞纪律：
#  - 隐私态只断言"未发送至远程/云端"，不得断言"未离开设备"——局域网明文
#    HTTP 与本地存储使更强承诺不实（沿 client.py 的措辞精度）；
#  - 降级文案一律不回显消息内容（R10 内容纪律的文案侧）；
#  - 面向家人：短句、友好、给出下一步（重试/找家人）。

CHAT_PRIVACY = "现在是隐私模式，这句话没有发送到远程或云端，我也暂时不能回话。想让小丽拉继续聊，请家人在设备管理页面关闭隐私模式。"
CHAT_UNAVAILABLE = "网络或远程服务暂时不可用，这句话没有发出去。请稍后再发一次。"
CHAT_BUSY = "小丽拉正在回另一条消息，请稍等几秒再发。"
CHAT_FAILED = "这次没有聊成，回复没有保存。请再发一次试试。"
CHAT_MEMBER_GONE = "这位家人的会话已经不存在了，请回到选人页重新进入。"
CHAT_TOO_LONG = "这条消息超过 2000 字了，请拆成几条发给我。"
CHAT_EMPTY = "输入框还是空的，想说什么就打出来发给我吧。"
#: 空会话引导语（U3 会话页零消息态渲染；会话历史为空时不显示"加载更多"）
CHAT_GREETING = "还没聊过天呢。在下面的输入框打句话，发给小丽拉吧。"
