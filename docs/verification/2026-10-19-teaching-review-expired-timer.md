# Review follow-up: terminal timer mutation (#3)

Receipt: [completed code review](2026-10-19-teaching-code-review.json), finding #3.

The existing cancellation path accepted an expired timer with its current revision, changed its status to cancelled, incremented the revision and deleted the queued expiry event. Updating an expired timer already rejected the request, but omitted its actual status. A second cancellation using the new revision also mutated an already cancelled timer.

Both update and cancellation now require a pending timer and matching timer revision. A conflict reports the actual `timer_revision` and `timer_status`; terminal status, revision and queued evidence remain unchanged. Executor replay of the same cancellation call ID still returns its original successful result. Task-revision revocation and session close retain their existing clearing semantics. No automatic task progression was added.

Proof-first verification (WSL device `.venv`):

- Before the source fix: focused `terminal_timer or cancelled_timer` cases **3 failed, 10 deselected** (1.18 seconds). Late cancellation returned `ok` instead of conflict; terminal update omitted status; repeated cancellation mutated the terminal timer.
- After the source fix: `tests/test_agent_timers.py tests/test_agent_task_state.py tests/test_realtime_capabilities.py` **36 passed** (1.52 seconds). The added tests await the real expiry callback before mutation, verify the original event is consumed exactly once, and cover cancellation idempotency and terminal update/cancel rejection.

Changed source is restricted to `device/lira/agent_timers.py` and `device/tests/test_agent_timers.py`. No classic chain, protocol, role, plan-pair, deployment or service changes.
