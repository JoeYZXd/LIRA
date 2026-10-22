# Review finding 1: bounded realtime capacity

Review: `20261019-teaching-48d377da/review.json`, finding 1, P1.

## Proof before changes

Three newly added tests failed against the original implementation: completed ordinary calls returned permanent `busy`; an occupied operation slot prevented `pause_agent_task`; the actual RealtimeSession tool path stopped accepting ordinary calls after its operation history filled. Source inspection also confirmed the separate 64-binding exception was swallowed by detached tool callbacks without returning a tool result.

## Changed semantics

- Limits now bound live execution, not the lifetime count. Default executor limits remain 32 running operations and 64 running calls. Terminal operation results retain the most recent 32; completed call results retain the most recent 64, independently of running work.
- Pause, stop, timer cancellation and owned TV clearing have eight reserved execution slots. Executor list/get/cancel controls also have reserved call slots. Real session bindings reserve eight slots for controls/revocation when 64 ordinary calls are pending. Saturation returns a tool `busy` result over the existing envelope, rather than throwing silently.
- Retained calls replay the original snapshot and reject changed names/arguments. Completed operations remain discoverable across user turns. Evicted calls return `expired` without execution; lookup of a retired operation returns `expired` rather than `unknown`.
- Each retired-ID tombstone is a fixed 64 KiB Bloom bitmap with four SHA-256-derived indices (approximately 0.04% false-positive rate after 20,000 retired IDs). There are no false negatives: an evicted ID cannot execute again. A false positive conservatively returns `expired` for a new ID. Extreme saturation may reject most new IDs; this is an explicit fail-closed retention limit, not permanent ordinary `busy` after 32/64 calls. Arbitrarily many exact IDs cannot be deduplicated with finite memory.
- Physical tasks that have not exited still count toward running operation capacity even when cancellation has been requested. No unbounded completed payload or tombstone collection is introduced.
- Unready exact-speech barriers cannot be evicted with completed acknowledgments. Tool replies use a local snapshot while sending so concurrent eviction cannot cause a post-send cache lookup error.

## Verification

WSL device venv: `pytest tests/test_realtime_capabilities.py tests/test_realtime_speech.py tests/test_agent_timers.py tests/test_agent_task_state.py -q`: **49 passed in 4.94 seconds** after running-task accounting changes.

Coverage includes 80 sequential real session calls; 80 executor calls with retention set to 2/3; retained conflict/replay; retired call refusal; retired operation lookup; real cross-turn result lookup; 32 running operations plus all four revocation handlers and cancel; 64 genuinely pending RealtimeSession bindings with a successfully returned cancel result; speech barriers; timer/task lifecycle.

No wire events, tool names, role prompts, owner-reset policy, classic chain, original plan pair, canonical index or commits changed. Cloud probes and integration verification remain parent-owned.
