"""M5 实机冒烟 v2（诊断版）：逐次打印轮询状态，区分"没捕获"与"被消费"。"""

from __future__ import annotations

import asyncio
import base64
import sys

sys.path.insert(0, "/home/orangepi/lira/device")

from lira.hal.board.ir_broadlink import BroadlinkIrController  # noqa: E402


async def main() -> int:
    window = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    ctrl = BroadlinkIrController("192.168.101.27", "ec:0b:ae:0c:74:e4")
    await ctrl.open()
    device = ctrl._device
    print(f"LEARN-WINDOW: {window:.0f}s 内用原遥控器贴近 RM4 mini 连续按键...", flush=True)
    await device.enter_learning()
    storage_errors = 0
    polls = 0
    code = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    next_rearm = loop.time() + 10.0  # 部分设备学习窗静默超时：定期 re-arm
    while loop.time() < deadline:
        if loop.time() >= next_rearm:
            await device.enter_learning()
            print("(re-arm)", flush=True)
            next_rearm = loop.time() + 10.0
        polls += 1
        try:
            data = await device.check_data()
        except Exception as exc:
            if "Storage" in type(exc).__name__:
                storage_errors += 1
                data = b""
            else:
                raise
        if data:
            code = base64.b64encode(bytes(data)).decode("ascii")
            print(f"CAPTURED at poll#{polls} ({len(data)} bytes)", flush=True)
            break
        await asyncio.sleep(0.5)
    print(f"polls={polls} storage_errors={storage_errors} code={'YES' if code else 'NONE'}",
          flush=True)
    if code is None:
        await ctrl.close()
        return 1
    print(f"LEARNED: {code}", flush=True)
    print("REPLAY in 5s — 观察家电是否反应...", flush=True)
    await asyncio.sleep(5)
    await ctrl.send(code)
    print("SENT OK", flush=True)
    await ctrl.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
