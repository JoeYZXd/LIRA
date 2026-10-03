"""LIRA 设备端 asyncio 入口：装配各模块并驱动主循环（M7 接线）。

主循环 = e2e ``DeviceHarness``（U9 已验证装配图）的生产形态：

    HAL 音频编排（16kHz mono int16 -> float32）
      -> PrivacyGatedSink（隐私门，R12 fail-closed）
        -> 路由分发（状态机统一切换，R21 三选二）:
            WAKE     -> 主唤醒 KWS -> engine.on_wake()
            LISTEN   -> 流式 ASR -> endpoint 切句 -> engine.on_asr_text()
            PLAYBACK -> 白名单 KWS -> engine.on_asr_text()
    DialogEngine.tick（单调时钟：LISTENING 8s / CONFIRMING 10s 超时）
    speak            -> TtsEngine（音量/语速读 DeviceSettings，R28 等待反馈挂 LLM）
    start_capture    -> 任务句柄返回状态机（F1 取消依据）
    send_ir          -> IRService.send_ir(confirmed=True)（第二道安全闸）
    播放控制六方法    -> ReadingPipeline 播放控制入口

伴随任务：同步监督循环（断线指数退避重连）与设备 Web UI（uvicorn in-process，
UiServices 注入同一批状态对象）。--dry-run 仅读配置与模型文件探测，不构造任何
引擎（README 承诺：仅 PyYAML 的环境可跑装配校验）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass

from lira.appliances import IRService, LearningManager
from lira.audio._paths import (
    ASR_MODEL_DIR,
    KWS_MODEL_DIR,
    MODELS_DIR,
    TTS_MODEL_DIR,
    VOCODER_ONNX,
    WAKEWORD_KEYWORDS_FILE,
)
from lira.config import AppConfig, ConfigError, load_config
from lira.dialog import phrasebook as pb
from lira.dialog.intents import PLAYBACK_WHITELIST_KEYWORDS_FILE
from lira.dialog.route_dispatch import RouteDispatch
from lira.dialog.router import Router
from lira.dialog.state_machine import AudioRoute, DialogEngine
from lira.hal.base import HalError
from lira.hal.mock import (
    IMAGE_SUFFIXES,
    MockAudioIO,
    MockCamera,
    MockDisplay,
    MockIrController,
)
from lira.privacy import (
    PassphraseVault,
    PrivacyGatedSink,
    PrivacyState,
    attach_announcer,
)
from lira.llm.client import make_text_polisher
from lira.sync import SyncClient, make_snapshot_announcer
from lira.sync_ws import WsSyncTransport
from lira.vision.reading import ReadingPipeline, TtsBlockSpeaker

__all__ = [
    "AudioStack",
    "DeviceRuntime",
    "build_audio_stack",
    "build_board_hal",
    "build_mock_hal",
    "main",
]

#: 唤醒词回退值（词表读取失败时；正常路径从拼音表首行解析）
WAKEWORD_FALLBACK = "小丽拉"


class RingBufferHandler(logging.Handler):
    """进程内环形日志缓冲（M8 计划 R2）：dev 控制台日志视图的数据源。

    挂根 logger 收集全量；内容纪律由各模块的日志纪律保证（只记事件与耗时，
    识别文本/播报内容不落日志——本 handler 不做二次过滤）。
    """

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._records: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._records.append(self.format(record))
        except Exception:  # noqa: BLE001 - 日志格式化失败不拖垮业务
            pass

    def snapshot(self) -> list[str]:
        return list(self._records)


#: 进程级单例（runtime 初始化时幂等挂载；dev 控制台读取）
RING_LOG_HANDLER = RingBufferHandler()


def ensure_ring_handler() -> None:
    """把环形日志 handler 幂等挂到根 logger（runtime 初始化与测试共用）。

    同时把根级别下探到 INFO（否则默认 WARNING 会让事件类 INFO 记录进不了
    缓冲；生产 main() 本就 basicConfig(INFO)，此下探对齐 dev/测试环境）。
    """
    root = logging.getLogger()
    if RING_LOG_HANDLER not in root.handlers:
        root.addHandler(RING_LOG_HANDLER)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

#: 同步重连退避 (秒)：指数退避边界；健康会话时长阈值（短命会话不重置退避）
SYNC_RECONNECT_MIN_SECONDS = 2.0
SYNC_RECONNECT_MAX_SECONDS = 60.0
SYNC_HEALTHY_SESSION_SECONDS = 60.0

#: 网络探测周期（秒）：后台伴随任务节奏，probe() 同步读缓存
NETWORK_PROBE_INTERVAL_SECONDS = 10.0


def _hal_device(value: str) -> int | str | None:
    """配置设备号 -> sounddevice device 参数：空串=None（系统默认），纯数字=序号，其余=名称子串。"""
    if not value:
        return None
    return int(value) if value.isdigit() else value


def _wakeword_text() -> str:
    """唤醒词原文（KWS 命中结果含关键词原文，比对用）：读拼音表首行，失败回退默认词。

    唤醒词由拼音表驱动（改词零成本），主循环不硬编码；解析失败回退并告警
    （词表与比对逻辑漂移时唤醒会失效，宁可保守提示）。
    """
    try:
        first = WAKEWORD_KEYWORDS_FILE.read_text(encoding="utf-8").splitlines()[0]
        word = first.split("@")[-1].strip()  # 词表格式: 拼音 @中文词（KWS 结果含中文词）
    except (OSError, IndexError):
        word = None
    if not word:
        logging.warning("唤醒词表读取失败，回退默认唤醒词 %r", WAKEWORD_FALLBACK)
        return WAKEWORD_FALLBACK
    return word


# ---------- HAL 装配（按 hal.backend 选择） ----------


def build_mock_hal(cfg: AppConfig) -> dict[str, object]:
    """按配置实例化全 mock HAL（x86 开发路径：文件相机/文件音频/日志 IR）。"""
    if cfg.hal.backend != "mock":
        raise ConfigError(f"hal.backend={cfg.hal.backend!r} 应走 build_board_hal（board）。")
    camera = MockCamera(cfg.hal.mock_images_dir)
    audio = MockAudioIO(cfg.hal.mock_audio_dir / "sample_16k.wav")
    ir = MockIrController()
    display = MockDisplay()
    return {"camera": camera, "audio": audio, "ir": ir, "display": display}


def build_board_hal(cfg: AppConfig) -> dict[str, object]:
    """板上真实 HAL（U10/M7）。

    - Mic = SoundDeviceMic（M4：FY-SP003U），Speaker = TTS 播放器（M6：ES8388，见
      build_audio_stack）。
    - Camera = V4l2RawCamera（M3：OV13855@CAM1 裸 Bayer 路径）。
    - Display 无触摸屏 -> MockDisplay（no-op 语义一致）。
    - IR 待 M5（BroadLink RM4 Mini 到货接 ``ir_broadlink``，传输层替换，链路不变），
      当前以 MockIrController 装配并明确告警：发射只记录、不生效。
    """
    from lira.audio.mic import SoundDeviceMic
    from lira.hal.board import V4l2RawCamera
    from lira.hal.board.camera_raw import DEFAULT_DEVICE

    mic = SoundDeviceMic(device=_hal_device(cfg.hal.mic_device))
    camera = V4l2RawCamera(device=cfg.hal.camera_device or DEFAULT_DEVICE)
    ir = MockIrController()
    logging.warning(
        "IR 板上实现待 M5（BroadLink RM4 Mini）：当前装配 MockIrController，"
        "红外发射只记录不生效（家电控制不可用）"
    )
    return {"camera": camera, "audio": mic, "ir": ir, "display": MockDisplay()}


# ---------- 引擎装配（依赖/模型缺失 -> None + 日志指引；dry-run 不构造） ----------


@dataclass
class AudioStack:
    """sherpa 语音栈：双 KWS + 流式 ASR + TTS（+ 板上播放器）。"""

    wake_kws: object  # WakeWordKws
    asr: object  # StreamingAsr
    playback_kws: object  # WakeWordKws（白名单）
    tts: object  # TtsEngine（board 后端时其内部持有 SoundDevicePlayer）


def build_audio_stack(cfg: AppConfig) -> AudioStack | None:
    """sherpa 语音栈装配。依赖/模型缺失返回 None（带日志指引），不抛异常。"""
    try:
        from lira.audio.asr import StreamingAsr
        from lira.audio.kws import WakeWordKws
        from lira.audio.tts import TtsEngine

        _ = PLAYBACK_WHITELIST_KEYWORDS_FILE  # intents 模块可用性一并确认
    except ImportError as exc:
        logging.warning("音频依赖未安装: %s（pip install -e '.[audio]'）", exc)
        return None
    try:
        # 播放器先建（board 后端），采样率在 TTS 引擎加载后回填（模型采样率权威）
        player = None
        if cfg.hal.backend == "board":
            from lira.hal.board.audio import SoundDevicePlayer

            player = SoundDevicePlayer(device=_hal_device(cfg.hal.speaker_device))
        tts = TtsEngine(player=player)
        if player is not None:
            player.samplerate = tts.sample_rate
        return AudioStack(
            wake_kws=WakeWordKws(),
            asr=StreamingAsr(),
            playback_kws=WakeWordKws(keywords_file=PLAYBACK_WHITELIST_KEYWORDS_FILE),
            tts=tts,
        )
    except FileNotFoundError as exc:
        logging.warning("语音栈模型未就绪: %s", exc)
        return None


def build_ocr(cfg: AppConfig):
    """OCR 引擎按后端选择：board -> RKNNLite（det core0/rec core1）；
    mock -> onnxruntime（x86 开发）。模型/依赖缺失抛 ConfigError（修复指引按因分叉）。"""
    if cfg.hal.backend == "board":
        from lira.vision.ocr_rknn import RknnOcrEngine

        factory = RknnOcrEngine
        model_hint = "OCR .rknn 模型缺失。请确认 models/ 下 ppocrv4_det.rknn 与 ppocrv4_rec.rknn（x86 转换产物已拷入）。"
        dep_hint = "OCR 板上推理依赖未安装（pip install rknn-toolkit-lite2，见 SETUP.md 2.1/2.8）。"
    else:
        from lira.vision.ocr_x86 import OnnxOcrEngine

        factory = OnnxOcrEngine
        model_hint = "OCR ONNX 模型缺失。请运行: python3 models/download_models.py"
        dep_hint = "OCR x86 依赖未安装（pip install -e '.[ocr-x86]'）。"
    try:
        return factory()
    except FileNotFoundError as exc:
        raise ConfigError(f"{model_hint}（原因: {exc}）") from exc
    except ImportError as exc:
        raise ConfigError(f"{dep_hint}（原因: {exc}）") from exc


def _make_network_probe(base_url: str, interval_seconds: float = 10.0):
    """R10 基础联网检测：后台伴随任务周期探测（to_thread），调用方同步读缓存。

    socket.create_connection 是阻塞调用（2s 超时 + 隐含 DNS 解析），绝不能在
    事件循环线程执行——否则断网期间每 10s 卡住音频编排/状态机 tick/UI 长达
    2s（评审 P1，correctness/adversarial/performance/python 四方共识）。
    """

    def probe_sync() -> bool:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return True
        except OSError:
            return False

    return probe_sync


class _NetworkProbe:
    """探测结果缓存 + 后台刷新器：probe() 同步读最近一次结果（不阻塞）。"""

    def __init__(self, probe_sync, interval_seconds: float) -> None:
        self._probe_sync = probe_sync
        self._interval = interval_seconds
        self._next_refresh = -1e9
        self.ok = False

    def __call__(self) -> bool:
        return self.ok

    async def run(self, stop: asyncio.Event) -> None:
        """周期探测（to_thread），直至 stop。启动时先探一次。"""
        while True:
            self.ok = await asyncio.to_thread(self._probe_sync)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue


def build_llm(cfg: AppConfig, *, privacy: PrivacyState, network_ok, on_wait_feedback):
    """LlmClient 装配：隐私谓词注入（fail-closed）+ R10 单一远程可用性信号。
    依赖缺失抛 ConfigError（含修复指引）。"""
    try:
        from lira.llm.client import LlmClient
    except ImportError as exc:
        raise ConfigError(f"LLM 依赖未安装（pip install -e '.[llm]'）: {exc}") from exc
    return LlmClient(
        cfg.llm,
        privacy=lambda: privacy.is_on,
        network_ok=network_ok,
        on_wait_feedback=on_wait_feedback,
    )


# ---------- 状态机回调装配（DialogCallbacks 实现） ----------


class DeviceCallbacks:
    """同步回调面：播报走 TTS，拍摄/红外/播放控制委托真实组件。

    与 e2e harness 的 _Callbacks 同构；差异点：
    - speak: TtsEngine（音量/语速读 DeviceSettings；无语音栈时降级日志）。
    - start_capture: 返回任务句柄给状态机（F1 取消依据，harness 未做）。
    """

    def __init__(self, runtime: "DeviceRuntime") -> None:
        self._rt = runtime

    def speak(self, text: str) -> None:
        # 最近播报环形记录（内存，不落盘；dev 回放/状态区展示面）
        self._rt.recent_speaks.append(text)
        tts = self._rt.tts
        if tts is None:
            # 日志纪律：只记事件与字数，话术文本（可含 LLM 回复/误听内容）不落日志
            logging.info("播报降级 event=spoken_no_tts chars=%d", len(text))
            return
        tts.speak(
            text,
            speed=self._rt.settings.tts_speed,
            volume=self._rt.settings.volume,
        )

    def start_listening(self) -> None:
        if self._rt.asr_stream is not None:
            self._rt.asr_stream.reset()

    def set_audio_route(self, route: AudioRoute) -> None:
        self._rt.route = route

    def start_capture(self) -> asyncio.Task:
        # 新阅读会话以全局音量/语速起步（会话内"大声点/小声点"再由 R21 调整）
        speaker = self._rt.speaker
        speaker.volume = self._rt.settings.volume
        speaker.speed = self._rt.settings.tts_speed
        return self._rt.spawn(self._rt.pipeline.run_capture())

    def send_ir(self, device: str, action: str) -> None:
        # 状态机已完成高危确认 -> 装配层以 confirmed=True 过第二道闸
        # （第二道闸仍独立校验 enabled/动作/码值，防御直调）
        self._rt.spawn(self._send_ir(device, action))

    async def _send_ir(self, device: str, action: str) -> None:
        try:
            await self._rt.ir_service.send_ir(device, action, confirmed=True)
            logging.info("IR 已发射 device=%s action=%s", device, action)
        except Exception as exc:  # noqa: BLE001 - IR 故障不拖垮主循环
            logging.warning("IR 发送失败 device=%s action=%s kind=%s", device, action,
                            getattr(exc, "kind", type(exc).__name__))

    def playback_pause(self) -> None:
        self._rt.pipeline.playback_pause()

    def playback_resume(self) -> None:
        self._rt.pipeline.playback_resume()

    def playback_stop(self) -> None:
        self._rt.pipeline.playback_stop()

    def playback_read_again(self) -> None:
        self._rt.pipeline.playback_read_again()

    def playback_volume_up(self) -> None:
        self._rt.pipeline.playback_volume_up()

    def playback_volume_down(self) -> None:
        self._rt.pipeline.playback_volume_down()


# ---------- 整机运行时 ----------


class DeviceRuntime:
    """生产主循环：组件装配 + 编排器 + 伴随任务（sync/UI）+ 优雅停机。

    Args 与 e2e DeviceHarness 同构，但组件为生产实现；audio/ocr/llm 均可注入
    替身供单测（None 语义见各字段）。
    """

    def __init__(
        self,
        *,
        cfg: AppConfig,
        hal: dict[str, object],
        store,
        settings,
        audio: AudioStack,
        ocr=None,
        llm=None,
        privacy: PrivacyState | None = None,
        speaker=None,
        sync_transport_factory=WsSyncTransport,
        clock=time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._hal = hal
        self._clock = clock
        self._sync_transport_factory = sync_transport_factory
        self._network_ok = _NetworkProbe(
            _make_network_probe(cfg.llm.base_url), NETWORK_PROBE_INTERVAL_SECONDS
        )
        self._stop = asyncio.Event()
        self._pending: set[asyncio.Task] = set()
        self._companions: set[asyncio.Task] = set()
        self._ui_server = None
        self._ui_task = None
        self._vault = PassphraseVault(store)
        #: 公开别名（dev 控制台等装配面使用）
        self.vault = self._vault
        #: 同步会话状态（_run_sync 维护；R1 状态面 + dev 状态区数据源）
        self.sync_status: dict[str, object] = {"session": "not_configured"}
        #: 当前同步客户端句柄（会话存活期非 None；手动拉取直发心跳用）
        self._sync_client = None
        #: 手动拉取事件（断线期缩短重连退避；会话存活期走直发心跳）
        self.sync_pull_event = asyncio.Event()
        #: 最近播报环形记录（dev 回放/状态区展示；内存，不落盘）
        self.recent_speaks: deque[str] = deque(maxlen=20)

        # 环形日志缓冲幂等挂载（dev 控制台数据源；进程级单例）
        ensure_ring_handler()

        self.privacy = privacy or PrivacyState()
        self.store = store
        self.settings = settings
        self.wakeword = _wakeword_text()
        self.route = AudioRoute.WAKE
        self.tts = audio.tts
        self.speaker = speaker or TtsBlockSpeaker(audio.tts)
        #: ASR 引擎引用（dev 控制台识别测试用；离线喂流自建独立流）
        self.asr = audio.asr
        #: 相机/OCR 公开别名（dev 控制台视觉测试用；与管线共享实例，互斥锁防并发）
        self.camera = hal["camera"]
        self.ocr = ocr
        self.wake_stream = audio.wake_kws.create_stream()
        self.asr_stream = audio.asr.create_stream()
        self.playback_stream = audio.playback_kws.create_stream()

        self.ir_service = IRService(store, hal["ir"])
        self.learning = LearningManager(self.ir_service)
        self.callbacks = DeviceCallbacks(self)

        # LLM 注入点：未注入按配置构建（R28 等待反馈挂播报）；显式落属性，
        # 消除 _build_engine 的隐藏副作用（评审：装配顺序耦合）
        if llm is None:
            llm = build_llm(
                cfg,
                privacy=self.privacy,
                network_ok=self._network_ok,
                on_wait_feedback=lambda: self.callbacks.speak(pb.WAIT_REMOTE),
            )
        self._llm = llm
        self._snapshot_announcer = make_snapshot_announcer(store, self.callbacks.speak)

        self.engine = self._build_engine(llm)
        self.pipeline = ReadingPipeline(
            hal["camera"],
            ocr,
            self.speaker,
            on_capture_done=lambda ok: self.spawn(self.engine.on_capture_done(ok)),
            on_reading_finished=lambda: self.spawn(self.engine.on_reading_finished()),
            text_polisher=make_text_polisher(self._llm),
        )
        self._gate = PrivacyGatedSink(
            RouteDispatch(streams=self, wakeword=self.wakeword, spawn=self.spawn),
            self.privacy,
        )
        # 隐私门订阅注册（privacy.py 约定的广播次序：gate.apply -> 唤醒门 -> 播报）
        self.privacy.subscribe(self._gate.apply)
        # 隐私后果播报最后注册（attach_announcer 纪律：最后注册 -> 最后播报）
        attach_announcer(self.privacy, self.callbacks.speak)

    def _build_engine(self, llm):
        """状态机 + 三级路由装配。"""
        from lira.llm.client import make_remote_handler

        router = Router(
            appliances=lambda: tuple(a.to_dialog() for a in self.store.get_all_appliances()),
            remote_llm=make_remote_handler(llm),
            remote_available=llm.is_available,
        )
        return DialogEngine(
            router,
            self.callbacks,
            wake_allowed=lambda: not self.privacy.is_on,
        )

    # ---------- 生命周期 ----------

    def status(self) -> dict[str, object]:
        """整机状态面（R1）：/status 与 dev 状态区共用。仅枚举/布尔/数值。"""
        breaker = getattr(self._llm, "breaker", None)
        breaker_state = getattr(breaker, "state", None)
        try:
            epoch = self.store.current_epoch()
            version = self.store.current_version()
        except Exception:  # noqa: BLE001 - store 异常不拖垮状态面
            epoch = version = None
        return {
            "engine": self.engine.state.value,
            "route": self.route.value,
            "privacy_on": self.privacy.is_on,
            "sync": {
                "session": self.sync_status.get("session", "unknown"),
                "epoch": epoch,
                "version": version,
            },
            "network": bool(self._network_ok()),
            "breaker": breaker_state.name if breaker_state is not None else "unknown",
            "models": {
                "audio": _audio_models_ready(),
                "ocr": _ocr_models_ready(self._cfg),
            },
        }

    def sync_pull(self) -> str:
        """手动拉取（dev/同步测试）：会话存活 → 立即心跳；断线 → 提前重连。"""
        if self._sync_client is not None:
            self.spawn(self._sync_client.heartbeat_once())
            return "heartbeat"
        if not self._cfg.sync.ws_url:
            return "not_configured"
        self.sync_pull_event.set()
        return "reconnect"

    def _spawn(self, coro, tasks: set, *, log_errors: bool = False) -> asyncio.Task:
        """任务跟踪骨架：tasks 集合归属区分长驻伴随与引擎在途工作。"""
        task = asyncio.ensure_future(coro)
        tasks.add(task)

        def _done(task: asyncio.Task) -> None:
            tasks.discard(task)
            if log_errors and not task.cancelled() and task.exception() is not None:
                logging.error("主循环任务异常", exc_info=task.exception())

        task.add_done_callback(_done)
        return task

    def spawn(self, coro) -> asyncio.Task:
        """状态机/管线在途任务：停机取消 + 异常记日志（settle/drain 的收敛面）。"""
        return self._spawn(coro, self._pending, log_errors=True)

    def spawn_companion(self, coro) -> asyncio.Task:
        """长驻伴随任务（UI serve / 同步监督）：停机统一取消，不进 _pending。

        异常同样记 ERROR——伴随任务静默死亡 = UI/同步无声消失，必须可见。
        """
        return self._spawn(coro, self._companions, log_errors=True)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass  # 非 POSIX（如 Windows）退化：Ctrl-C 走 KeyboardInterrupt

    async def run(self) -> None:
        """进入主循环直至停机信号或音频源 EOF（mock 源语义）。"""
        self._install_signal_handlers()
        from contextlib import AsyncExitStack

        stack = AsyncExitStack()
        try:
            for name, resource in self._hal.items():
                await stack.enter_async_context(resource)
                logging.info("HAL 资源已就绪: %s", name)
            if self._cfg.sync.ws_url:
                self.spawn_companion(self._run_sync())
            else:
                logging.info("同步未配置（sync.ws_url 为空）：离线纯本地运行")
            self._ui_task = self.spawn_companion(self._run_ui())
            self.spawn_companion(self._network_ok.run(self._stop))
            print("LIRA 运行中（Ctrl-C 退出）...")
            await self._orchestrate()
        finally:
            await self._shutdown(stack)

    async def _orchestrate(self) -> None:
        """编排主循环：音频读取 -> 隐私门 -> 路由分发 -> 状态机 tick。"""
        import numpy as np  # 惰性导入：仅真实主循环需要（dry-run 不触发）
        from lira.audio.mic import CHUNK_FRAMES  # 同上：mic 顶部导入 numpy

        source = self._hal["audio"]
        while not self._stop.is_set():
            chunk = await source.read_chunk(CHUNK_FRAMES * 2)
            if not chunk:
                logging.info("音频源结束（EOF），主循环退出")
                break
            samples = np.frombuffer(chunk, dtype=np.int16)
            samples = samples.astype(np.float32) / 32768.0
            self._gate.feed(samples)
            self.engine.tick(self._clock())

    # ---------- 伴随任务：同步 / UI ----------

    async def _run_sync(self) -> None:
        """同步伴随任务：断线指数退避重连（transport 工厂可注入，测试用替身）。

        退避重置纪律（评审修正）：connect() 成功不足以重置——只有健康会话
        （时长 >= SYNC_HEALTHY_SESSION_SECONDS）结束才重置，否则"连上即死"的
        后端会以固定 2s 节奏无限重连。生产 transport 须先 open()（P0 修复）。
        """
        sync_cfg = self._cfg.sync
        backoff = SYNC_RECONNECT_MIN_SECONDS
        if not sync_cfg.ws_url:
            self.sync_status = {"session": "not_configured"}
            return
        while not self._stop.is_set():
            transport = self._sync_transport_factory(sync_cfg.ws_url)
            client = SyncClient(
                store=self.store,
                transport=transport,
                token=sync_cfg.device_token,
                learn_handler=lambda d, a: self.learning.begin(d, a),
                privacy=self.privacy,
                tts_settings=self.settings,
                on_snapshot_applied=self._snapshot_announcer,
            )
            session_ok = False
            try:
                # 生产传输层显式建连（ScriptedTransport 等测试替身可无 open）
                opener = getattr(transport, "open", None)
                if opener is not None:
                    await opener()
                started = time.monotonic()
                await client.connect()
                self._sync_client = client
                self.sync_status = {
                    "session": "connected",
                    "epoch": self.store.current_epoch(),
                    "version": self.store.current_version(),
                }
                logging.info("后台同步已连接")
                await client.run_forever(heartbeat_seconds=sync_cfg.heartbeat_seconds)
                session_ok = True
                self.sync_status = {"session": "disconnected", "reason": "后台关闭连接"}
                logging.info("同步会话结束（后台关闭连接）")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 中断由监督循环退避重连，不拖垮主循环
                self.sync_status = {
                    "session": "interrupted",
                    "reason": str(exc)[:80],
                    "backoff": backoff,
                }
                logging.warning("同步会话中断: %s（%.0fs 后重连）", exc, backoff)
            finally:
                self._sync_client = None
                await transport.close()
            # 健康会话才重置退避（短命会话继续指数退避，防固定节奏重连风暴）
            if session_ok and (time.monotonic() - started) >= SYNC_HEALTHY_SESSION_SECONDS:
                backoff = SYNC_RECONNECT_MIN_SECONDS
            if self._stop.is_set():
                return
            # 退避等待：stop 或 pull 事件先到即醒（pull = 提前重连尝试）
            stop_wait = asyncio.ensure_future(self._stop.wait())
            pull_wait = asyncio.ensure_future(self.sync_pull_event.wait())
            done, pending = await asyncio.wait(
                {stop_wait, pull_wait},
                timeout=backoff,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self.sync_pull_event.clear()
            backoff = min(backoff * 2, SYNC_RECONNECT_MAX_SECONDS)

    def _dev_handle(self):
        """dev 控制台句柄（enabled 时返回 DevHandle，否则 None = 不挂载）。"""
        if not self._cfg.dev_console.enabled:
            return None
        from lira.ui.dev import DevHandle

        return DevHandle(self)

    async def _run_ui(self) -> None:
        """设备 Web UI 伴随任务（uvicorn in-process）。依赖缺失 -> 告警返回。"""
        try:
            import uvicorn

            from lira.ui.app import UiServices, create_app
        except ImportError as exc:
            logging.warning("UI 依赖未安装（pip install -e '.[ui]'）: %s", exc)
            return
        services = UiServices(
            privacy=self.privacy,
            vault=self._vault,
            store=self.store,
            settings=self.settings,
            remote_available=self._llm.is_available,
            network_ok=self._network_ok,
            status_provider=self.status,
            dev=self._dev_handle(),
        )
        app = create_app(services)
        # 显式 int 级别的日志配置：板上实测 uvicorn 默认 dictConfig 的字符串
        # 级别（'INFO'）会在 _checkLevel 处报 Unknown level（仅完整运行进程内
        # 复现，隔离不可复现；int 级别绕开字符串解析，根因存疑待查）
        uvicorn_log_config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "%(levelprefix)s %(message)s",
                }
            },
            "handlers": {
                "default": {
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                }
            },
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": 20, "propagate": False},
                "uvicorn.error": {"level": 20},
                "uvicorn.access": {"handlers": ["default"], "level": 20, "propagate": False},
            },
        }
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self._cfg.ui.host,
                port=self._cfg.ui.port,
                access_log=False,
                log_config=uvicorn_log_config,
            )
        )
        self._ui_server = server
        try:
            await server.serve()
        except (SystemExit, OSError) as exc:
            # uvicorn 端口绑定失败走 sys.exit(1)（SystemExit）或抛 OSError：
            # 语音主功能不陪葬，降级为纯语音运行（评审 P2）
            logging.warning("UI 服务不可用（%s），设备降级为纯语音模式", exc)
        finally:
            # Ctrl-C/SIGTERM 由 uvicorn 捕获时（capture_signals 暂接管信号）：
            # serve 正常返回即驱动整机停机；UI 自身故障（绑端口失败）不触发停机
            if server.should_exit:
                self._stop.set()

    async def _shutdown(self, stack) -> None:
        """优雅停机：UI 先退（should_exit -> serve 自然返回）->
        取消引擎在途任务与伴随任务 -> 释放 HAL。幂等。"""
        if self._ui_server is not None:
            self._ui_server.should_exit = True
        if self._ui_task is not None and not self._ui_task.done():
            # 等 serve 自然返回（0.1s 轮询），超时取消兜底
            try:
                await asyncio.wait_for(asyncio.shield(self._ui_task), timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                self._ui_task.cancel()
        tasks = [
            t
            for t in list(self._pending) + list(self._companions)
            if t is not self._ui_task
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await stack.aclose()
        logging.info("LIRA 已退出，HAL 资源已释放。")


# ---------- dry-run 装配图（仅 cfg + 模型文件探测，不构造引擎） ----------


def _count_mock_images(cfg: AppConfig) -> int:
    """dry-run 不打开 HAL，直接数目录里的图片文件。"""
    d = cfg.hal.mock_images_dir
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def _audio_models_ready() -> bool:
    return bool(
        KWS_MODEL_DIR.is_dir()
        and ASR_MODEL_DIR.is_dir()
        and TTS_MODEL_DIR.is_dir()
        and VOCODER_ONNX.is_file()
        and WAKEWORD_KEYWORDS_FILE.is_file()
    )


def _ocr_models_ready(cfg: AppConfig) -> bool:
    suffix = "rknn" if cfg.hal.backend == "board" else "onnx"
    return all(
        (MODELS_DIR / name).is_file()
        for name in (f"ppocrv4_det.{suffix}", f"ppocrv4_rec.{suffix}", "ppocr_keys_v1.txt")
    )


def print_assembly(cfg: AppConfig, privacy: PrivacyState) -> None:
    """打印模块装配图（dry-run 的核心产出）。不构造引擎，README 兼容仅 PyYAML。"""
    bar = "=" * 62
    print(bar)
    print("LIRA 模块装配图 (dry-run)")
    print(bar)
    print(f"  HAL backend : {cfg.hal.backend}")
    if cfg.hal.backend == "board":
        print(f"    mic       : SoundDeviceMic <- {cfg.hal.mic_device or '<系统默认设备>'}")
        print(f"    speaker   : SoundDevicePlayer <- {cfg.hal.speaker_device or '<PipeWire 默认>'}")
        print(f"    camera    : V4l2RawCamera <- {cfg.hal.camera_device or '/dev/video0'}")
        print("    ir        : MockIrController（M5 BroadLink 到货前：只记录不发射）")
        print("    display   : MockDisplay（无触摸屏）")
    else:
        print(f"    camera    : MockCamera <- {cfg.hal.mock_images_dir}"
              f" ({_count_mock_images(cfg)} 张样张)")
        print(f"    audio     : MockAudioIO <- {cfg.hal.mock_audio_dir / 'sample_16k.wav'}")
        print("    ir        : MockIrController (内存记录 sent_codes)")
        print("    display   : MockDisplay (内存记录 shown)")
    audio_state = "就绪" if _audio_models_ready() else "<未就绪> python3 models/download_models.py"
    print(f"  语音栈      : sherpa KWS/ASR/TTS  {audio_state}")
    ocr_backend = "RKNNLite（det@core0 + rec@core1）" if cfg.hal.backend == "board" else "onnxruntime"
    ocr_state = "就绪" if _ocr_models_ready(cfg) else "<未就绪>"
    print(f"  OCR         : {ocr_backend}  {ocr_state}")
    api_key_state = "已配置" if cfg.llm.api_key else "<未设置>"
    print(f"  LLM         : {cfg.llm.base_url}  model={cfg.llm.model}  api_key={api_key_state}")
    print(f"  store       : {cfg.db_path} (SQLite WAL, synchronous=FULL)")
    sync_state = cfg.sync.ws_url if cfg.sync.ws_url else "<未配置（离线纯本地）>"
    print(f"  sync        : {sync_state}  heartbeat={cfg.sync.heartbeat_seconds}s")
    print(f"  ui          : http://{cfg.ui.host}:{cfg.ui.port}")
    print(f"  privacy     : {'开启' if privacy.is_on else '关闭'}（初始态）")
    print(f"  log level   : {cfg.log_level}")
    print("  主循环      : 编排器 + 同步监督 + Web UI 伴随任务（M7 接线）")
    print(bar)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lira", description="LIRA 设备端主程序")
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认 LIRA_CONFIG 环境变量或 device/config.yaml）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="装配校验：读配置 + 模型探测，打印装配图后退出；不要求 api_key",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config, require_api_key=not args.dry_run)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    privacy = PrivacyState()
    logging.basicConfig(
        level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if args.dry_run:
        print_assembly(cfg, privacy)
        print("dry-run 完成：装配校验通过，未进入主循环。")
        return 0

    # 真实运行：引擎装配（缺失 -> 明确报错退出，绝不静默残缺运行）
    from lira.appliances.store import ApplianceStore
    from lira.settings import DeviceSettings

    store = None
    try:
        hal = build_mock_hal(cfg) if cfg.hal.backend == "mock" else build_board_hal(cfg)
        audio = build_audio_stack(cfg)
        ocr = build_ocr(cfg)
    except ConfigError as exc:
        print(f"[装配失败] {exc}", file=sys.stderr)
        return 2
    if audio is None:
        print(
            "[装配失败] 语音栈未就绪（模型或依赖缺失，详见日志）。"
            "运行 python3 models/download_models.py，并安装 extras（pip install -e '.[audio]'）。",
            file=sys.stderr,
        )
        return 2

    store = ApplianceStore(cfg.db_path)
    try:
        settings = DeviceSettings()
        runtime = DeviceRuntime(
            cfg=cfg, hal=hal, store=store, settings=settings,
            audio=audio, ocr=ocr, privacy=privacy,
        )
    except ConfigError as exc:
        # build_llm 依赖缺失等装配期错误（store 已建，保证关闭）
        print(f"[装配失败] {exc}", file=sys.stderr)
        store.close()
        return 2

    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        pass
    except (HalError, OSError) as exc:
        # 板上设备不可用（麦克风/摄像头/播放器 PortAudio 与 v4l2 错误族）
        print(f"[硬件错误] {exc}", file=sys.stderr)
        return 3
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
