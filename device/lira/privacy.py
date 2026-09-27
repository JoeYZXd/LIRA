"""隐私模式一等状态（U7，R12/R17）。

隐私模式是全 app 广播的状态对象（计划 U7 Approach）：

    PrivacyState.set_enabled(on, source)
        ├─ 订阅者按注册顺序广播（装配层决定次序）：
        │    1. 音频门（PrivacyGatedSink 停止向 KWS/ASR 分发）
        │    2. 状态机唤醒门（wake_allowed 谓词，隐私期不可唤醒）
        │    3. TTS 后果播报（含麦克风已关提示，话术在 phrasebook）
        └─ LLM 层无需订阅：LlmClient 每次调用现查 `privacy` 谓词
           （U5 注入点，fail-closed，见 llm/client.py）。

通道纪律：
  - UI 开关是隐私状态的唯一切换通道，口令校验在 lira/ui/app.py
    完成，通过后才触碰本状态。

口令（R17/R30 本端应用）：设备本地口令仅用于 UI 隐私开关路径，
存本地库 meta 表的 scrypt 哈希（stdlib，无新依赖），**无默认值**——
首次设置前 UI 隐私路径一律引导到设置页（fail-closed）。

日志纪律：本模块只记事件类型（on/off、来源、帧数），绝不落任何
语音/文本内容。
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from lira.appliances.store import ApplianceStore

__all__ = [
    "PrivacyEvent",
    "PrivacySubscriber",
    "PrivacyState",
    "PrivacyGatedSink",
    "PassphraseVault",
    "attach_announcer",
]

logger = logging.getLogger(__name__)

#: 订阅者契约：接收新状态（True=隐私开启）。同步或 async 函数均可。
PrivacySubscriber = Callable[[bool], None | Awaitable[None]]


class AudioSink(Protocol):
    """与 lira.audio.mic.AudioSink 同形（避免运行期强耦合 import）。"""

    def feed(self, samples) -> None: ...


@dataclass(frozen=True)
class PrivacyEvent:
    """一次隐私状态变更的审计记录（只含元数据，不含任何用户内容）。"""

    enabled: bool
    source: str
    seq: int


class PrivacyState:
    """隐私模式状态对象 + 订阅者广播。

    用法（装配层）::

        privacy = PrivacyState()
        privacy.subscribe(audio_gate.apply)      # 先关麦
        privacy.subscribe(sm_wake_gate.apply)    # 再封唤醒
        privacy.subscribe(announcer)             # 最后播报后果
        llm = LlmClient(cfg, privacy=privacy)    # 谓词每调用现查
    """

    def __init__(self) -> None:
        self._on = False
        self._subscribers: list[PrivacySubscriber] = []
        self.events: list[PrivacyEvent] = []

    # ---------- 查询 ----------

    @property
    def is_on(self) -> bool:
        return self._on

    def __call__(self) -> bool:
        """谓词形态：可直接作为 LlmClient(privacy=...) 注入（U5 入口）。"""
        return self._on

    # ---------- 变更与广播 ----------

    def subscribe(self, subscriber: PrivacySubscriber) -> None:
        """注册订阅者；注册顺序即广播顺序。"""
        self._subscribers.append(subscriber)

    async def set_enabled(self, enabled: bool, *, source: str) -> bool:
        """设置隐私状态并广播。

        幂等：状态不变时不广播、不留事件（R30 同版本重复投递不重复播报的
        设备侧同源纪律）。返回是否有实际变更。
        """
        if enabled == self._on:
            return False
        self._on = enabled
        self.events.append(
            PrivacyEvent(enabled=enabled, source=source, seq=len(self.events))
        )
        logger.info("privacy event=%s source=%s", "on" if enabled else "off", source)
        for subscriber in self._subscribers:
            result = subscriber(enabled)
            if inspect.isawaitable(result):
                await result
        return True


class PrivacyGatedSink:
    """音频分发门（AudioSink 装饰器）：隐私 ON 时丢弃喂入，KWS/ASR 零输入。

    装配层把它插在 MicDistributor 与 KWS/ASR 流之间：关麦 = 不再有 waveform
    进入任何识别流（"KWS 不再命中" 由构造保证，不依赖 KWS 自身配合）。
    """

    def __init__(self, inner: AudioSink, privacy: PrivacyState) -> None:
        self._inner = inner
        self._privacy = privacy
        #: 隐私期丢弃的 PCM 样本数（测试/审计断言用，无内容）
        self.dropped_samples = 0

    def feed(self, samples) -> None:
        if self._privacy.is_on:
            self.dropped_samples += len(samples)
            return
        self._inner.feed(samples)

    def apply(self, enabled: bool) -> None:
        """作为 PrivacyState 订阅者：仅记事件（门逻辑在 feed 现查，无状态残留）。"""
        logger.info(
            "privacy audio gate %s", "closed（停止分发）" if enabled else "open（恢复分发）"
        )


class PassphraseVault:
    """设备本地口令（UI 隐私开关路径的唯一凭据）。

    - 哈希存储：stdlib `hashlib.scrypt`（n=2**14, r=8, p=1）+ 16 字节随机盐，
      常数时间比较；本地库只落哈希，泄露库文件不泄露口令。
    - 无默认值：`is_set()` 为 False 时 UI 隐私路径必须先走设置流程
      （fail-closed，计划 U7 Approach）。
    """

    META_KEY = "ui_passphrase_scrypt"
    MIN_LENGTH = 4
    _N, _R, _P, _DKLEN = 2**14, 8, 1, 32

    def __init__(self, store: ApplianceStore) -> None:
        self._store = store

    def is_set(self) -> bool:
        return self._store.get_meta(self.META_KEY) is not None

    def set(self, passphrase: str) -> None:
        """设置（或替换）口令。空/过短口令拒绝（无默认值纪律）。"""
        if len(passphrase) < self.MIN_LENGTH:
            raise ValueError(f"口令至少 {self.MIN_LENGTH} 个字符。")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            passphrase.encode("utf-8"), salt=salt, n=self._N, r=self._R, p=self._P,
            dklen=self._DKLEN,
        )
        stored = f"scrypt${self._N}${self._R}${self._P}${salt.hex()}${digest.hex()}"
        self._store.set_meta(self.META_KEY, stored)

    def verify(self, passphrase: str) -> bool:
        stored = self._store.get_meta(self.META_KEY)
        if stored is None:
            return False
        try:
            scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
            if scheme != "scrypt":
                return False
            digest = hashlib.scrypt(
                passphrase.encode("utf-8"),
                salt=bytes.fromhex(salt_hex),
                n=int(n), r=int(r), p=int(p),
                dklen=len(bytes.fromhex(digest_hex)),
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))


def attach_announcer(
    privacy: PrivacyState, speak: Callable[[str], None]
) -> Callable[[bool], None]:
    """把隐私切换的 TTS 后果播报挂为订阅者（装配层最后注册 → 最后播报）。"""

    from lira.dialog import phrasebook as pb

    def announce(enabled: bool) -> None:
        speak(pb.PRIVACY_ON if enabled else pb.PRIVACY_OFF)

    privacy.subscribe(announce)
    return announce
