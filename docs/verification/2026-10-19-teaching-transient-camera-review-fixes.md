# Realtime transient camera review findings #5 / #6

Scope: only realtime transient camera ownership and execution. No board capture, deployment, service restart, role/protocol changes, or teaching sequence changes.

## Implementation

- `device/lira/hal/board/camera_isp.py`: `capture_transient()` runs the existing `capture()` algorithm in a dedicated thread with its own asyncio loop. Decode, orientation, luma/adaptive selection, merge, bilateral filtering, exposure and JPEG all execute there; their algorithms are unchanged. A ContextVar is set/reset only within this transient coroutine. Classic `capture()` callers retain the original `_sensor_set` / `_v4l2` method bodies and async execution behavior (the added guarded branches are inactive).
- Transient sensor and stream commands share a bounded command helper (existing 15-second capture timeout), shield subprocess creation, kill on timeout/cancellation, tolerate an already-exited-child kill race, and await `communicate()` to reap before raw frame deletion. Subprocess spawning that completes after cancellation is still awaited and reaped.
- Cross-thread cancellation uses `loop.call_soon_threadsafe(task.cancel)`. Native CPU work cannot be forcibly killed, so transient cancellation waits for the actual thread completion before returning, including repeated caller cancellation. Only then is its raw frame removed and camera ownership released.
- `device/lira/realtime_vision.py`: cancellation revokes the request and cancels the camera coroutine while capture is pending. It waits for camera cleanup inside the existing shared mutex. The native OCR stage continues to use its previous shield/revocation discipline; late results never become published text.

## Verification

Regression red: temporarily restoring the previous `capture_transient()` wrapper (restored to fixed source in `finally`) gives **2 failed**: stalled sensor leaves child un-killed/un-reaped, and actual 4224×3136 merge executes on the main event-loop thread. Raw output: `tmp/transient-camera-red.txt`.

Fixed source: `tests/test_realtime_vision.py tests/test_camera_isp_quality.py tests/test_camera_quality_diagnostics.py` → **68 passed in 8.78s**.

New coverage includes four stalled-child cases (timeout, ordinary cancellation, cancellation racing subprocess creation, child-exit racing kill), asserting raw still exists during reaping and is absent afterward; actual three-frame 4224×3136 merge/filter/exposure/JPEG with an asyncio heartbeat and worker-thread assertion; and cancellation during uninterruptible camera CPU work, asserting the shared lock/raw remain owned until the worker actually finishes. Existing OCR cancellation, continuous receive/control, cleanup, and classic camera algorithm tests remain passing. Earlier thread-switch fixture uses a threading.Event for cross-loop signaling rather than an asyncio.Event bound to another loop.

Limitations: fake subprocesses and synthetic full-resolution images are host verification, not real board camera/subprocess/driver evidence. This does not prove physical capture or OCR correctness and does not enable the feature flag.
