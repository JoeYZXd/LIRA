"""音频路由分发（M7 抽取：生产主循环与 e2e harness 共用同一实现）。

按状态机当前 AudioRoute 把样本喂给三选二识别流（R11/R21）::

    WAKE     -> 主唤醒 KWS；LISTEN -> 流式 ASR；PLAYBACK -> 白名单 KWS
    命中即经注入的 spawn(coro) 驱动状态机；识别文本一律不落日志（日志纪律）。

流与路由状态由装配层持有（"streams holder"：需暴露 route / wake_stream /
asr_stream / playback_stream / wakeword），分发器无自身状态。
"""

from __future__ import annotations

from lira.dialog.state_machine import AudioRoute

__all__ = ["RouteDispatch"]


class RouteDispatch:
    """AudioSink：按持有者的当前 AudioRoute 分发样本，命中即驱动状态机。"""

    def __init__(self, *, streams, wakeword: str, spawn) -> None:
        """Args:
        streams: 持有 route/wake_stream/asr_stream/playback_stream 的对象
            （生产 = DeviceRuntime，测试 = DeviceHarness）。
        wakeword: 唤醒词原文（KWS 命中结果含关键词原文，比对用）。
        spawn: 协程启动器（生产 = runtime.spawn 异常记录；e2e = ensure_future）。
        """
        self._streams = streams
        self._wakeword = streams.wakeword if hasattr(streams, "wakeword") else wakeword
        self._spawn = spawn

    def feed(self, samples) -> None:
        rt = self._streams
        route = rt.route
        if route is AudioRoute.WAKE and rt.wake_stream is not None:
            hit = rt.wake_stream.feed_and_poll(samples)
            if hit and self._wakeword in hit:
                self._spawn(rt.engine.on_wake())
        elif route is AudioRoute.LISTEN and rt.asr_stream is not None:
            rt.asr_stream.feed(samples)
            # 先解码就绪帧再判 endpoint：sherpa 的 endpoint 检测在解码后评估
            rt.asr_stream.text()
            if rt.asr_stream.is_endpoint():
                text = rt.asr_stream.text().strip()
                rt.asr_stream.reset()
                if text:
                    self._spawn(rt.engine.on_asr_text(text))
        elif route is AudioRoute.PLAYBACK and rt.playback_stream is not None:
            hit = rt.playback_stream.feed_and_poll(samples)
            if hit:
                self._spawn(rt.engine.on_asr_text(hit.split("@")[-1].strip()))